"""Parser for the chat.deepseek.com completion SSE stream.

Supports two formats:

* Legacy: every event is ``{"choices": [{"delta": {"content": ..., "type": ...}}], ...}``.
* Patch format (2025+): events are JSON patches ``{"p": path, "o": op, "v": value}``.
  ``p`` and ``o`` may be omitted, in which case the last path is reused and the
  value is appended. The first event usually carries the whole ``response``
  object (``{"v": {"response": {...}}}``).

Each content piece is returned as a ``MessageData`` with a single choice whose
``delta.type`` is ``"thinking"`` or ``"text"``, so callers can keep using the
same API as ``DeepseekAPI.chat``.
"""

import json
from typing import Iterator, List, Optional

from chat_deepseek_api.model import Choice, Delta, MessageData

THINKING = "thinking"
TEXT = "text"


def _fragment_kind(fragment_type: Optional[str]) -> Optional[str]:
    """Delta type for a fragment, or None for fragments that are not answer text (e.g. SEARCH)."""
    kind = (fragment_type or "").upper()
    if kind in ("THINK", "THINKING"):
        return THINKING
    if kind in ("", "RESPONSE", "TEXT"):
        return TEXT
    return None


class StreamParser:
    """Incremental parser: feed raw text chunks, get ``MessageData`` items."""

    def __init__(self):
        self._buffer = ""
        self._last_path: Optional[str] = None
        # One [kind, text emitted so far] per fragment, in stream order.
        self._fragments: List[list] = []
        self.message_id: int = 0
        self.parent_id: int = 0
        self.model: str = ""
        self.finished: bool = False
        self.raw_events: List[str] = []

    # ------------------------------------------------------------------ input
    def feed(self, text: str) -> Iterator[MessageData]:
        """Feed a raw chunk of the SSE body and yield parsed content pieces."""
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            yield from self._parse_line(line)

    def flush(self) -> Iterator[MessageData]:
        """Parse whatever is left in the buffer (stream ended)."""
        line, self._buffer = self._buffer, ""
        yield from self._parse_line(line)

    # --------------------------------------------------------------- parsing
    def _parse_line(self, line: str) -> Iterator[MessageData]:
        line = line.strip()
        if not line.startswith("data:"):
            if line.startswith("event:") and "close" in line:
                self.finished = True
            return
        payload = line[len("data:"):].strip()
        if not payload:
            return
        if payload == "[DONE]":
            self.finished = True
            return
        self.raw_events.append(payload)
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        if "choices" in event:
            yield from self._parse_legacy(event)
        else:
            yield from self._parse_patch(event)

    def _parse_legacy(self, event: dict) -> Iterator[MessageData]:
        data = MessageData.model_validate(event)
        if data.message_id:
            self.message_id = data.message_id
        if data.has_content():
            yield data

    def _parse_patch(self, event: dict) -> Iterator[MessageData]:
        # Metadata events sent before the response stream starts.
        if "response_message_id" in event:
            self.message_id = int(event.get("response_message_id") or 0)
            self.parent_id = int(event.get("request_message_id") or 0)
            return

        path = event.get("p")
        op = event.get("o")
        value = event.get("v")
        if path is not None:
            self._last_path = path
        else:
            path = self._last_path

        if isinstance(value, dict):
            yield from self._parse_object(path, value)
        elif isinstance(value, list):
            yield from self._parse_list(path, op, value)
        elif isinstance(value, str):
            yield from self._parse_string(path, op, value)

    def _parse_object(self, path: Optional[str], value: dict) -> Iterator[MessageData]:
        response = value.get("response") if path in (None, "") else None
        if path == "response":
            response = value
        if not isinstance(response, dict):
            return
        self.message_id = int(response.get("message_id") or self.message_id or 0)
        self.parent_id = int(response.get("parent_id") or self.parent_id or 0)
        self.model = response.get("model") or self.model
        if response.get("thinking_content"):
            yield self._piece(response["thinking_content"], THINKING)
        if response.get("content"):
            yield self._piece(response["content"], TEXT)
        for fragment in response.get("fragments") or []:
            yield from self._add_fragment(fragment)

    def _parse_list(self, path: Optional[str], op, value: list) -> Iterator[MessageData]:
        if path and path.endswith("fragments"):
            fragments = [f for f in value if isinstance(f, dict)]
            if op == "APPEND":
                for fragment in fragments:
                    yield from self._add_fragment(fragment)
            else:
                # SET: the full list again (sent at the end); emit only what is new.
                yield from self._replace_fragments(fragments)
            return
        # Batch of nested patches: {"p": "response", "o": "BATCH", "v": [{"p": "fragments", "v": [...]}, ...]}
        # Sub-paths are relative to the batch path; with web search the RESPONSE fragment arrives here.
        last_path = self._last_path
        for sub in value:
            if isinstance(sub, dict) and "v" in sub and sub.get("p"):
                sub_path = f"{path}/{sub['p']}" if path else sub["p"]
                yield from self._parse_patch({"p": sub_path, "o": sub.get("o"), "v": sub["v"]})
        self._last_path = last_path

    def _parse_string(self, path: Optional[str], op, value: str) -> Iterator[MessageData]:
        if not path:
            return
        if path.endswith("status") and value == "FINISHED":
            self.finished = True
            return
        if not path.endswith("content"):
            return
        if op not in (None, "APPEND"):
            return
        if not value:
            return
        if "fragments" in path:
            fragment = self._fragment_for(path)
            if fragment is None:
                fragment = [TEXT, ""]
                self._fragments.append(fragment)
            fragment[1] += value
            if fragment[0]:
                yield self._piece(value, fragment[0])
            return
        yield self._piece(value, THINKING if "thinking" in path else TEXT)

    # --------------------------------------------------------------- helpers
    def _add_fragment(self, fragment: dict) -> Iterator[MessageData]:
        kind = _fragment_kind(fragment.get("type"))
        content = fragment.get("content") or ""
        self._fragments.append([kind, content])
        if content and kind:
            yield self._piece(content, kind)

    def _replace_fragments(self, fragments: List[dict]) -> Iterator[MessageData]:
        for index, fragment in enumerate(fragments):
            if index >= len(self._fragments):
                yield from self._add_fragment(fragment)
                continue
            known = self._fragments[index]
            content = fragment.get("content") or ""
            if content.startswith(known[1]) and len(content) > len(known[1]):
                suffix = content[len(known[1]):]
                known[1] = content
                if known[0]:
                    yield self._piece(suffix, known[0])

    def _fragment_for(self, path: str) -> Optional[list]:
        # path like "response/fragments/-1/content" or "response/fragments/2/content"
        parts = path.split("/")
        try:
            return self._fragments[int(parts[parts.index("fragments") + 1])]
        except (ValueError, IndexError):
            return None

    def _piece(self, content: str, kind: str) -> MessageData:
        return MessageData(
            choices=[Choice(delta=Delta(content=content, type=kind))],
            model=self.model,
            message_id=self.message_id,
            parent_id=self.parent_id,
        )

"""DeepSeek client that drives a real Chrome with a persistent profile.

The web app itself handles login, the AWS WAF challenge, request encryption and
the proof-of-work header; this class only types into the chat box and reads the
completion stream the page receives. Requires ``pip install chat_deepseek_api[browser]``.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import AsyncIterator, Optional

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from chat_deepseek_api.model import MessageData
from chat_deepseek_api.stream_parser import StreamParser

BASE_URL = "https://chat.deepseek.com"
CHAT_URL_RE = re.compile(r"/a/chat/s/([0-9a-fA-F-]{36})")
COMPLETION_PATTERNS = ("/chat/completion", "/chat/regenerate", "/chat/resume")
INPUT_SELECTOR = "textarea"
DEFAULT_PROFILE_DIR = Path.home() / ".chat-deepseek-api" / "chrome-profile"
DEFAULT_CDP_PORT = 9333
CHROME_CANDIDATES = [
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def find_chrome() -> str:
    """Return the path of the installed Google Chrome (or Chromium)."""
    for candidate in CHROME_CANDIDATES:
        if candidate and os.path.isfile(candidate):
            return candidate
    for name in ("google-chrome", "chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError("Google Chrome not found; pass chrome_path=...")


def _cdp_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
            return True
    except Exception:
        return False


def open_login_window(profile_dir: Optional[str] = None, chrome_path: Optional[str] = None):
    """Open the profile in a plain Chrome (no automation, no debugging port)
    and block until the window is closed. Use it to log in when the anti-bot
    check rejects the automated browser."""
    profile = Path(profile_dir) if profile_dir else DEFAULT_PROFILE_DIR
    profile.mkdir(parents=True, exist_ok=True)
    exe = chrome_path or find_chrome()
    subprocess.run(
        [exe, f"--user-data-dir={profile}", "--no-first-run",
         "--no-default-browser-check", f"{BASE_URL}/sign_in"],
        check=False,
    )

# Tees every completion stream the page reads through fetch() to Python.
FETCH_HOOK = """
(() => {
  if (window.__dsHooked) return;
  window.__dsHooked = true;
  const patterns = %s;
  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const resp = await origFetch.apply(this, args);
    try {
      const req = args[0];
      const url = typeof req === 'string' ? req : (req && req.url) || '';
      if (patterns.some((p) => url.includes(p)) && resp.body) {
        const id = Math.random().toString(36).slice(2);
        const reader = resp.clone().body.getReader();
        const decoder = new TextDecoder();
        window.__dsStream(id, 'start', String(resp.status));
        (async () => {
          try {
            for (;;) {
              const { done, value } = await reader.read();
              if (done) break;
              window.__dsStream(id, 'chunk', decoder.decode(value, { stream: true }));
            }
            window.__dsStream(id, 'end', '');
          } catch (e) {
            window.__dsStream(id, 'error', String(e));
          }
        })();
      }
    } catch (e) {}
    return resp;
  };
})();
""" % json.dumps(list(COMPLETION_PATTERNS))


def _is_completion(url: str) -> bool:
    return any(p in url for p in COMPLETION_PATTERNS)


class DeepseekBrowserAPI:
    """Browser-backed counterpart of ``DeepseekAPI`` (same ``chat`` interface)."""

    def __init__(
        self,
        profile_dir: Optional[str] = None,
        headless: bool = False,
        channel: Optional[str] = "chrome",
        login_timeout: float = 300,
        response_timeout: float = 300,
        verbose_level: int = 0,
        dump_dir: Optional[str] = None,
        mode: str = "cdp",
        chrome_path: Optional[str] = None,
        cdp_port: int = DEFAULT_CDP_PORT,
        keep_open: bool = False,
    ):
        """
        Parameters:
        profile_dir (str): Chrome user-data dir; the login is kept there between runs.
        headless (bool): Run without a window (``--headless=new``; the
            "HeadlessChrome" user agent is replaced by the regular one). Needs
            a profile that is already logged in; fails fast otherwise.
        mode (str): "cdp" (default) starts the installed Chrome as a normal
            process with a debugging port and attaches to it, so the browser
            carries no automation flags. If a Chrome is already listening on
            ``cdp_port`` it is reused. "persistent" lets Playwright launch it
            (``launch_persistent_context``); simpler but easier to detect.
        chrome_path (str): Chrome executable for "cdp" mode (auto-detected).
        cdp_port (int): Debugging port for "cdp" mode.
        keep_open (bool): In "cdp" mode, leave the Chrome window running on
            close() so the next run can attach to it.
        channel (str): Browser channel for "persistent" mode ("chrome",
            "msedge") or None for Playwright's bundled Chromium.
        login_timeout (float): Seconds to wait for a manual login in the window.
        response_timeout (float): Seconds to wait between stream events.
        dump_dir (str): If set, every raw completion stream is saved there.
        """
        self.profile_dir = Path(profile_dir) if profile_dir else DEFAULT_PROFILE_DIR
        self.headless = headless
        self.channel = channel
        self.login_timeout = login_timeout
        self.response_timeout = response_timeout
        self.verbose_level = verbose_level
        self.dump_dir = Path(dump_dir) if dump_dir else None
        if mode not in ("cdp", "persistent"):
            raise ValueError("mode must be 'cdp' or 'persistent'")
        self.mode = mode
        self.chrome_path = chrome_path
        self.cdp_port = cdp_port
        self.keep_open = keep_open

        self.chat_id: Optional[str] = None
        self._chrome_proc: Optional[subprocess.Popen] = None
        self._browser = None
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._events: "asyncio.Queue[tuple]" = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._overrides: dict = {}

    # ------------------------------------------------------------ lifecycle
    async def __aenter__(self):
        try:
            await self._start()
        except BaseException:
            # Do not leave a Chrome process behind when startup fails.
            await self.__aexit__(None, None, None)
            raise
        return self

    async def _start(self):
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        if self.mode == "cdp":
            await self._attach_cdp()
        else:
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                channel=self.channel,
                headless=self.headless,
                no_viewport=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
        await self._context.expose_function("__dsStream", self._on_stream_event)
        await self._context.add_init_script(FETCH_HOOK)
        existing = [p for p in self._context.pages if p.url.startswith(BASE_URL)]
        if not existing and self.mode == "persistent":
            existing = self._context.pages
        self._page = existing[0] if existing else await self._context.new_page()
        if self.headless:
            await self._hide_headless_user_agent()
        self._page.on("response", self._on_response)
        await self._page.route("**/api/v0/chat/completion*", self._rewrite_completion)
        await self._page.goto(BASE_URL)
        await self._ensure_logged_in()

    async def _hide_headless_user_agent(self):
        """Headless Chrome reports "HeadlessChrome/x" as user agent, an easy
        anti-bot signal; replace it with the regular "Chrome/x" one."""
        cdp = await self._context.new_cdp_session(self._page)
        version = await cdp.send("Browser.getVersion")
        user_agent = version["userAgent"].replace("HeadlessChrome", "Chrome")
        full_version = version["product"].split("/")[-1]
        major = full_version.split(".")[0]
        platform = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux")
        brands = [
            {"brand": "Google Chrome", "version": major},
            {"brand": "Chromium", "version": major},
            {"brand": "Not.A/Brand", "version": "24"},
        ]
        # Without metadata Chrome clears navigator.userAgentData and the
        # sec-ch-ua headers, which is itself unusual.
        await cdp.send("Emulation.setUserAgentOverride", {
            "userAgent": user_agent,
            "userAgentMetadata": {
                "brands": brands,
                "fullVersionList": [dict(b, version=full_version) for b in brands],
                "fullVersion": full_version,
                "platform": platform,
                "platformVersion": "",
                "architecture": "x86",
                "model": "",
                "mobile": False,
            },
        })

    async def _attach_cdp(self):
        if not _cdp_alive(self.cdp_port):
            args = [
                self.chrome_path or find_chrome(),
                f"--remote-debugging-port={self.cdp_port}",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if self.headless:
                args.append("--headless=new")
            self._chrome_proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            deadline = time.monotonic() + 30
            while not _cdp_alive(self.cdp_port):
                if self._chrome_proc.poll() is not None:
                    raise RuntimeError(
                        "Chrome exited at startup. Is another Chrome already "
                        f"using the profile {self.profile_dir}? Close it and retry."
                    )
                if time.monotonic() > deadline:
                    raise TimeoutError("Chrome did not open its debugging port")
                await asyncio.sleep(0.3)
        self._browser = await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.cdp_port}"
        )
        self._context = self._browser.contexts[0]

    async def __aexit__(self, exc_type, exc, tb):
        if self.mode == "cdp":
            if self._chrome_proc and not self.keep_open:
                # We started this Chrome: close it (Browser.close over CDP).
                try:
                    await self._browser.close()
                except Exception:
                    pass
                try:
                    self._chrome_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._chrome_proc.kill()
            elif self._page:
                # Attached to a Chrome we did not start: just detach.
                try:
                    await self._page.unroute("**/api/v0/chat/completion*")
                except Exception:
                    pass
        elif self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        self._context = self._page = self._playwright = self._browser = None
        self._chrome_proc = None

    @staticmethod
    async def create(*args, **kwargs) -> "DeepseekBrowserAPI":
        self = DeepseekBrowserAPI(*args, **kwargs)
        await self.__aenter__()
        return self

    async def close(self):
        await self.__aexit__(None, None, None)

    # ----------------------------------------------------------------- login
    async def _ensure_logged_in(self):
        page = self._page
        try:
            await page.wait_for_selector(INPUT_SELECTOR, timeout=10_000)
            return
        except Exception:
            pass
        if self.headless:
            raise RuntimeError(
                "Not logged in and Chrome is headless, so there is no window to "
                "log in. Run open_login_window() (examples/browser_app.py --login) "
                "first, then retry."
            )
        self._log(
            "Not logged in: log in in the Chrome window "
            f"(waiting up to {int(self.login_timeout)} s). If the anti-bot check "
            "keeps failing, close everything and run open_login_window() "
            "(examples/browser_app.py --login) first.",
            level=0,
        )
        await page.wait_for_selector(INPUT_SELECTOR, timeout=self.login_timeout * 1000)
        self._log("Logged in.", level=0)

    # ----------------------------------------------------------------- chats
    async def new_chat(self) -> None:
        """Open an empty chat. DeepSeek assigns the session id on the first
        message; it is available afterwards as ``self.chat_id``."""
        await self._page.goto(BASE_URL)
        await self._page.wait_for_selector(INPUT_SELECTOR, timeout=30_000)
        self.chat_id = None
        return None

    async def open_chat(self, chat_id: str):
        """Open an existing chat session by id."""
        await self._page.goto(f"{BASE_URL}/a/chat/s/{chat_id}")
        await self._page.wait_for_selector(INPUT_SELECTOR, timeout=30_000)
        self.chat_id = chat_id

    async def chat(
        self,
        message: str,
        id: Optional[str] = None,
        parent_message_id=None,
        thinking_enabled: Optional[bool] = None,
        search_enabled: Optional[bool] = None,
    ) -> AsyncIterator[MessageData]:
        """Send ``message`` through the web UI and yield the response pieces.

        ``id`` opens that chat session first; ``None`` continues the chat that is
        open in the page (call ``new_chat()`` to start a fresh one).
        ``parent_message_id`` is accepted for compatibility and ignored: the web
        app always continues from the last message of the open chat.
        ``thinking_enabled`` / ``search_enabled`` override the request sent by
        the page; ``None`` keeps what the UI has selected.
        """
        async with self._lock:
            if id and id != self.chat_id:
                await self.open_chat(id)

            self._overrides = {}
            if thinking_enabled is not None:
                self._overrides["thinking_enabled"] = thinking_enabled
            if search_enabled is not None:
                self._overrides["search_enabled"] = search_enabled

            self._drain_events()
            box = self._page.locator(INPUT_SELECTOR).first
            await box.fill(message)
            await box.press("Enter")

            async for piece in self._read_stream():
                yield piece

            match = CHAT_URL_RE.search(self._page.url)
            if match:
                self.chat_id = match.group(1)

    # -------------------------------------------------------------- streaming
    def _on_stream_event(self, stream_id: str, kind: str, data: str):
        self._events.put_nowait(("hook", stream_id, kind, data))

    async def _on_response(self, response):
        if not _is_completion(response.url):
            return
        try:
            body = await response.text()
        except Exception as e:  # body not available (e.g. page navigated)
            body = f"__error__ {e}"
        self._events.put_nowait(("response", "", str(response.status), body))

    async def _rewrite_completion(self, route, request):
        if not self._overrides or request.method != "POST":
            await route.continue_()
            return
        try:
            body = json.loads(request.post_data or "{}")
        except json.JSONDecodeError:
            # Encrypted or non-JSON body: leave it untouched.
            await route.continue_()
            return
        body.update(self._overrides)
        await route.continue_(post_data=json.dumps(body))

    def _drain_events(self):
        while not self._events.empty():
            self._events.get_nowait()

    async def _read_stream(self) -> AsyncIterator[MessageData]:
        parser = StreamParser()
        raw = []
        hooked = False
        status = None
        yielded = False
        while True:
            try:
                source, _sid, kind, data = await asyncio.wait_for(
                    self._events.get(), timeout=self.response_timeout
                )
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"No response from DeepSeek in {self.response_timeout} s"
                ) from None

            if source == "hook":
                if kind == "start":
                    hooked = True
                    status = int(data)
                    continue
                if kind == "chunk":
                    raw.append(data)
                    for piece in parser.feed(data):
                        yielded = True
                        yield piece
                    continue
                if kind == "error":
                    raise RuntimeError(f"Stream error in page: {data}")
                # kind == "end"
                for piece in parser.flush():
                    yielded = True
                    yield piece
                break

            # source == "response": full body once the request finished.
            if hooked:
                continue  # already streamed through the fetch hook
            status = int(kind)
            raw.append(data)
            for piece in parser.feed(data):
                yielded = True
                yield piece
            for piece in parser.flush():
                yielded = True
                yield piece
            break

        text = "".join(raw)
        self._dump(text)
        if status and status != 200:
            raise RuntimeError(f"Chat request failed with status {status}: {text[:500]}")
        if not yielded and "data:" not in text:
            raise RuntimeError(f"Unexpected completion response: {text[:500]}")

    # ---------------------------------------------------------------- helpers
    def _dump(self, text: str):
        if not self.dump_dir:
            return
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        path = self.dump_dir / f"stream-{int(time.time() * 1000)}.txt"
        path.write_text(text, encoding="utf-8")
        self._log(f"Stream saved to {path}")

    def _log(self, message: str, level: int = 1):
        if self.verbose_level >= level:
            print(message, flush=True)

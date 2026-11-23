# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Unofficial, reverse-engineered async Python wrapper for the chat.deepseek.com web API (not the official DeepSeek platform API), published on PyPI as `chat-deepseek-api`. The import package is `chat_deepseek_api` (the README sample still uses the old `deepseek_api` name; `examples/simple_app.py` is the correct reference).

`USAGE.md` is the short how-to for sessions that only need to *use* the client (CLI, Python snippet, limits, common errors); keep it in sync when the public API of `DeepseekBrowserAPI` or the example flags change.

## Commands

- Install deps: `pip install -e .[browser]` into a venv (`.venv/` is gitignored). The pins in `requirements.txt` (e.g. aiohttp 3.9.2) do not build on Python 3.14; `setup.py` holds the looser `install_requires` plus the `browser` extra (Playwright). Keep both files in sync when adding a dependency.
- Browser client (the one that works against the current site): `python examples/browser_app.py --login` once (opens the profile in a plain Chrome; log in, close the window), then `python examples/browser_app.py "question"` or with no args for an interactive loop. Flags: `--think`, `--search`, `--dump <dir>` (save raw SSE streams), `--profile` (default `<repo>/.chrome-profile/`, gitignored), `--headless`, `-v`.
- HTTP client example (needs a filled `.env` from `.env.example`): `python examples/simple_app.py`. As of 2026-11 it fails at login: CloudFront/AWS WAF answers `202` with `x-amzn-waf-action: challenge`, and the web now encrypts the login body.
- Build and check the package: `make build-package` (`python -m build` + `twine check dist/*`; requires `build` and `twine`).
- Publish to PyPI: `make distribute`. Bump `version` in `setup.py` first.

There is no test suite or linter configured. Verifying a change means running an example against the live service. Parser changes can be checked offline by replaying streams saved with `--dump` through `StreamParser.feed()` in small chunks.

## Architecture

Two clients share `MessageData` (`model.py`) as the unit yielded by `chat()`.

### Browser client (`browser_api.py` + `stream_parser.py`)

`DeepseekBrowserAPI` drives a real Chrome, so the site itself handles login, the AWS WAF challenge, request encryption and the `x-ds-pow-response` proof of work.

- `mode="cdp"` (default) starts the installed Chrome as a plain process with `--remote-debugging-port` (default 9333) and `--user-data-dir` (library default `~/.chat-deepseek-api/chrome-profile`; the example overrides it with `<repo>/.chrome-profile/`), then `connect_over_cdp`. If something already listens on the port it attaches instead and does not close it. `headless=True` adds `--headless=new` and overrides the user agent over CDP (`Emulation.setUserAgentOverride`, including `userAgentMetadata`, or `userAgentData.brands` ends up empty) to drop the `HeadlessChrome` token; it raises instead of waiting when the profile is not logged in. `mode="persistent"` uses Playwright's `launch_persistent_context`; the anti-bot check rejected logins in that mode, which is why CDP and `open_login_window()` (plain Chrome, no debugging port) exist.
- `chat()` fills the first `textarea`, presses Enter and reads the response through a `window.fetch` wrapper injected with `add_init_script`, which tees `/chat/completion` (also `regenerate`/`resume`) chunks to Python via `expose_function("__dsStream")`. A `page.on("response")` full-body fallback covers non-fetch transports. `thinking_enabled` / `search_enabled` are applied by rewriting the request JSON in `page.route`; `None` leaves the UI's choice.
- `parent_message_id` is ignored: the UI always continues the open chat. `new_chat()` returns `None`; the session id is read from the URL (`/a/chat/s/<uuid>`) after the first message and kept in `self.chat_id`. One `chat()` at a time (`asyncio.Lock`).
- `StreamParser` handles the legacy `choices[].delta` events and the current JSON-patch events (`{"p": path, "o": op, "v": value}`, where a bare `{"v": "..."}` appends to the last path). The current site streams `response/fragments`: fragments of type `THINK` become `delta.type == "thinking"`, `RESPONSE` becomes `"text"`. At the end it re-sends the whole fragment list with no `o` (a SET); the parser tracks what it already emitted per fragment and only yields new suffixes. `event:` lines (`ready`, `title`, `close`) are not content.

### HTTP client (`deepseek_api.py`)

- `DeepSeekBase` (ABC) validates constructor input and builds two header dicts from `constants.py`: `headers` (login / session creation) and `completion_headers` (chat completion). Both are merged with the user's `custom_headers`, which **must** contain `cookie` and `x-ds-pow-response` (copied by hand from a logged-in browser session) or `MissingHeaderConfigError` is raised. The proof-of-work header is not computed by the library.
- `DeepseekAPI` is the only concrete implementation. Instantiate via `await DeepseekAPI.create(...)` (or `async with`), which opens an `aiohttp.ClientSession` and logs in. Always `await app.close()`.
- Login: `POST /users/login` stores the response in `self.credentials`; the JWT is at `credentials["data"]["user"]["token"]`. With `save_login=True`, credentials are cached in `login.json` in the **current working directory** and reused without re-validating. `_schedule_update_token` is a no-op (token refresh is commented out).
- `new_chat()` → `POST /chat_session/create`, returns `data.biz_data.id`.
- `chat()` is an async generator: `POST /chat/completion`, reads the SSE stream line by line, strips `data: `, skips `[DONE]`, and yields `MessageData` (pydantic, `model.py`) only for chunks with non-empty `choices[0].delta.content`. Callers must track the highest `message_id` seen and pass it as `parent_message_id` on the next turn to continue the conversation (see the example).
- Endpoints are in `constants.API_URL` (base `https://chat.deepseek.com/api/v0`). The hardcoded browser headers there (user-agent, `x-app-version`, etc.) mimic the web client; when the upstream site changes, breakage usually shows up here or in the `MessageData` schema.

## Gotchas

- `constants.py` uses `enum.StrEnum`, so the real minimum is Python 3.11, even though `setup.py` says `>=3.6`.
- Errors from the HTTP layer are raised as plain `Exception` with the response text; only the three classes in `errors.py` are custom.
- `.env`, `login.json` and the Chrome profile directory contain credentials/session tokens — never commit them.
- Chrome refuses to start a second instance on the same `--user-data-dir`; close leftover windows of the profile before running the browser client.
- DOM assumptions in the browser client are minimal (`textarea` + Enter); if the site changes, `INPUT_SELECTOR` and the `COMPLETION_PATTERNS` URLs are the places to adjust.

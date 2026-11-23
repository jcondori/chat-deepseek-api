"""Chat with chat.deepseek.com through a real Chrome window.

First run: log in once in a plain Chrome window (no automation, so the
anti-bot check passes), then close that window. The profile keeps the session.

    python examples/browser_app.py --login               # log in, then close Chrome
    python examples/browser_app.py                       # interactive chat
    python examples/browser_app.py "who are you"         # one question
    python examples/browser_app.py --think "2+2*2?"      # with DeepThink
"""

import argparse
import asyncio
import sys
from pathlib import Path

from chat_deepseek_api.browser_api import DeepseekBrowserAPI, open_login_window
from chat_deepseek_api.stream_parser import THINKING

# Keep the Chrome profile inside the repo (gitignored) instead of ~/.chat-deepseek-api.
REPO_PROFILE_DIR = Path(__file__).resolve().parent.parent / ".chrome-profile"


async def ask(app: DeepseekBrowserAPI, prompt: str, think, search):
    in_thinking = False
    async for chunk in app.chat(prompt, thinking_enabled=think, search_enabled=search):
        delta = chunk.choices[0].delta
        if delta.type == THINKING and not in_thinking:
            print("[thinking] ", end="", flush=True)
            in_thinking = True
        elif delta.type != THINKING and in_thinking:
            print("\n[answer] ", end="", flush=True)
            in_thinking = False
        print(delta.content, end="", flush=True)
    print(flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="*")
    parser.add_argument("--think", action="store_true", default=None)
    parser.add_argument("--search", action="store_true", default=None)
    parser.add_argument("--profile", default=str(REPO_PROFILE_DIR),
                        help="Chrome profile directory (default: <repo>/.chrome-profile)")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--dump", help="Save raw completion streams to this folder")
    parser.add_argument("--login", action="store_true",
                        help="Open the profile in a plain Chrome to log in, then exit")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args()

    if args.login:
        print("Log in to DeepSeek in the Chrome window, then close it.")
        open_login_window(args.profile)
        return

    app = await DeepseekBrowserAPI.create(
        profile_dir=args.profile,
        headless=args.headless,
        dump_dir=args.dump,
        verbose_level=args.verbose,
    )
    try:
        await app.new_chat()
        if args.prompt:
            await ask(app, " ".join(args.prompt), args.think, args.search)
            print(f"(chat id: {app.chat_id})", file=sys.stderr)
            return
        while True:
            try:
                prompt = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if prompt in ("", "exit", "quit"):
                break
            await ask(app, prompt, args.think, args.search)
    finally:
        await app.close()


if __name__ == "__main__":
    asyncio.run(main())

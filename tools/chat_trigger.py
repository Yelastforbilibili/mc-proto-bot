"""Send chat lines to a running acceptance suite and print its replies.

Start the suite in chat mode in one shell::

    python tools/live_suite.py --version 26.3

Then trigger it from another, without opening the game::

    python tools/chat_trigger.py "0" "?22" "2"

Each argument is one chat message; the script waits between messages (the
server kicks above its chat spam budget) and prints every reply the suite sends
back, so the chat path can be verified end to end.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from protobot import connect, get_version

from live_suite import _plain_component


async def main_async(args: argparse.Namespace) -> int:
    bot = await connect(
        args.host,
        port=args.port,
        username=args.username,
        version=get_version(args.version),
        timeout=30.0,
    )
    seen: list[tuple[str, str]] = []

    def on_chat(sender, name, message, chat_type_id, target_name) -> None:
        who = _plain_component(name).strip() or "(server)"
        text = _plain_component(message).strip()
        if who == args.username:
            return
        seen.append((who, text))
        print(f"[chat] {who}: {text}", flush=True)

    bot.events.on("player_chat", on_chat)
    try:
        await asyncio.sleep(1.5)
        for message in args.messages:
            print(f"[send] {message}", flush=True)
            await bot.send_message(message)
            await asyncio.sleep(args.interval)
        await asyncio.sleep(args.tail)
    finally:
        await bot.close()

    replies = [text for who, text in seen if who == args.suite_user]
    print(f"[result] {len(replies)} 条来自 {args.suite_user} 的回复", flush=True)
    for text in replies:
        print(f"  {text}", flush=True)
    return 0 if replies else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("messages", nargs="*", default=["0"], help="chat lines to send")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--version", default="26.3")
    parser.add_argument("--username", default="Watcher", help="this bot's own name")
    parser.add_argument(
        "--suite-user", default="SuiteBot", help="who runs tools/live_suite.py"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.5,
        help="seconds between messages (the server kicks above its spam budget)",
    )
    parser.add_argument(
        "--tail", type=float, default=8.0, help="seconds to wait for late replies"
    )
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

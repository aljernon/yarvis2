"""Dump recent messages from Anton's private chat to stdout."""

import argparse
import datetime
import pathlib
import sys

# Load .env BEFORE importing yarvis_ptb modules, because storage.py reads
# DATABASE_URL from os.environ at module-import time (not lazily).
import dotenv

dotenv.load_dotenv(pathlib.Path(__file__).parent / ".env", verbose=True)

from yarvis_ptb.prompting import (
    convert_db_messages_to_claude_messages,
    render_mesage_param_exact,
)
from yarvis_ptb.settings.main import DEFAULT_TIMEZONE, load_env
from yarvis_ptb.storage import connect, get_messages

CHAT_ID = 96009555  # Anton's private chat (ROOT_USER_ID == chat_id for private chats)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Dump recent messages from Anton's private chat."
    )
    parser.add_argument(
        "--since",
        "-s",
        type=str,
        default=None,
        help="Start date in ISO format (default: 24h ago)",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=200,
        help="Max messages to fetch from DB (default: 200)",
    )
    parser.add_argument(
        "--max-line-length",
        type=int,
        default=200,
        help="Truncate each output line to this length (default: 200)",
    )
    return parser.parse_args()


def truncate_line(line: str, max_len: int) -> str:
    if max_len > 0 and len(line) > max_len:
        return line[:max_len] + " [...]"
    return line


def main():
    load_env()
    args = parse_args()

    now = datetime.datetime.now(DEFAULT_TIMEZONE)

    if args.since is not None:
        since_dt = datetime.datetime.fromisoformat(args.since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=DEFAULT_TIMEZONE)
    else:
        since_dt = now - datetime.timedelta(hours=24)

    with connect() as conn:
        with conn.cursor() as curr:
            db_messages = get_messages(curr, chat_id=CHAT_ID, limit=args.limit)

    # Filter by date
    filtered = [m for m in db_messages if m.created_at >= since_dt]

    print(
        f"Fetched {len(db_messages)} messages, {len(filtered)} since {since_dt.isoformat()}",
        file=sys.stderr,
    )

    # Convert to Claude MessageParam format and render
    claude_messages = convert_db_messages_to_claude_messages(filtered)

    for msg in claude_messages:
        lines = render_mesage_param_exact(msg)
        for line in lines:
            # Handle multi-line strings within a single rendered line
            for subline in line.splitlines():
                print(truncate_line(subline, args.max_line_length))
        print()  # blank line between messages


if __name__ == "__main__":
    main()

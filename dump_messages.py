"""Dump recent messages from Anton's private chat to stdout.

Examples:
    # Last 24h (default)
    python dump_messages.py

    # Search across all agents (main + archives + subagents)
    python dump_messages.py -q "don't like working"

    # Search within a specific archive agent
    python dump_messages.py -q "leaving Meta" --agent archive/2026-03-25

    # Show 3 messages of context around each search hit
    python dump_messages.py -q "leaving Meta" -C 3

    # List all agents (archives, subagents, etc.)
    python dump_messages.py --list-agents

    # Dump a specific agent's history
    python dump_messages.py --agent archive/2026-03-25

    # Date range
    python dump_messages.py -s 2026-03-25 --until 2026-03-26

    # Full output, no truncation
    python dump_messages.py -q "dinner" --max-line-length 0
"""

import argparse
import datetime
import pathlib
import re
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
from yarvis_ptb.storage import DbMessage, connect, get_agent_by_slug, get_messages

CHAT_ID = 96009555  # Anton's private chat (ROOT_USER_ID == chat_id for private chats)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Dump recent messages from Anton's private chat.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  %(prog)s -q "don't like working"           # search all agents
  %(prog)s -q "dinner" --agent archive/2026-03-25  # search one agent
  %(prog)s -q "leaving Meta" -C 3            # 3 messages context around hits
  %(prog)s --list-agents                      # list all agents
  %(prog)s --agent archive/2026-03-25        # dump an agent's history""",
    )
    parser.add_argument(
        "--since",
        "-s",
        type=str,
        default=None,
        help="Start date in ISO format (default: 24h ago, or unbounded with -q)",
    )
    parser.add_argument(
        "--until",
        "-u",
        type=str,
        default=None,
        help="End date in ISO format",
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
        help="Truncate each output line to this length (0=no limit, default: 200)",
    )
    parser.add_argument(
        "--search",
        "-q",
        type=str,
        default=None,
        help="Search message text and meta (case-insensitive). Searches all agents by default.",
    )
    parser.add_argument(
        "--context",
        "-C",
        type=int,
        default=0,
        help="Show N messages before and after each search hit (like grep -C)",
    )
    parser.add_argument(
        "--agent",
        "-a",
        type=str,
        default=None,
        help="Agent slug to query (e.g. 'archive/2026-03-25'). Default: main chat.",
    )
    parser.add_argument(
        "--list-agents",
        action="store_true",
        help="List all agents and exit.",
    )
    return parser.parse_args()


def truncate_line(line: str, max_len: int) -> str:
    if max_len > 0 and len(line) > max_len:
        return line[:max_len] + " [...]"
    return line


def list_agents(curr):
    """Print all agents with their slug, creation date, and message count."""
    curr.execute(
        """
        SELECT a.id, a.slug, a.created_at,
               COUNT(m.id) as msg_count,
               MIN(m.created_at) as first_msg,
               MAX(m.created_at) as last_msg
        FROM agents a
        LEFT JOIN messages m ON m.agent_id = a.id AND m.is_visible = true
        WHERE a.chat_id = %s
        GROUP BY a.id, a.slug, a.created_at
        ORDER BY a.created_at DESC
        """,
        (CHAT_ID,),
    )
    rows = curr.fetchall()
    print(
        f"{'ID':>5}  {'SLUG':<40}  {'MSGS':>5}  {'CREATED':<20}  RANGE", file=sys.stderr
    )
    print("-" * 110, file=sys.stderr)
    for row in rows:
        aid, slug, created, msg_count, first_msg, last_msg = row
        slug_str = slug or f"(no slug, id={aid})"
        created_str = (
            created.astimezone(DEFAULT_TIMEZONE).strftime("%Y-%m-%d %H:%M")
            if created
            else ""
        )
        if first_msg and last_msg:
            range_str = (
                f"{first_msg.astimezone(DEFAULT_TIMEZONE).strftime('%m-%d %H:%M')} → "
                f"{last_msg.astimezone(DEFAULT_TIMEZONE).strftime('%m-%d %H:%M')}"
            )
        else:
            range_str = ""
        print(
            f"{aid:>5}  {slug_str:<40}  {msg_count:>5}  {created_str:<20}  {range_str}"
        )


def search_messages(
    curr, query: str, agent_id: int | None, since_dt, until_dt, limit: int
) -> list[DbMessage]:
    """Search messages by text content across message and meta fields.

    agent_id=None with no --agent flag means search ALL agents.
    agent_id=0 is a sentinel for "main chat only" (agent_id IS NULL).
    agent_id=N searches that specific agent.
    """
    conditions = ["chat_id = %s", "is_visible = true"]
    params: list = [CHAT_ID]

    # Agent filtering
    if agent_id == 0:
        conditions.append("agent_id IS NULL")
    elif agent_id is not None:
        conditions.append("agent_id = %s")
        params.append(agent_id)
    # else: no agent filter — search all

    # Text search
    conditions.append("(message ILIKE %s OR meta::text ILIKE %s)")
    like_pattern = f"%{query}%"
    params.extend([like_pattern, like_pattern])

    # Date filters
    if since_dt:
        conditions.append("created_at >= %s")
        params.append(since_dt)
    if until_dt:
        conditions.append("created_at <= %s")
        params.append(until_dt)

    params.append(limit)

    sql = f"""
        SELECT created_at, chat_id, user_id, message, meta, id, marked_for_archive, agent_id, is_hidden_auto_message
        FROM messages
        WHERE {' AND '.join(conditions)}
        ORDER BY created_at ASC, id ASC
        LIMIT %s
    """
    curr.execute(sql, params)
    rows = curr.fetchall()
    return [
        DbMessage(
            created_at=row[0].astimezone(DEFAULT_TIMEZONE),
            chat_id=row[1],
            user_id=row[2],
            message=row[3],
            meta=row[4],
            message_id=row[5],
            marked_for_archive=row[6],
            agent_id=row[7],
            is_hidden_auto_message=row[8],
        )
        for row in rows
    ]


def get_context_messages(curr, msg: DbMessage, context_n: int) -> list[DbMessage]:
    """Get N messages before and after a message, within the same agent scope."""
    if msg.agent_id is not None:
        agent_clause = "AND agent_id = %s"
        base_params = [CHAT_ID, msg.agent_id]
    else:
        agent_clause = "AND agent_id IS NULL"
        base_params = [CHAT_ID]

    # Before
    curr.execute(
        f"""
        SELECT created_at, chat_id, user_id, message, meta, id, marked_for_archive, agent_id, is_hidden_auto_message
        FROM messages
        WHERE chat_id = %s {agent_clause} AND is_visible = true
          AND (created_at < %s OR (created_at = %s AND id < %s))
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
        [*base_params, msg.created_at, msg.created_at, msg.message_id, context_n],
    )
    before = [
        DbMessage(
            created_at=r[0].astimezone(DEFAULT_TIMEZONE),
            chat_id=r[1],
            user_id=r[2],
            message=r[3],
            meta=r[4],
            message_id=r[5],
            marked_for_archive=r[6],
            agent_id=r[7],
            is_hidden_auto_message=r[8],
        )
        for r in curr.fetchall()
    ][::-1]

    # After
    curr.execute(
        f"""
        SELECT created_at, chat_id, user_id, message, meta, id, marked_for_archive, agent_id, is_hidden_auto_message
        FROM messages
        WHERE chat_id = %s {agent_clause} AND is_visible = true
          AND (created_at > %s OR (created_at = %s AND id > %s))
        ORDER BY created_at ASC, id ASC
        LIMIT %s
        """,
        [*base_params, msg.created_at, msg.created_at, msg.message_id, context_n],
    )
    after = [
        DbMessage(
            created_at=r[0].astimezone(DEFAULT_TIMEZONE),
            chat_id=r[1],
            user_id=r[2],
            message=r[3],
            meta=r[4],
            message_id=r[5],
            marked_for_archive=r[6],
            agent_id=r[7],
            is_hidden_auto_message=r[8],
        )
        for r in curr.fetchall()
    ]

    return before + [msg] + after


def render_messages(
    db_messages: list[DbMessage], max_line_length: int, highlight: str | None = None
):
    """Render messages to stdout. Optionally highlight search terms."""
    claude_messages, _ = convert_db_messages_to_claude_messages(db_messages)

    for msg in claude_messages:
        lines = render_mesage_param_exact(msg)
        for line in lines:
            for subline in line.splitlines():
                out = truncate_line(subline, max_line_length)
                if highlight:
                    # Case-insensitive highlight with ANSI bold
                    out = re.sub(
                        re.escape(highlight),
                        lambda m: f"\033[1;33m{m.group()}\033[0m",
                        out,
                        flags=re.IGNORECASE,
                    )
                print(out)
        print()


def get_agent_slug_for_id(curr, agent_id: int | None) -> str | None:
    """Look up an agent's slug by ID."""
    if agent_id is None:
        return None
    curr.execute("SELECT slug FROM agents WHERE id = %s", (agent_id,))
    row = curr.fetchone()
    return row[0] if row else None


def main():
    load_env()
    args = parse_args()

    with connect() as conn:
        with conn.cursor() as curr:
            # List agents mode
            if args.list_agents:
                list_agents(curr)
                return

            now = datetime.datetime.now(DEFAULT_TIMEZONE)

            # Parse dates
            since_dt = None
            if args.since is not None:
                since_dt = datetime.datetime.fromisoformat(args.since)
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=DEFAULT_TIMEZONE)
            elif args.search is None and args.agent is None:
                # Default to 24h ago only when not searching and not viewing a specific agent
                since_dt = now - datetime.timedelta(hours=24)

            until_dt = None
            if args.until is not None:
                until_dt = datetime.datetime.fromisoformat(args.until)
                if until_dt.tzinfo is None:
                    until_dt = until_dt.replace(tzinfo=DEFAULT_TIMEZONE)

            # Resolve agent slug to ID
            resolved_agent_id = None  # None = no agent filter (search all)
            if args.agent:
                result = get_agent_by_slug(curr, CHAT_ID, args.agent)
                if result is None:
                    print(
                        f"Agent '{args.agent}' not found. Use --list-agents to see available agents.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                resolved_agent_id = result[0]
            elif args.search is None:
                # No search, no --agent: default to main chat
                resolved_agent_id = 0  # sentinel for agent_id IS NULL

            # Search mode
            if args.search:
                hits = search_messages(
                    curr, args.search, resolved_agent_id, since_dt, until_dt, args.limit
                )
                print(
                    f"Found {len(hits)} messages matching '{args.search}'",
                    file=sys.stderr,
                )

                if args.context > 0:
                    # Show context around each hit, grouped
                    seen_ids: set[int] = set()
                    for hit in hits:
                        context_msgs = get_context_messages(curr, hit, args.context)
                        # Deduplicate across groups
                        new_msgs = [
                            m for m in context_msgs if m.message_id not in seen_ids
                        ]
                        if not new_msgs:
                            continue
                        for m in context_msgs:
                            seen_ids.add(m.message_id)

                        # Show agent slug header
                        slug = get_agent_slug_for_id(curr, hit.agent_id) or "(main)"
                        print(
                            f"\033[1;36m--- [{slug}] id={hit.message_id} at={hit.created_at.isoformat()} ---\033[0m"
                        )
                        render_messages(
                            new_msgs, args.max_line_length, highlight=args.search
                        )
                        print("---\n")
                else:
                    # Group hits by agent for readability
                    current_agent = -999
                    for hit in hits:
                        if hit.agent_id != current_agent:
                            current_agent = hit.agent_id
                            slug = get_agent_slug_for_id(curr, hit.agent_id) or "(main)"
                            print(f"\033[1;36m=== Agent: {slug} ===\033[0m")
                        render_messages(
                            [hit], args.max_line_length, highlight=args.search
                        )
                return

            # Normal dump mode
            if resolved_agent_id == 0:
                db_messages = get_messages(curr, chat_id=CHAT_ID, limit=args.limit)
            else:
                db_messages = get_messages(
                    curr, chat_id=CHAT_ID, limit=args.limit, agent_id=resolved_agent_id
                )

            # Filter by date
            filtered = db_messages
            if since_dt:
                filtered = [m for m in filtered if m.created_at >= since_dt]
            if until_dt:
                filtered = [m for m in filtered if m.created_at <= until_dt]

            print(
                f"Fetched {len(db_messages)} messages, {len(filtered)} after date filtering",
                file=sys.stderr,
            )

            render_messages(filtered, args.max_line_length)


if __name__ == "__main__":
    main()

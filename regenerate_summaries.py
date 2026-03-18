"""Regenerate DAU archive summaries using sonnet for a range of archives."""

import sys
import time

from yarvis_ptb.daily_agent_update import _summarize_messages
from yarvis_ptb.settings import ROOT_USER_ID
from yarvis_ptb.settings.main import load_env
from yarvis_ptb.storage import (
    connect,
    get_dau_sessions,
    get_messages,
    update_agent_meta,
)

load_env()

MIN_SLUG = sys.argv[1] if len(sys.argv) > 1 else "archive-2026-03-10"

chat_id = ROOT_USER_ID

with connect() as conn:
    with conn.cursor() as curr:
        sessions = get_dau_sessions(curr, chat_id)

        targets = [s for s in sessions if s["slug"] >= MIN_SLUG]
        targets.sort(key=lambda s: s["slug"])

        print(f"Regenerating summaries for {len(targets)} archives (>= {MIN_SLUG}):")
        for s in targets:
            print(f"  {s['slug']} (id={s['id']})")

        for s in targets:
            slug = s["slug"]
            agent_id = s["id"]
            old_summary = s["meta"].get("summary", "(none)")

            t0 = time.time()
            msgs = get_messages(curr, chat_id, agent_id=agent_id)
            t_fetch = time.time() - t0
            print(f"\n{'='*60}")
            print(f"{slug}: {len(msgs)} messages (fetched in {t_fetch:.1f}s)")
            print(f"  Old summary: {old_summary[:150]}...")

            t0 = time.time()
            summary = _summarize_messages(msgs)
            t_summarize = time.time() - t0
            if summary:
                update_agent_meta(curr, agent_id, {"summary": summary})
                conn.commit()
                print(f"  New summary ({t_summarize:.1f}s): {summary[:150]}...")
            else:
                print(f"  WARNING: no summary generated ({t_summarize:.1f}s)")

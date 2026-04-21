"""Background reflection on scheduled-subagent runs.

After a `sched/` subagent finishes and the main agent replies to its output,
this module runs a reflection that reviews the whole episode and can:
- tighten the schedule's `context` via `update_schedule`
- save learnings to memory / skills
The reflect runs via the shared `run_reflect_core` pipeline in
daily_self_reflect — only history assembly differs (subagent stack +
rendered main reply vs. ROOT history).
"""

import datetime
import logging
import traceback

from yarvis_ptb.agent_config import AgentMeta
from yarvis_ptb.agent_slugs import schedule_reflect_slug
from yarvis_ptb.complex_chat import COMPLEX_CHAT_LOCK, DEFAULT_AGENT_CONFIG
from yarvis_ptb.daily_self_reflect import get_reflect_lock, run_reflect_core
from yarvis_ptb.debug_chat import add_debug_message_to_queue
from yarvis_ptb.prompting import (
    convert_db_messages_to_claude_messages,
    render_mesage_param_exact,
)
from yarvis_ptb.settings import BOT_USER_ID, DEFAULT_TIMEZONE, SYSTEM_USER_ID
from yarvis_ptb.storage import (
    DbMessage,
    connect,
    get_messages,
    get_schedules,
)

logger = logging.getLogger(__name__)

SCHEDULE_REFLECT_PROMPT = """\
You are running as a sub-agent for automatic self-reflection after a scheduled invocation you just finished. Please read the auto-reflect skill using read_skill tool (name: "auto-reflect") and follow its instructions.

Additionally, consider:
- Could you have executed the task better, or sent your result back in a more useful form?
- Is the schedule context you received clear enough for next time? If not, call `update_schedule` to tighten it.

<main_agent_reply>
{main_reply}
</main_agent_reply>"""


async def _run_schedule_reflect_inner(
    curr,
    chat_id: int,
    subagent_id: int,
    subagent_slug: str,
    subagent_end_time: datetime.datetime,
    application,
    bot,
) -> None:
    label = "Schedule-reflect"
    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    max_history_length_turns = DEFAULT_AGENT_CONFIG.rendering.max_history_length_turns

    logger.info(f"{label}: starting for subagent {subagent_slug}")
    add_debug_message_to_queue(
        f"**{label.upper()}: starting** (subagent: {subagent_slug})"
    )

    async with COMPLEX_CHAT_LOCK:
        subagent_messages = get_messages(curr, chat_id=chat_id, agent_id=subagent_id)
        main_messages = get_messages(curr, chat_id=chat_id, limit=20)
        scheduled_invocations = get_schedules(curr, chat_id)

    # The main-agent reply = the first BotTurn saved after the subagent finished.
    main_reply_msg = next(
        (
            m
            for m in main_messages
            if m.created_at >= subagent_end_time and m.user_id == BOT_USER_ID
        ),
        None,
    )
    if main_reply_msg is None:
        main_reply_rendered = "(main agent did not reply to the subagent)"
    else:
        reply_params, _ = convert_db_messages_to_claude_messages([main_reply_msg])
        reply_lines: list[str] = []
        for mp in reply_params:
            reply_lines.extend(render_mesage_param_exact(mp))
        main_reply_rendered = "\n".join(reply_lines) or "(empty reply)"

    trigger_text = SCHEDULE_REFLECT_PROMPT.format(main_reply=main_reply_rendered)
    reflect_msg = DbMessage(
        chat_id=chat_id,
        created_at=now,
        user_id=SYSTEM_USER_ID,
        message=trigger_text,
    )
    history_snapshot = [*subagent_messages, reflect_msg][-max_history_length_turns:]

    slug = schedule_reflect_slug(subagent_slug)
    await run_reflect_core(
        curr,
        chat_id,
        label=label,
        history_snapshot=history_snapshot,
        trigger_text=trigger_text,
        scheduled_invocations=scheduled_invocations,
        now=now,
        slug=slug,
        agent_meta=AgentMeta(type="schedule_reflect"),
        notification_prefix=f"{label} on subagent `{subagent_slug}` (run by subagent: {slug})",
        notification_meta={"is_reflection": True, "reflected_agent_id": subagent_id},
        application=application,
        bot=bot,
    )


async def schedule_reflect_job(context) -> None:
    """job_queue wrapper so PTB holds a strong reference while we reflect."""
    data = context.job.data
    await run_schedule_reflect(
        chat_id=data["chat_id"],
        subagent_id=data["subagent_id"],
        subagent_slug=data["subagent_slug"],
        subagent_end_time=data["subagent_end_time"],
        application=context.application,
        bot=context.bot,
    )


async def run_schedule_reflect(
    chat_id: int,
    subagent_id: int,
    subagent_slug: str,
    subagent_end_time: datetime.datetime,
    application,
    bot,
) -> None:
    """Background reflection on a scheduled-subagent run.

    The caller awaits the main agent's reply to the subagent before
    dispatching, so the reply is guaranteed to be saved by the time we
    snapshot history. Lock is per-agent so concurrent reflections on
    different subagents (or ROOT) can run in parallel.
    """
    if get_reflect_lock(subagent_id).locked():
        logger.info(
            f"Schedule-reflect: skipping, {subagent_slug} is already being reflected on"
        )
        return

    async with get_reflect_lock(subagent_id):
        try:
            with connect() as conn, conn.cursor() as curr:
                await _run_schedule_reflect_inner(
                    curr,
                    chat_id,
                    subagent_id,
                    subagent_slug,
                    subagent_end_time,
                    application,
                    bot,
                )
        except Exception:
            logger.exception("Schedule-reflect failed")
            add_debug_message_to_queue(
                f"**SCHEDULE-REFLECT FAILED**\n```\n{traceback.format_exc()}\n```"
            )

"""DAU (Disjoint Agent Union) — daily session rotation.

At 2am local time:
1. Create a frozen archive agent (archive-YYYY-MM-DD) for yesterday's messages
2. Reassign yesterday's main-chat messages to the archive agent
3. Run haiku to generate a one-paragraph summary (topics, people, events)
4. Insert a system message into the archive agent marking it frozen
5. Insert a system message in main chat about the new session
6. Trigger a Claude invocation so the new session proactively figures out what to do
"""

import datetime
import json
import logging

from yarvis_ptb.agent_config import AgentConfig, AgentMeta
from yarvis_ptb.agent_slugs import archive_slug
from yarvis_ptb.complex_chat import (
    COMPLEX_CHAT_LOCK,
    DEFAULT_AGENT_CONFIG,
)
from yarvis_ptb.debug_chat import add_debug_message_to_queue
from yarvis_ptb.prompting import render_mesage_param_exact
from yarvis_ptb.ptb_util import get_anthropic_client
from yarvis_ptb.rendering_config import RenderingConfig
from yarvis_ptb.sampling import SamplingConfig
from yarvis_ptb.settings import BOT_USER_ID, DEFAULT_TIMEZONE, SYSTEM_USER_ID
from yarvis_ptb.settings.main import SUBAGENT_MODEL_MAP
from yarvis_ptb.storage import (
    DbMessage,
    DbSchedule,
    Invocation,
    create_agent,
    get_agent_by_slug,
    get_dau_sessions,
    get_messages,
    reassign_messages_to_agent,
    save_message,
    update_agent_meta,
)
from yarvis_ptb.timezones import get_timezone

logger = logging.getLogger(__name__)

DAU_HOUR = 2  # 2am local time


def should_run_dau(curr, chat_id: int) -> bool:
    """Check if the daily agent update should run now.

    Triggers once per day at DAU_HOUR local time if there are main-chat
    messages from yesterday and no archive agent for yesterday exists yet.
    """
    local_tz = get_timezone(complex_chat=True)
    now_local = datetime.datetime.now(local_tz)

    if now_local.hour != DAU_HOUR:
        return False

    yesterday = (now_local - datetime.timedelta(days=1)).date()
    slug = archive_slug(yesterday)

    # Already archived?
    if get_agent_by_slug(curr, chat_id, slug) is not None:
        return False

    # Any main-chat messages in the last 2 days?
    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    day_start = now - datetime.timedelta(days=2)
    msgs = get_messages(curr, chat_id)
    day_msgs = [m for m in msgs if m.created_at >= day_start]
    if not day_msgs:
        return False

    return True


async def run_daily_agent_update(curr, chat_id: int, application, bot) -> None:
    """Execute the DAU: freeze yesterday's session and start a new one."""

    if COMPLEX_CHAT_LOCK.locked():
        logger.info("DAU: skipping, complex chat lock is held")
        return

    async with COMPLEX_CHAT_LOCK:
        local_tz = get_timezone(complex_chat=True)
        now = datetime.datetime.now(DEFAULT_TIMEZONE)
        now_local = datetime.datetime.now(local_tz)
        yesterday = (now_local - datetime.timedelta(days=1)).date()
        slug = archive_slug(yesterday)

        logger.info(f"DAU: archiving {yesterday} as {slug}")
        add_debug_message_to_queue(f"**DAU: archiving {yesterday} as {slug}**")

        # 1. Create the archive agent
        agent_meta = AgentMeta(
            agent_config=AgentConfig(
                rendering=RenderingConfig(
                    prompt_name="anton_private",
                ),
                sampling=SamplingConfig(
                    output_mode="tool_message",
                ),
            ),
            type="dau_session",
            status="frozen",
            date=str(yesterday),
        )
        agent_id = create_agent(
            curr,
            chat_id,
            meta=agent_meta.model_dump(),
            slug=slug,
        )
        logger.info(f"DAU: created agent {slug} (id={agent_id})")

        # 2. Reassign messages from the last 2 days up to now.
        # is_visible + agent_id IS NULL prevents double-reassignment.
        day_start = now - datetime.timedelta(days=2)
        day_end = now
        n_reassigned = reassign_messages_to_agent(
            curr, chat_id, agent_id, date_start=day_start, date_end=day_end
        )
        logger.info(f"DAU: reassigned {n_reassigned} messages to {slug}")

        # 3. Summarize the archived messages with haiku
        archived_msgs = get_messages(curr, chat_id, agent_id=agent_id)
        summary = _summarize_messages(archived_msgs)
        if summary:
            update_agent_meta(curr, agent_id, {"summary": summary})
            logger.info(f"DAU: summary for {slug}: {summary[:200]}")

        # 4. Insert freeze system message into the archive agent
        freeze_msg = (
            f"This agent session ({slug}) is now FROZEN. "
            f"Your conversation history up to this point is preserved and immutable. "
            f"Any future queries to you come from a newer version of yourself — "
            f"these exchanges are ephemeral and will not be added to your history.\n\n"
            f"When answering queries, act as a factual archive — not a chatbot. "
            f"Extract and present the relevant information from your history directly. "
            f"Do not ask follow-up questions or make "
            f"conversational small talk. Just provide the facts. "
            f"You may use tools if needed, but do not use them to look up current "
            f"state (e.g., reading CKR) — the caller can do that itself. "
            f"Your value is the knowledge in your conversation history."
        )
        save_message(
            curr,
            DbMessage(
                created_at=now,
                chat_id=chat_id,
                user_id=SYSTEM_USER_ID,
                message=freeze_msg,
                agent_id=agent_id,
            ),
        )

    # 5-7. Build new-session message and trigger invocation
    await invoke_new_session(curr, chat_id, yesterday, slug, application, bot)
    logger.info(f"DAU: new session invocation complete for {slug}")


async def invoke_new_session(
    curr, chat_id: int, yesterday, slug: str, application, bot
) -> None:
    """Build the new-session system message and trigger a Claude invocation."""
    from yarvis_ptb.complex_chat import process_multi_message_claude_invocation

    sessions = get_dau_sessions(curr, chat_id)
    session_entries = []
    for s in sessions[:10]:  # Show last 10
        s_summary = s["meta"].get("summary", "no summary")
        session_entries.append(
            json.dumps({"agent": s["slug"], "description": s_summary})
        )
    sessions_text = "\n".join(session_entries) if session_entries else "(none)"

    new_session_msg = (
        f"=== New Session ===\n"
        f"Your conversation history up to {yesterday} has been moved to archive "
        f"agent '{slug}'. Your current context is now empty — use "
        f'run_subagent(agent="{slug}", message="...") to query past conversations.\n\n'
        f"Archived sessions (descriptions are LLM-generated summaries):\n"
        f"{sessions_text}\n\n"
        f"Review yesterday's session and decide if there's anything you should "
        f"proactively do: follow up on tasks, update CKR with important info, "
        f"check on commitments, etc."
    )
    initial_db_message = DbMessage(
        chat_id=chat_id,
        created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
        user_id=SYSTEM_USER_ID,
        message=new_session_msg,
    )

    dau_schedule = DbSchedule(
        next_run_at=datetime.datetime.now(DEFAULT_TIMEZONE),
        chat_id=chat_id,
        title="New session: review yesterday and follow up",
        schedule_type="at",
        context=(
            f"Yesterday's conversation was archived as '{slug}'. "
            f"Query it with run_subagent to review what happened. "
            f"Follow up on any pending tasks, commitments, or items that need attention. "
            f"Update CKR if needed. Send a message to Anton only if there's something actionable."
        ),
    )
    await process_multi_message_claude_invocation(
        curr,
        application=application,
        bot=bot,
        chat_id=chat_id,
        agent_config=DEFAULT_AGENT_CONFIG,
        invocation=Invocation(invocation_type="schedule", db_invocation=dau_schedule),
        initial_db_message=initial_db_message,
    )


def _summarize_messages(
    db_messages: list[DbMessage], max_summary_tokens: int = 160_000
) -> str | None:
    """Run haiku to produce a one-paragraph summary of the messages."""
    if not db_messages:
        return None

    # Render messages as text
    lines: list[str] = []
    for msg in db_messages:
        meta = msg.meta
        if msg.user_id == BOT_USER_ID and meta and "message_params" in meta:
            for param in meta["message_params"]:
                lines.extend(render_mesage_param_exact(param))
        elif msg.user_id == SYSTEM_USER_ID:
            lines.append(f"[SYSTEM] {msg.message}")
        else:
            lines.append(f"[USER] {msg.message}")
    conversation_text = "\n".join(lines)

    client = get_anthropic_client()
    haiku_model = SUBAGENT_MODEL_MAP["haiku"]
    max_input_tokens = max_summary_tokens

    prompt_template = (
        "You are creating an index summary of a conversation archive. "
        "This summary will be used by other AI agents to decide whether to query "
        "this archive for information. Maximize keyword/topic coverage so relevant "
        "queries can find this archive. Be token-efficient: use comma-separated "
        "lists, not bullet points.\n\n"
        "Format:\n"
        "# Session Summary\n"
        "One sentence overview.\n\n"
        "**Topics:** comma-separated list of all topics/subjects/themes "
        "(be comprehensive, include minor topics)\n\n"
        "**People:** Name (context), Name (context), ...\n\n"
        "**Events:** comma-separated list of events/appointments/activities\n\n"
        "**Instructions/Preferences:** any user corrections, preferences, or rules established\n\n"
        "**Learnings:** new information learned, insights, discoveries\n\n"
        "Do NOT include action items or forward-looking tasks.\n\n"
        "<conversation>\n{conversation}\n</conversation>"
    )

    # Truncate from the left until we fit within the token budget
    full_prompt = prompt_template.format(conversation=conversation_text)
    messages: list[dict] = [{"role": "user", "content": full_prompt}]
    while (
        client.messages.count_tokens(model=haiku_model, messages=messages).input_tokens
        > max_input_tokens
    ):
        conversation_text = (
            "(truncated) ...\n" + conversation_text[len(conversation_text) // 2 :]
        )
        full_prompt = prompt_template.format(conversation=conversation_text)
        messages = [{"role": "user", "content": full_prompt}]

    max_output_tokens = 1000
    summary = ""
    try:
        for attempt in range(3):
            response = client.messages.create(
                model=haiku_model,
                max_tokens=max_output_tokens,
                messages=messages,
            )
            text_blocks = [b.text for b in response.content if b.type == "text"]
            summary = " ".join(text_blocks).strip()
            if response.stop_reason == "end_turn":
                return summary or None
            logger.info(
                f"DAU: summary hit max_tokens on attempt {attempt + 1}, retrying"
            )
        # Last attempt hit the limit too — return what we got
        return summary or None
    except Exception:
        logger.exception("DAU: haiku summarization failed")
        return None

import asyncio
import datetime
import functools
import logging
import os
import traceback

import httpx
import tenacity
from anthropic.types import MessageParam

from yarvis_ptb.agent_config import AgentConfig, AgentMeta
from yarvis_ptb.agent_slugs import reflect_slug
from yarvis_ptb.complex_chat import (
    COMPLEX_CHAT_LOCK,
    DEFAULT_AGENT_CONFIG,
)
from yarvis_ptb.debug_chat import add_debug_message_to_queue
from yarvis_ptb.message_search import save_message_and_update_index
from yarvis_ptb.on_disk_memory import commit_memory, render_memory_content
from yarvis_ptb.prompt_consts import SYSTEM_PROMPTS
from yarvis_ptb.prompting import (
    build_claude_input,
    convert_db_messages_to_claude_messages,
    render_claude_response_short,
    render_mesage_param_exact,
)
from yarvis_ptb.ptb_util import InterruptionScope
from yarvis_ptb.sampling import NoOpHooks, SamplingConfig
from yarvis_ptb.settings import BOT_USER_ID, DEFAULT_TIMEZONE, SYSTEM_USER_ID
from yarvis_ptb.settings.main import SUBAGENT_MODEL_MAP
from yarvis_ptb.storage import (
    DbMessage,
    Invocation,
    connect,
    create_agent,
    get_messages,
    get_schedules,
    save_message,
)
from yarvis_ptb.timezones import get_timezone
from yarvis_ptb.tool_sampler import (
    MODEL_PRICING,
    _DummyJobQueue,
    cost_breakdown,
    estimate_cost,
    get_tools_for_agent_config,
    process_query,
)
from yarvis_ptb.tools.collect_message_tool import NoOpSendMessageTool

logger = logging.getLogger(__name__)

MODEL = SUBAGENT_MODEL_MAP["opus"]
MAX_CONVERSATION_TOKENS = 150_000

REFLECT_PROMPT = """\
You are performing a periodic self-reflection. Below is your recent conversation history with Anton.

Your task:
1. Read through the conversation carefully
2. Identify any new information worth remembering — facts about Anton, preferences, patterns, decisions, or anything that should persist across conversations
3. Update the workspace files accordingly using the str_replace_editor tool
4. Review the scheduled invocations below. Cancel any that are no longer needed (e.g., reminders for things already addressed). Add new ones if follow-ups are needed.
5. Summarize what you reflected on and what changes (if any) you made

Focus on:
- New facts about Anton's life, preferences, habits, relationships
- Technical decisions or project context worth remembering
- Patterns in how Anton likes to interact
- Any corrections to existing knowledge
- Scheduled invocations that should be cancelled or added

Do NOT:
- Add trivial or temporary information
- Duplicate information already in the knowledge files
- Remove information unless it's clearly wrong

<scheduled_invocations>
{scheduled_invocations}
</scheduled_invocations>

<recent_conversation>
{conversation}
</recent_conversation>
"""


def _estimate_tokens(system: str, messages: list[MessageParam]) -> int:
    """Rough token estimate: ~4 chars per token."""
    total_chars = len(system)
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    total_chars += len(block["text"])
    return total_chars // 4


@tenacity.retry(
    retry=tenacity.retry_if_exception_type((httpx.HTTPStatusError, httpx.HTTPError)),
    wait=tenacity.wait_exponential(multiplier=2, min=2, max=30),
    stop=tenacity.stop_after_attempt(3),
    before_sleep=lambda retry_state: logger.warning(
        f"count_tokens API error, retrying in {retry_state.next_action and retry_state.next_action.sleep}s... "
        f"(Attempt {retry_state.attempt_number})"
    ),
)
def _count_tokens_with_retry(system: str, messages: list[MessageParam]) -> int:
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages/count_tokens",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "system": system,
            "messages": messages,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        logger.warning(
            "count_tokens API returned %s: %s", resp.status_code, resp.text[:500]
        )
    resp.raise_for_status()
    return resp.json()["input_tokens"]


def _count_tokens(system: str, messages: list[MessageParam]) -> int:
    """Count tokens using the Anthropic count_tokens API, with fallback to estimation."""
    try:
        return _count_tokens_with_retry(system, messages)
    except tenacity.RetryError as e:
        logger.warning(
            "count_tokens API failed after retries (%s), falling back to estimation",
            e.last_attempt.exception(),
        )
        return _estimate_tokens(system, messages)


def _trim_conversation_to_token_budget(
    conversation_lines: list[str],
    system: str,
    invocations_text: str,
) -> str:
    """Trim conversation from the beginning to fit within token budget.

    Uses binary search with the count_tokens API for exact measurement.
    """
    full_text = "\n".join(conversation_lines)
    user_message: MessageParam = {
        "role": "user",
        "content": REFLECT_PROMPT.format(
            conversation=full_text, scheduled_invocations=invocations_text
        ),
    }
    total_tokens = _count_tokens(system, [user_message])
    logger.info(f"Reflection: full conversation is {total_tokens} tokens")

    if total_tokens <= MAX_CONVERSATION_TOKENS:
        return full_text

    # Binary search: find how many lines to skip from the start
    lo, hi = 0, len(conversation_lines)
    while lo < hi:
        mid = (lo + hi) // 2
        trimmed = "\n".join(conversation_lines[mid:])
        msg: MessageParam = {
            "role": "user",
            "content": REFLECT_PROMPT.format(
                conversation=trimmed, scheduled_invocations=invocations_text
            ),
        }
        tokens = _count_tokens(system, [msg])
        if tokens > MAX_CONVERSATION_TOKENS:
            lo = mid + 1
        else:
            hi = mid

    result = "\n".join(conversation_lines[lo:])
    logger.info(
        f"Reflection: trimmed to {len(conversation_lines) - lo}/{len(conversation_lines)} lines"
    )
    return result


async def run_force_reflect(
    curr, chat_id: int, bot, max_turns: int | None = None
) -> str:
    messages = get_messages(curr, chat_id, limit=max_turns)
    claude_messages, _ = convert_db_messages_to_claude_messages(messages)

    # Render conversation to readable text
    rendered_lines = []
    for msg in claude_messages:
        rendered_lines.extend(render_mesage_param_exact(msg))

    # Fetch and format scheduled invocations
    scheduled_invocations = get_schedules(curr, chat_id)
    target_tz = get_timezone(complex_chat=True)
    if scheduled_invocations:
        inv_lines = []
        for sched in scheduled_invocations:
            if sched.schedule_type == "at":
                type_desc = "one-time"
            elif sched.schedule_type == "cron":
                type_desc = f'cron "{sched.schedule_spec}"'
            elif sched.schedule_type == "every":
                type_desc = f"every {sched.schedule_spec}"
            else:
                type_desc = sched.schedule_type
            inv_lines.append(
                f"- (id={sched.schedule_id}) {type_desc}; next at {sched.next_run_at.astimezone(target_tz)}; title: '{sched.title}'"
            )
        invocations_text = "\n".join(inv_lines)
    else:
        invocations_text = "No scheduled invocations."

    # Build system prompt with memory
    system = SYSTEM_PROMPTS["anton_private"]
    system += (
        "\n=== Workspace content:\n\n"
        "The following is the current content of the workspace. "
        "All files are on disk and can be modified using str_replace tool or directly via bash.\n\n"
        + render_memory_content()
    )

    # Trim conversation to fit token budget
    conversation_text = _trim_conversation_to_token_budget(
        rendered_lines, system, invocations_text
    )

    # Build the user message
    user_message_text = REFLECT_PROMPT.format(
        conversation=conversation_text,
        scheduled_invocations=invocations_text,
    )
    user_message: MessageParam = {
        "role": "user",
        "content": user_message_text,
    }

    reflect_config = AgentConfig(
        sampling=SamplingConfig(model="opus", tool_subset="all"),
    )
    agent_id = create_agent(
        curr,
        chat_id,
        meta=AgentMeta(agent_config=reflect_config, type="force_reflect").model_dump(),
        slug=reflect_slug(now.date()),
    )

    # Save the prompt we sent to the agent (before query, for crash safety)
    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    save_message(
        curr,
        DbMessage(
            created_at=now,
            chat_id=chat_id,
            user_id=SYSTEM_USER_ID,
            message=user_message_text,
            agent_id=agent_id,
        ),
    )

    tools = get_tools_for_agent_config(reflect_config, curr, chat_id, bot)
    scope = InterruptionScope(chat_id=chat_id, message_id=None)
    result = await process_query(
        system=system,
        messages=[user_message],
        agent_config=reflect_config,
        tools=tools,
        hooks=NoOpHooks(),
        job_queue=_DummyJobQueue(),
        scope=scope,
    )
    msg_params = result.message_params

    # Save bot response with usage to DB under the agent_id
    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    bot_meta_fr: dict = {"message_params": msg_params}
    if result.claude_calls:
        from yarvis_ptb.tool_sampler import MODEL_PRICING, cost_breakdown, estimate_cost

        model_id = reflect_config.sampling.resolve_model_name()
        pricing = MODEL_PRICING.get(model_id)
        bot_meta_fr["usage"] = {
            "model": model_id,
            "calls": [c.to_usage_dict(pricing) for c in result.claude_calls],
            "estimated_cost_usd": estimate_cost(result.claude_calls, model_id),
            "cost_breakdown_usd": cost_breakdown(result.claude_calls, model_id),
        }
    save_message(
        curr,
        DbMessage(
            created_at=now,
            chat_id=chat_id,
            user_id=BOT_USER_ID,
            message="USE_CONTENT_FROM_META",
            meta=bot_meta_fr,
            agent_id=agent_id,
        ),
    )

    # Commit any memory changes
    commit_memory()

    return render_claude_response_short(msg_params)


AUTO_REFLECT_PROMPT = """\
You are running as a sub-agent for automatic self-reflection. \
Please read the auto-reflect skill using read_skill tool (name: "auto-reflect") and follow its instructions."""

AUTO_REFLECT_COOLDOWN_SECS = 3600  # 1 hour
AUTO_REFLECT_IDLE_MIN_SECS = 210  # 3:30
AUTO_REFLECT_IDLE_MAX_SECS = 270  # 4:30
MIN_USER_MESSAGES_FOR_REFLECT = 5
MIN_TOOL_CALLS_FOR_REFLECT = 10


def _get_last_reflect_time(curr, chat_id: int) -> datetime.datetime | None:
    """Last time a reflection on ROOT (main chat) ran, for cooldown gating.

    Scoped to reflected_agent_id IS NULL so schedule-reflects (which target a
    specific subagent) don't suppress the idle auto-reflect cooldown.
    """
    curr.execute(
        """
        SELECT created_at FROM messages
        WHERE chat_id = %s AND agent_id IS NULL AND is_visible = true
          AND meta @> '{"is_reflection": true}'
          AND meta->>'reflected_agent_id' IS NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        (chat_id,),
    )
    row = curr.fetchone()
    return row[0].astimezone(DEFAULT_TIMEZONE) if row else None


async def should_auto_reflect(curr, chat_id: int) -> bool:
    """Check if idle-triggered auto-reflection should run."""
    now = datetime.datetime.now(DEFAULT_TIMEZONE)

    # Check cooldown: at least 1 hour since last reflection
    last_reflect = _get_last_reflect_time(curr, chat_id)
    if last_reflect is not None:
        if (now - last_reflect).total_seconds() < AUTO_REFLECT_COOLDOWN_SECS:
            return False

    # Check idle time: between 3:30 and 4:30 since last user message
    recent_messages = get_messages(curr, chat_id, limit=100)
    if not recent_messages:
        return False

    # Find last user message for idle check
    last_user_msg = None
    for msg in reversed(recent_messages):
        if msg.user_id > 0:
            last_user_msg = msg
            break

    if last_user_msg is None:
        return False

    idle_secs = (now - last_user_msg.created_at).total_seconds()
    if idle_secs < AUTO_REFLECT_IDLE_MIN_SECS or idle_secs > AUTO_REFLECT_IDLE_MAX_SECS:
        return False

    # Count user messages and tool calls since the last reflection.
    # Skip if conversation is too thin.
    user_msg_count = 0
    tool_call_count = 0
    for msg in reversed(recent_messages):
        if msg.meta and msg.meta.get("is_reflection"):
            break
        if msg.user_id > 0:
            user_msg_count += 1
        meta = msg.meta
        if (
            msg.user_id == BOT_USER_ID
            and msg.message == "USE_CONTENT_FROM_META"
            and meta is not None
        ):
            for mp in meta.get("message_params", []):
                if isinstance(mp.get("content"), list):
                    tool_call_count += sum(
                        1
                        for block in mp["content"]
                        if isinstance(block, dict) and block.get("type") == "tool_use"
                    )

    if (
        user_msg_count < MIN_USER_MESSAGES_FOR_REFLECT
        and tool_call_count < MIN_TOOL_CALLS_FOR_REFLECT
    ):
        return False

    return True


async def run_reflect_core(
    curr,
    chat_id: int,
    *,
    label: str,
    history_snapshot: list[DbMessage],
    trigger_text: str,
    scheduled_invocations,
    now: datetime.datetime,
    slug: str,
    agent_meta: AgentMeta,
    notification_prefix: str,
    notification_meta: dict[str, object],
    application,
    bot,
    agent_config: AgentConfig | None = None,
) -> None:
    """Shared reflect pipeline for auto-reflect and schedule-reflect.

    Caller must have already snapshotted history + schedules under
    COMPLEX_CHAT_LOCK and composed the trigger text + full
    history_snapshot. This helper runs the Claude loop without the lock
    (so user messages can be answered concurrently) and re-acquires the
    lock only for the DB writes at the end.

    For schedule-reflect, pass the subagent's agent_config so the
    Anthropic prompt cache is shared with the preceding subagent run.
    """
    if agent_config is None:
        agent_config = DEFAULT_AGENT_CONFIG
    rendering_config = agent_config.rendering

    system, message_params, _ = build_claude_input(
        history_snapshot,
        rendering_config,
        invocation=Invocation(invocation_type="automatic"),
        scheduled_invocations=scheduled_invocations,
        forced_now_date=now,
    )

    scope = InterruptionScope(chat_id=chat_id, message_id=None)
    # Real send_message spec (no Telegram side effect) so the tools array matches
    # the main chat's — using a different spec here forces full cache_creation
    # on every reflect run.
    all_tools = [
        NoOpSendMessageTool(t.spec()) if t.name == "send_message" else t
        for t in get_tools_for_agent_config(agent_config, curr, chat_id, bot)
    ]
    result = await process_query(
        system=system,
        messages=message_params,
        agent_config=agent_config,
        tools=all_tools,
        hooks=NoOpHooks(),
        job_queue=application.job_queue,
        scope=scope,
    )
    result_params = result.message_params

    async with COMPLEX_CHAT_LOCK:
        agent_id = create_agent(curr, chat_id, meta=agent_meta.model_dump(), slug=slug)
        save_message(
            curr,
            DbMessage(
                created_at=now,
                chat_id=chat_id,
                user_id=SYSTEM_USER_ID,
                message=trigger_text,
                agent_id=agent_id,
            ),
        )
        bot_meta: dict = {"message_params": result_params}
        if result.claude_calls:
            pricing = MODEL_PRICING.get(MODEL)
            bot_meta["usage"] = {
                "model": MODEL,
                "calls": [c.to_usage_dict(pricing) for c in result.claude_calls],
                "estimated_cost_usd": estimate_cost(result.claude_calls, MODEL),
                "cost_breakdown_usd": cost_breakdown(result.claude_calls, MODEL),
            }
        save_message(
            curr,
            DbMessage(
                created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
                chat_id=chat_id,
                user_id=BOT_USER_ID,
                message="USE_CONTENT_FROM_META",
                meta=bot_meta,
                agent_id=agent_id,
            ),
        )

        sent_messages = result.agent_messages
        if sent_messages:
            notification = f"{notification_prefix}:\n" + "\n".join(sent_messages)
        else:
            notification = f"{notification_prefix}: no summary returned."
        save_message_and_update_index(
            curr,
            DbMessage(
                created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
                chat_id=chat_id,
                user_id=SYSTEM_USER_ID,
                message=notification,
                meta=notification_meta,
            ),
        )

    commit_memory()

    summary = render_claude_response_short(result_params)
    logger.info(f"{label} completed: {summary[:200]}")
    add_debug_message_to_queue(f"**{label.upper()}: completed**\n{summary}")


async def _run_reflect_inner(
    curr, chat_id: int, application, bot, *, label: str = "Auto-reflect"
) -> None:
    """Idle/midnight auto-reflect on the ROOT main chat."""
    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    max_history_length_turns = DEFAULT_AGENT_CONFIG.rendering.max_history_length_turns

    logger.info(f"{label}: starting")
    add_debug_message_to_queue(f"**{label.upper()}: starting**")

    async with COMPLEX_CHAT_LOCK:
        history = get_messages(curr, chat_id=chat_id, limit=max_history_length_turns)
        scheduled_invocations = get_schedules(curr, chat_id)

    reflect_msg = DbMessage(
        chat_id=chat_id,
        created_at=now,
        user_id=SYSTEM_USER_ID,
        message=AUTO_REFLECT_PROMPT,
    )
    history_snapshot = [*history, reflect_msg][-max_history_length_turns:]

    slug = reflect_slug(now.date())
    await run_reflect_core(
        curr,
        chat_id,
        label=label,
        history_snapshot=history_snapshot,
        trigger_text=AUTO_REFLECT_PROMPT,
        scheduled_invocations=scheduled_invocations,
        now=now,
        slug=slug,
        agent_meta=AgentMeta(type="auto_reflect"),
        notification_prefix=f"{label} (run by subagent: {slug})",
        notification_meta={"is_reflection": True},
        application=application,
        bot=bot,
    )


async def should_midnight_reflect(curr, chat_id: int) -> bool:
    """Check if a midnight end-of-day reflection should run.

    Runs once at midnight if there are user messages since the last reflection.
    """
    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    local_hour = now.astimezone(get_timezone(complex_chat=True)).hour
    if local_hour != 0:
        return False

    # Check if we already reflected since the last user message
    last_reflect = _get_last_reflect_time(curr, chat_id)
    recent_messages = get_messages(curr, chat_id, limit=100)

    last_user_msg = None
    for msg in reversed(recent_messages):
        if msg.user_id > 0:
            last_user_msg = msg
            break

    if last_user_msg is None:
        return False

    # Already reflected after last user message — skip
    if last_reflect is not None and last_reflect > last_user_msg.created_at:
        return False

    return True


@functools.cache
def get_reflect_lock(reflected_agent_id: int | None) -> asyncio.Lock:
    """Per-reflected-agent reflection lock. None identifies ROOT / main chat.

    Prevents two reflections from running on the same agent at once while
    still allowing concurrent reflections on different agents (e.g. idle
    auto-reflect on ROOT alongside a schedule-reflect on a subagent).
    """
    # `@functools.cache` never evicts, so every distinct agent id keeps a
    # dead Lock for the process lifetime. Fine here — Heroku cycles the dyno
    # daily, so the set stays bounded to ~one day of scheduled subagents.
    return asyncio.Lock()


async def auto_reflect_job(context) -> None:
    """job_queue wrapper so PTB holds a strong reference while we reflect."""
    await run_auto_reflect(
        context.job.data["chat_id"], context.application, context.bot
    )


async def run_auto_reflect(chat_id: int, application, bot) -> None:
    """Run self-reflection in the background.

    Opens its own DB connection (the caller's cursor may be released before
    reflection finishes) and peeks at the ROOT reflect lock to skip when
    another reflection on ROOT is already running. COMPLEX_CHAT_LOCK is
    acquired only briefly inside _run_reflect_inner — user messages are
    answered concurrently with the Claude loop.
    """
    if get_reflect_lock(None).locked():
        logger.info("Auto-reflect: skipping, a reflection on ROOT is already running")
        return

    async with get_reflect_lock(None):
        try:
            with connect() as conn, conn.cursor() as curr:
                await _run_reflect_inner(
                    curr, chat_id, application, bot, label="Auto-reflect"
                )
        except Exception:
            logger.exception("Auto-reflect failed")
            add_debug_message_to_queue(
                f"**AUTO-REFLECT FAILED**\n```\n{traceback.format_exc()}\n```"
            )

import datetime
import logging
import os

import httpx
import tenacity
from anthropic.types import MessageParam

from yarvis_ptb.complex_chat import (
    COMPLEX_CHAT_LOCK,
    COMPLEX_CHAT_PUT_CONTEXT_AT_THE_BEGINNING,
    COMPLEX_CHAT_PUT_CONTEXT_AT_THE_END,
    DEFAULT_COMPLEX_CHAT_CONFIG,
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
from yarvis_ptb.settings import BOT_USER_ID, DEFAULT_TIMEZONE, SYSTEM_USER_ID
from yarvis_ptb.settings.main import SUBAGENT_MODEL_MAP
from yarvis_ptb.storage import (
    DbMessage,
    Invocation,
    VariablesForChat,
    create_agent,
    get_messages,
    get_schedules,
    save_message,
)
from yarvis_ptb.timezones import get_timezone
from yarvis_ptb.tool_sampler import process_query, process_subagent_query

logger = logging.getLogger(__name__)

MODEL = SUBAGENT_MODEL_MAP["opus"]
MAX_CONVERSATION_TOKENS = 150_000

REFLECT_PROMPT = """\
You are performing a periodic self-reflection. Below is your recent conversation history with Anton.

Your task:
1. Read through the conversation carefully
2. Identify any new information worth remembering — facts about Anton, preferences, patterns, decisions, or anything that should persist across conversations
3. Update the Core Knowledge Repository files accordingly using the str_replace_editor tool
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
    claude_messages = convert_db_messages_to_claude_messages(messages)

    # Render conversation to readable text
    rendered_lines = []
    for msg in claude_messages:
        rendered_lines.extend(render_mesage_param_exact(msg))

    # Fetch and format scheduled invocations
    scheduled_invocations = get_schedules(curr, chat_id)
    target_tz = get_timezone(True)
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
                f"- (id={sched.schedule_id}) {type_desc}; next at {sched.next_run_at.astimezone(target_tz)}; reason: '{sched.reason}'"
            )
        invocations_text = "\n".join(inv_lines)
    else:
        invocations_text = "No scheduled invocations."

    # Build system prompt with memory
    system = SYSTEM_PROMPTS["anton_private"]
    system += (
        "\n=== Core Knowledge Repository content:\n\n"
        "The following is the current content of the Core Knowledge Repository. "
        "All repository files are on disk and can be modified using str_replace tool or directly via bash.\n\n"
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

    agent_id = create_agent(curr, chat_id, meta={"type": "reflect"})

    # Save the prompt we sent to the agent
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

    msg_params, _claude_calls = await process_subagent_query(
        system=system,
        messages=[user_message],
        tool_names=None,  # All available tools
        chat_id=chat_id,
        agent_id=agent_id,
        curr=curr,
        bot=bot,
        model_name=MODEL,
    )

    # Save to DB under the agent_id
    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    save_message(
        curr,
        DbMessage(
            created_at=now,
            chat_id=chat_id,
            user_id=BOT_USER_ID,
            message="USE_CONTENT_FROM_META",
            meta={"message_params": msg_params},
            agent_id=agent_id,
        ),
    )

    # Commit any memory changes
    commit_memory()

    return render_claude_response_short(msg_params)


AUTO_REFLECT_PROMPT = """\
This is an automatic self-reflection triggered by the system, not a user message. \
Please read the auto-reflect skill using read_memory tool (name: "auto-reflect") and follow its instructions."""

# TODO: Add a daily 3am reflection that bypasses idle window/substance checks
AUTO_REFLECT_COOLDOWN_SECS = 3600  # 1 hour
AUTO_REFLECT_IDLE_MIN_SECS = 210  # 3:30
AUTO_REFLECT_IDLE_MAX_SECS = 270  # 4:30
MIN_USER_MESSAGES_FOR_REFLECT = 5
MIN_TOOL_CALLS_FOR_REFLECT = 10
LAST_AUTO_REFLECT_TIME_VAR = "LAST_AUTO_REFLECT_TIME"


async def should_auto_reflect(curr, chat_id: int) -> bool:
    """Check if auto-reflection should be triggered."""
    now = datetime.datetime.now(DEFAULT_TIMEZONE)

    # Check cooldown: at least 1 hour since last auto-reflection
    chat_vars = VariablesForChat(curr, chat_id)
    last_reflect = chat_vars.get(LAST_AUTO_REFLECT_TIME_VAR)
    if last_reflect is not None:
        since_last_reflect = (now - last_reflect).total_seconds()
        if since_last_reflect < AUTO_REFLECT_COOLDOWN_SECS:
            return False

    # Check idle time: between 3:30 and 4:30 since last user message
    # get_messages returns ASC order, so last element is most recent
    recent_messages = get_messages(curr, chat_id, limit=50)
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

    # Count user messages and tool calls since the last auto-reflect placeholder.
    # Skip reflection if conversation is too thin.
    user_msg_count = 0
    tool_call_count = 0
    for msg in reversed(recent_messages):
        if (
            msg.user_id == BOT_USER_ID
            and msg.message == "USE_CONTENT_FROM_META"
            and msg.meta
            and any(
                "Self-reflection completed" in block.get("text", "")
                for mp in msg.meta.get("message_params", [])
                if isinstance(mp.get("content"), list)
                for block in mp["content"]
                if isinstance(block, dict)
            )
        ):
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


async def run_auto_reflect(curr, chat_id: int, application, bot) -> None:
    """Run automatic self-reflection using the warm cache from recent conversation.

    Unlike run_force_reflect(), this uses the same message-building pipeline as
    process_multi_message_claude_invocation to get a cache hit on the prompt prefix.
    Results are saved under an agent_id (visible on dashboard, not in main history).
    A placeholder assistant message is inserted into main history.
    """
    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    chat_config = DEFAULT_COMPLEX_CHAT_CONFIG

    # Don't block if lock is held (user conversation in progress)
    if COMPLEX_CHAT_LOCK.locked():
        logger.info("Auto-reflect: skipping, complex chat lock is held")
        return

    async with COMPLEX_CHAT_LOCK:
        logger.info("Auto-reflect: starting")
        add_debug_message_to_queue("**AUTO-REFLECT: starting**")

        # 1. Load messages (same as normal invocation)
        max_history_length_turns = chat_config.max_history_length_turns
        db_messages = get_messages(
            curr, chat_id=chat_id, limit=max_history_length_turns
        )

        # 2. Create the reflection trigger as a system message
        reflect_msg = DbMessage(
            chat_id=chat_id,
            created_at=now,
            user_id=SYSTEM_USER_ID,
            message=AUTO_REFLECT_PROMPT,
        )
        db_messages = [*db_messages, reflect_msg][-max_history_length_turns:]

        # 3. Build Claude input (same pipeline = cache hit)
        scheduled_invocations = get_schedules(curr, chat_id)
        system, message_params = build_claude_input(
            db_messages,
            chat_config,
            invocation=Invocation(invocation_type="schedule"),  # no special type needed
            put_context_at_the_beginning=COMPLEX_CHAT_PUT_CONTEXT_AT_THE_BEGINNING,
            put_context_at_the_end=COMPLEX_CHAT_PUT_CONTEXT_AT_THE_END,
            scheduled_invocations=scheduled_invocations,
            forced_now_date=now,
        )

        # 4. Call process_query with full tool access
        scope = InterruptionScope(chat_id=chat_id, message_id=None)
        (
            result_params,
            claude_calls,
            tool_init_time,
            subagent_usages,
        ) = await process_query(
            curr=curr,
            bot=bot,
            chat_config=chat_config,
            chat_id=chat_id,
            system=system,
            messages=message_params,
            on_update=None,
            scope=scope,
            job_queue=application.job_queue,
        )

        # 5. Save trigger + bot response under agent
        agent_id = create_agent(curr, chat_id, meta={"type": "auto_reflect"})

        save_message(
            curr,
            DbMessage(
                created_at=now,
                chat_id=chat_id,
                user_id=SYSTEM_USER_ID,
                message=AUTO_REFLECT_PROMPT,
                agent_id=agent_id,
            ),
        )
        save_message(
            curr,
            DbMessage(
                created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
                chat_id=chat_id,
                user_id=BOT_USER_ID,
                message="USE_CONTENT_FROM_META",
                meta={"message_params": result_params},
                agent_id=agent_id,
            ),
        )

        # 6. Save trigger + placeholder to main history
        save_message_and_update_index(
            curr,
            DbMessage(
                created_at=now,
                chat_id=chat_id,
                user_id=SYSTEM_USER_ID,
                message=AUTO_REFLECT_PROMPT,
            ),
        )
        placeholder_params: list[MessageParam] = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "[Self-reflection completed; output omitted from history]",
                    }
                ],
            }
        ]
        save_message_and_update_index(
            curr,
            DbMessage(
                chat_id=chat_id,
                created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
                user_id=BOT_USER_ID,
                message="USE_CONTENT_FROM_META",
                meta={"message_params": placeholder_params},
            ),
        )

        # 7. Update last auto-reflect time
        chat_vars = VariablesForChat(curr, chat_id)
        chat_vars.set(
            LAST_AUTO_REFLECT_TIME_VAR, datetime.datetime.now(DEFAULT_TIMEZONE)
        )

        # 8. Commit memory changes
        commit_memory()

        summary = render_claude_response_short(result_params)
        logger.info(f"Auto-reflect completed: {summary[:200]}")
        add_debug_message_to_queue(f"**AUTO-REFLECT: completed**\n{summary}")

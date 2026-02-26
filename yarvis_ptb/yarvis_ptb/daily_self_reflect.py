import datetime
import logging
import os

import httpx
from anthropic.types import MessageParam

from yarvis_ptb.on_disk_memory import commit_memory, render_memory_content
from yarvis_ptb.prompt_consts import SYSTEM_PROMPTS
from yarvis_ptb.prompting import (
    convert_db_messages_to_claude_messages,
    render_claude_response_short,
    render_mesage_param_exact,
)
from yarvis_ptb.settings import BOT_USER_ID, DEFAULT_TIMEZONE
from yarvis_ptb.settings.main import SUBAGENT_MODEL_MAP
from yarvis_ptb.storage import (
    DbMessage,
    create_agent,
    get_messages,
    get_scheduled_invocations,
    save_message,
)
from yarvis_ptb.timezones import get_timezone
from yarvis_ptb.tool_sampler import process_subagent_query

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


def _count_tokens(system: str, messages: list[MessageParam]) -> int:
    """Count tokens using the Anthropic count_tokens API."""
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
    resp.raise_for_status()
    return resp.json()["input_tokens"]


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


async def run_reflect(curr, chat_id: int, bot, max_turns: int | None = None) -> str:
    messages = get_messages(curr, chat_id, limit=max_turns)
    claude_messages = convert_db_messages_to_claude_messages(messages)

    # Render conversation to readable text
    rendered_lines = []
    for msg in claude_messages:
        rendered_lines.extend(render_mesage_param_exact(msg))

    # Fetch and format scheduled invocations
    scheduled_invocations = get_scheduled_invocations(curr, chat_id)
    target_tz = get_timezone(True)
    if scheduled_invocations:
        inv_lines = []
        for inv in scheduled_invocations:
            recur = "recurring daily" if inv.is_recurring else "one-time"
            inv_lines.append(
                f"- (id={inv.scheduled_id}) {recur}; next at {inv.scheduled_at.astimezone(target_tz)}; reason: '{inv.reason}'"
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
    user_message: MessageParam = {
        "role": "user",
        "content": REFLECT_PROMPT.format(
            conversation=conversation_text,
            scheduled_invocations=invocations_text,
        ),
    }

    agent_id = create_agent(curr, chat_id, meta={"type": "reflect"})

    msg_params = await process_subagent_query(
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

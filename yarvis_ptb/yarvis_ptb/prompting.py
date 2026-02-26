import copy
import datetime
import difflib
import json
import logging
import re
from collections import namedtuple
from typing import Iterable, Literal, TypedDict, Union

import pytz
from anthropic.types import (
    ContentBlock,
    DocumentBlockParam,
    ImageBlockParam,
    MessageParam,
    TextBlockParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)
from typing_extensions import Required

from yarvis_ptb.chat_config import ChatConfig
from yarvis_ptb.on_disk_memory import render_memory_content
from yarvis_ptb.prompt_consts import SYSTEM_PROMPTS
from yarvis_ptb.settings import (
    BOT_USER_ID,
    DEFAULT_TIMEZONE,
    HISTORY_LENGTH_LONG_SHRINKING_FACTOR,
    HISTORY_LENGTH_LONG_TOKENS,
    LARGE_MESSAGE_SIZE_THRESHOLD,
    SYSTEM_USER_ID,
    TOOL_CALL_USER_ID,
    USER_ID_MAP,
)
from yarvis_ptb.storage import (
    IMAGE_B64_META_FIELD,
    DbMessage,
    DbScheduledInvocation,
    Invocation,
    MemoryType,
)
from yarvis_ptb.timezones import get_timezone

logger = logging.getLogger(__name__)


class NormalizedMessageParam(TypedDict, total=False):
    content: Required[
        Iterable[
            Union[
                TextBlockParam,
                ImageBlockParam,
                ToolUseBlockParam,
                ToolResultBlockParam,
                DocumentBlockParam,
                ContentBlock,
            ]
        ]
    ]

    role: Required[Literal["user", "assistant"]]


COMPLEX_ANTON_PROMPT = "anton_private"

ApiMsgAnnotation = namedtuple("ApiMsgAnnotation", ["db_msg_id", "turn_idx"])


def normalize_message_param(message: MessageParam) -> NormalizedMessageParam:
    content = message["content"]
    if isinstance(content, str):
        content = [TextBlockParam(type="text", text=content)]
    return {"role": message["role"], "content": content}


def build_memory_str(memories: list[MemoryType]) -> str:
    # Build Core Knowledge Repository string from memory items
    memories_copy = [dict(m) for m in memories]
    for m in memories_copy:
        m.pop("chat_id", None)
    chunks = ["<memory>"] + [json.dumps(m) for m in memories_copy] + ["</memory>"]
    return "\n".join(chunks)


def build_context_info(
    *,
    invocation: Invocation | None,
    scheduled_invocations: list[DbScheduledInvocation] | None,
    chat_config: ChatConfig,
    forced_now_date: datetime.datetime | None = None,
) -> str:
    # Build Dynamic Context information that changes with each message
    system_parts = []
    target_tz = get_timezone(chat_config.is_complex_chat)
    if forced_now_date is not None:
        now = forced_now_date.astimezone(target_tz)
    else:
        now_utc = datetime.datetime.now(pytz.UTC)  # Get current time in UTC
        now = now_utc.astimezone(target_tz)  # Convert to target timezone
    day_name = now.strftime("%A")
    tz_name = str(target_tz)  # Get timezone name
    system_parts.append(
        f"<datetime>{now.isoformat()} ({day_name} {tz_name})</datetime>"
    )
    if invocation is not None:
        invocation_dict = dict(invocation_type=invocation.invocation_type)
        if invocation.db_invocation is not None:
            invocation_dict["scheduled_at"] = (
                invocation.db_invocation.scheduled_at.astimezone(target_tz).isoformat()
            )
            invocation_dict["reason"] = invocation.db_invocation.reason
        system_parts.append(f"<invocation>{invocation_dict}</invocation>")
    if chat_config.is_complex_chat:
        constants = {
            "max_history_length_turns": chat_config.max_history_length_turns,
            "HISTORY_LENGTH_LONG_TOKENS": HISTORY_LENGTH_LONG_TOKENS,
            "HISTORY_LENGTH_LONG_SHRINKING_FACTOR": HISTORY_LENGTH_LONG_SHRINKING_FACTOR,
            "LARGE_MESSAGE_SIZE_THRESHOLD": LARGE_MESSAGE_SIZE_THRESHOLD,
        }
        if chat_config.tool_result_truncation_after_n_turns is not None:
            constants["tool_result_truncation_after_n_turns"] = (
                chat_config.tool_result_truncation_after_n_turns
            )
        system_parts.append(f"<constants>{json.dumps(constants)}</constants>")

    if scheduled_invocations is not None:
        str_chunks = []
        if not scheduled_invocations:
            str_chunks.append("No scheduled invocations.")
        else:
            for inv in scheduled_invocations:
                if inv.is_recurring:
                    str_chunks.append(
                        f"(scheduled_id={inv.scheduled_id}) Scheduled daily; next at {inv.scheduled_at.astimezone(target_tz)}; reason: '{inv.reason}'"
                    )
                else:
                    str_chunks.append(
                        f"(scheduled_id={inv.scheduled_id}) Scheduled at {inv.scheduled_at.astimezone(target_tz)}; reason: '{inv.reason}'"
                    )

        scheduled_invocations_str = "\n".join(str_chunks)
        system_parts.append(
            f"<scheduled_invocations>\n{scheduled_invocations_str}\n</scheduled_invocations>"
        )
    return "<context>\n%s\n</context>" % "\n".join(system_parts)


def convert_db_messages_to_claude_messages(
    messages: list[DbMessage],
    tool_result_truncation_after_n_turns: int | None = None,
) -> list[MessageParam]:
    all_role_messages: list[MessageParam] = []
    api_msg_annotations: list[ApiMsgAnnotation] = []

    messages = copy.deepcopy(messages)

    for turn_idx, msg in enumerate(messages):
        role_messages: list[MessageParam] = []
        if msg.user_id == BOT_USER_ID:
            if msg.meta and "message_params" in msg.meta:
                role_messages.extend(msg.meta["message_params"])
            else:
                role_messages.append({"role": "assistant", "content": msg.message})
        elif msg.user_id == TOOL_CALL_USER_ID:
            # OLD, not used anymore
            pass
        elif msg.user_id == SYSTEM_USER_ID:
            full_message = f"<system>System message created at {msg.created_at.isoformat()}: {msg.message}</system>"
            role_messages.append({"role": "user", "content": full_message})
        else:
            meta = msg.meta or {}
            content_chunks = []

            if b64_image := meta.get(IMAGE_B64_META_FIELD):
                content_chunks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64_image,
                        },
                    }
                )

            is_voice_message = meta.get("is_voice", False)
            full_message = f"<system>Sent by {USER_ID_MAP.get(msg.user_id, f'unknown user ({msg.user_id})')} at {msg.created_at.isoformat()} {is_voice_message=}</system>\n{msg.message}"
            content_chunks.append({"type": "text", "text": full_message})

            role_messages.append({"role": "user", "content": content_chunks})
        if (
            role_messages
            and not role_messages[-1]["content"]
            and role_messages[-1]["role"] == "assistant"
        ):
            logger.debug(f"Empty message: {role_messages[-2:]}")
            del role_messages[-1]

        if msg.marked_for_archive:
            for rm in role_messages:
                if isinstance(rm["content"], str):
                    rm["content"] = "[MARKED_FOR_DELETION] " + rm["content"]
                else:
                    assert isinstance(role_messages[0]["content"], list)
                    if rm["content"] and rm["content"][0]["type"] == "text":
                        rm["content"][0]["text"] = (
                            "[MARKED_FOR_DELETION] " + rm["content"][0]["text"]
                        )

        for _ in role_messages:
            api_msg_annotations.append(
                ApiMsgAnnotation(db_msg_id=msg.message_id, turn_idx=turn_idx)
            )
        all_role_messages.extend(role_messages)

    if tool_result_truncation_after_n_turns is not None:
        apply_tool_call_compactification(
            all_role_messages,
            api_msg_annotations,
            total_turns=len(messages),
            truncation_after_n_turns=tool_result_truncation_after_n_turns,
        )

    return all_role_messages


def _get_tool_result_content_size(content) -> int:
    """Get the byte size of a tool_result block's content."""
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    if isinstance(content, list):
        total = 0
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    total += len(item.get("text", "").encode("utf-8"))
                else:
                    total += len(json.dumps(item).encode("utf-8"))
            else:
                total += len(str(item).encode("utf-8"))
        return total
    return len(json.dumps(content).encode("utf-8"))


def apply_tool_call_compactification(
    api_messages: list[MessageParam],
    api_msg_annotations: list[ApiMsgAnnotation],
    total_turns: int,
    truncation_after_n_turns: int,
) -> None:
    """Truncate large tool results in old turns in-place.

    For turns older than truncation_after_n_turns from the end, find tool_result
    blocks with content >= 10k bytes and replace them with a truncation notice.
    Skip tool results for send_message calls.
    """
    TRUNCATION_THRESHOLD_BYTES = 10_000

    # Build a map from tool_use_id to (tool_name, api_message_index) for quick lookup
    tool_use_map: dict[str, str] = {}  # tool_use_id -> tool_name
    for msg in api_messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_use_map[block["id"]] = block["name"]

    for i, msg in enumerate(api_messages):
        annotation = api_msg_annotations[i]
        turns_from_end = total_turns - 1 - annotation.turn_idx
        if turns_from_end <= truncation_after_n_turns:
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            continue

        # Track tool_result index within this message's content
        tool_result_idx = 0
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue

            tool_use_id = block.get("tool_use_id", "")
            tool_name = tool_use_map.get(tool_use_id, "")

            # Don't truncate send_message results
            if tool_name == "send_message":
                tool_result_idx += 1
                continue

            original_content = block.get("content", "")
            original_size = _get_tool_result_content_size(original_content)

            if original_size >= TRUNCATION_THRESHOLD_BYTES:
                msg_id = annotation.db_msg_id
                block["content"] = [
                    {
                        "type": "text",
                        "text": f"Tool output truncated ({original_size} bytes). Use get_tool_output(msg_id={msg_id}, tool_index={tool_result_idx}) to retrieve.",
                    }
                ]

            tool_result_idx += 1


def build_claude_input(
    messages: list[DbMessage],
    chat_config: ChatConfig,
    *,
    put_context_at_the_end: bool,
    put_context_at_the_beginning: bool,
    invocation: Invocation | None = None,
    scheduled_invocations: list[DbScheduledInvocation] | None = None,
    forced_now_date: datetime.datetime | None = None,
) -> tuple[str, list[MessageParam]]:
    # Build input with system prompt (containing Core Knowledge Repository) and Dynamic Context
    system = SYSTEM_PROMPTS[chat_config.prompt_name]
    context_info = build_context_info(
        invocation=invocation,
        scheduled_invocations=scheduled_invocations,
        chat_config=chat_config,
        forced_now_date=forced_now_date,
    )
    assert put_context_at_the_end or put_context_at_the_beginning, "No context? Really?"
    if put_context_at_the_end:
        context_message = DbMessage(
            created_at=datetime.datetime.now(DEFAULT_TIMEZONE)
            if forced_now_date is None
            else forced_now_date,
            chat_id=-1,  # Not used.
            user_id=SYSTEM_USER_ID,
            message=context_info,
        )
        messages = messages + [context_message]

    if chat_config.memory_access:
        system = f"{system}\n=== Core Knowledge Repository content:\n\nThe following is the current content of the Core Knowledge Repository. All repository files are on disk and can be modified using str_replace tool or directly via bash.\n\n{render_memory_content()}"
        if put_context_at_the_beginning:
            system = f"{system}\n{context_info}"
    history = convert_db_messages_to_claude_messages(
        messages,
        tool_result_truncation_after_n_turns=chat_config.tool_result_truncation_after_n_turns,
    )
    return system, history


def render_mesage_param_exact(rec: MessageParam) -> list[str]:
    formatted = []
    content = copy.deepcopy(rec["content"])
    meta = {k: v for k, v in rec.items() if k != "content"}
    formatted.append(f"## {meta}")
    if isinstance(content, str):
        formatted.append(content)
    else:
        for section in content:
            section = dict(section)
            if section.get("type") in ("thinking", "redacted_thinking"):
                formatted.append(f"**[{section.get('type')}]**")
                continue
            section_content = section.pop("text", None)
            if section_content is None:
                section_content = section.pop("content", None)
            if section_content is None:
                section_content = section.pop("input", None)
                if section_content:
                    section_content = json.dumps(section_content, indent=2)
            formatted.append(f"**{section}**")
            if section_content:
                formatted.append(str(section_content) + "\n")
    return formatted


def render_claude_response_short(
    mesages: list[MessageParam], remove_thinking: bool = True
) -> str:
    tool_calls = []
    tool_results = []
    chunks = []
    for message in mesages:
        if isinstance(message["content"], str):
            chunks.append(message["content"])
        else:
            for content in message["content"]:
                if content["type"] == "text":
                    chunks.append(content["text"])
                elif content["type"] == "tool_use":
                    tool_calls.append(content)
                elif content["type"] == "tool_result":
                    tool_results.append(content)
                    err_tag = "[E]" if tool_results[-1]["is_error"] else "[S]"
                    chunks.append(f"{err_tag}{tool_calls[-1]['name']}")
                elif content["type"] in ("thinking", "redacted_thinking"):
                    pass
                else:
                    logger.error(f"Unknown content type: {content}")
                    chunks.append(content)
    if remove_thinking:
        text = "\n".join(chunks)
        chunks = re.split(r"(<thinking>.*?</thinking>)", text, flags=re.DOTALL)
        chunks = [
            x.strip()
            for x in chunks
            if not x.startswith("<thinking>") or not x.endswith("</thinking>")
        ]
    text = "\n".join(chunks)
    text = re.sub("\n\n+", "\n\n", text)
    return text


def render_claude_response_verbose(
    mesages: list[MessageParam], skip_first_n: int | None = None
) -> list[str]:
    chunks = []
    last_tool_call: ToolUseBlockParam | None = None
    for i, message in enumerate(mesages):
        if isinstance(message["content"], str):
            chunks.append("**ASSISTANT**\n" + str(message))
        else:
            content: TextBlockParam | ToolUseBlockParam | ToolResultBlockParam
            for content in message["content"]:  # type: ignore
                if content["type"] == "text":
                    chunks.append(content["text"])
                elif content["type"] == "tool_use":
                    last_tool_call = content
                    chunks.append(format_tool_call_verbose(last_tool_call))
                elif content["type"] == "tool_result":
                    status = (
                        "**SUCCESS**" if not content.get("is_error") else "**ERROR**"
                    )
                    result = content.get("content", "")
                    if isinstance(result, str):
                        result_str = result
                    else:
                        result_str = "\n".join(
                            x["text"] if x["type"] == "text" else "[IMAGE]"
                            for x in result
                        )
                    try:
                        result_dict = json.loads(result_str)
                    except ValueError:
                        pass
                    else:
                        assert last_tool_call is not None, content
                        result_str = format_tool_result_verbose(
                            last_tool_call, result_dict
                        )

                    chunks.append(f"{status}\n```\n{result_str}\n```")

                elif content["type"] in ("thinking", "redacted_thinking"):
                    pass
                else:
                    logger.error(f"Unknown content type: {content}")
                    chunks.append(content)
        if skip_first_n is not None and i < skip_first_n:
            # when skip first is set, we only use the initial messages to get
            # last_tool_call.
            chunks.clear()
    return chunks


def render_diff(fname: str, old_content: str, new_content: str) -> str:
    """
    Render two multi-line strings as a diff with custom file labels.

    Args:
        old_fname (str): Name of the old file
        new_fname (str): Name of the new file
        old_content (str): Content of the old file
        new_content (str): Content of the new file

    Returns:
        str: A formatted diff output with + and - symbols
    """
    # Split the input strings into lines
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()

    # Create a unified diff
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"OLD:{fname}",
        tofile=f"NEW:{fname}",
        lineterm="",
    )

    # Join the diff lines and return
    return "\n".join(diff)


def format_tool_call_verbose(tool_call: ToolUseBlockParam) -> str:
    args: dict
    if isinstance(tool_call["input"], dict):
        args = tool_call["input"]
    else:
        args = {"cmd": tool_call["input"]}
    try:
        match tool_call["name"]:
            case "bash_run":
                return f'```bash\n{args["code"]}\n```'
            case "python_repl":
                return f'```python\n{args["code"]}\n```'
            case "str_replace_editor":
                if args.get("command") == "str_replace":
                    diff_content = render_diff(
                        args["path"], args["old_str"], args["new_str"]
                    )
                    return f"```diff\n{diff_content}\n```"
    except KeyError as e:
        logger.error(f"WTF {e} {tool_call=} {args=}")
        return f"WTF {e} {tool_call=} {args=}"
    formatted_args = "\n".join([f"{k}={v}" for k, v in args.items()])
    return f"```\n{tool_call['name']}:\n{formatted_args}\n```"


def format_tool_result_verbose(tool_call: ToolUseBlockParam, result: dict) -> str:
    match tool_call["name"]:
        case "bash_run" | "python_repl":
            if not (result["stdout"]) or not (result["stderr"]):
                return (result["stdout"] + result["stderr"]).strip()
            else:
                return f"--- STDOUT:\n{result['stdout']}\n\n--- STDERR:\n{result['stderr']}"
    return json.dumps(result, indent=2)

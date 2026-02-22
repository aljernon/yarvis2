import asyncio
import copy
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

import telegram
import tenacity
from anthropic import Anthropic, APIStatusError, RateLimitError
from anthropic.types import (
    ContentBlock,
    MessageParam,
    TextBlock,
    TextBlockParam,
    ToolChoiceAutoParam,
    ToolResultBlockParam,
    ToolUseBlock,
    ToolUseBlockParam,
)
from anthropic.types.beta import BetaTextBlock, BetaToolUseBlock
from telegram.ext import ContextTypes, JobQueue

from yarvis_ptb.chat_config import ChatConfig
from yarvis_ptb.debug_chat import add_debug_message_to_queue
from yarvis_ptb.prompt_consts import INTERRUPTION_MESSAGE, OVERLOAD_MESSAGE_TPL
from yarvis_ptb.prompting import NormalizedMessageParam, normalize_message_param
from yarvis_ptb.ptb_util import (
    InterruptionScope,
    get_async_anthropic_client,
)
from yarvis_ptb.settings import FULL_LOG_CHAT_ID
from yarvis_ptb.settings.main import CLAUDE_MODEL_NAME
from yarvis_ptb.tools.bash_repl import BashRunTool
from yarvis_ptb.tools.editor_tool import EditorTool
from yarvis_ptb.tools.file_tools import build_chat_send_file_tools
from yarvis_ptb.tools.gcal_tools import get_calendar_tools
from yarvis_ptb.tools.gkeep_tools import get_gkeep_tools
from yarvis_ptb.tools.gmail_tool import get_gmail_tools
from yarvis_ptb.tools.image_tools import build_image_tools
from yarvis_ptb.tools.location import GetLocationTool
from yarvis_ptb.tools.mcp_util import MCPClientBase
from yarvis_ptb.tools.memory_tools import build_memory_tools
from yarvis_ptb.tools.message_search_tool import build_message_search_tools
from yarvis_ptb.tools.message_tool import build_message_tools
from yarvis_ptb.tools.python_repl import PythonREPLTool
from yarvis_ptb.tools.scheduling_tools import build_scheduling_tools
from yarvis_ptb.tools.telegram_tools import get_telegram_tools
from yarvis_ptb.tools.tool_output import GetToolOutputTool
from yarvis_ptb.tools.tool_spec import ClaudeTool, LocalTool, ToolResult

logger = logging.getLogger(__name__)

GENERIC_LOCAL_TOOLS: list[LocalTool] = [
    PythonREPLTool(),
    BashRunTool(),
    EditorTool(),
]

TELEGRAM_TOOLS: list[LocalTool] = get_telegram_tools()

ANTON_DATA_TOOLS: list[LocalTool] = [GetLocationTool()] + (
    get_calendar_tools() + get_gmail_tools() + get_gkeep_tools()
)


MCP_CLIENTS: list[MCPClientBase] = []


ANTHROPIC_EXCEPTIONS_TO_RETRY: tuple[type[APIStatusError], ...] = (APIStatusError,)


@dataclass
class ClaudeCallInfo:
    num_prompt_tokens: int
    num_cached_tokens: int
    seconds_till_start: float | None
    tool_execution_time: float | None


@dataclass
class PartialSample:
    content: list[ContentBlock]
    stop_reason: Literal["tool_use", "eot", "interrupt"]
    num_prompt_tokens: int
    num_cached_tokens: int
    seconds_till_start: float | None


def get_tools_for_config(
    chat_config: ChatConfig, curr, chat_id, bot
) -> list[LocalTool]:
    tool_classes = []
    if chat_config.tool_filter == "none":
        pass
    elif chat_config.tool_filter == "basic":
        tool_classes = ["fs", "chat_send_file"]
    elif chat_config.tool_filter == "logseq":
        tool_classes = ["anton_google", "fs", "chat_send_file"]

        # Add messaging tool only when tool_only_messaging is enabled
        # This tool requires "all" tool filter
        if chat_config.tool_only_messaging:
            tool_classes.append("messaging")
    elif chat_config.tool_filter == "all":
        tool_classes = [
            "anton_google",
            "fs",
            "scheduling",
            "anton_message_search",
            "chat_send_file",
            "telegram",
            "image_editing",
            "memory",
        ]

        # Add messaging tool only when tool_only_messaging is enabled
        # This tool requires "all" tool filter
        if chat_config.tool_only_messaging:
            tool_classes.append("messaging")

        if chat_config.tool_result_truncation_after_n_turns is not None:
            tool_classes.append("tool_output")
    else:
        raise ValueError(f"Unknown tool_filter: {chat_config.tool_filter}")
    all_local_tool_objects = []
    for tool_class in tool_classes:
        if tool_class == "anton_google":
            all_local_tool_objects.extend(ANTON_DATA_TOOLS)
        elif tool_class == "fs":
            all_local_tool_objects.extend(GENERIC_LOCAL_TOOLS)
        elif tool_class == "scheduling":
            all_local_tool_objects.extend(build_scheduling_tools(curr, chat_id))
        elif tool_class == "anton_message_search":
            all_local_tool_objects.extend(build_message_search_tools(chat_id))
        elif tool_class == "chat_send_file":
            debug_chat_id = FULL_LOG_CHAT_ID if chat_config.is_complex_chat else None
            all_local_tool_objects.extend(
                build_chat_send_file_tools(chat_id, bot, debug_chat_id=debug_chat_id)
            )
        elif tool_class == "telegram":
            all_local_tool_objects.extend(TELEGRAM_TOOLS)
        elif tool_class == "messaging":
            all_local_tool_objects.extend(build_message_tools(bot, chat_id, curr))
        elif tool_class == "image_editing":
            all_local_tool_objects.extend(build_image_tools(chat_id, curr))
        elif tool_class == "memory":
            all_local_tool_objects.extend(build_memory_tools())
        elif tool_class == "tool_output":
            all_local_tool_objects.append(GetToolOutputTool(curr))
        else:
            raise ValueError(f"Unknown tool class: {tool_class}")
    return all_local_tool_objects


async def dummy_on_update(messages: list[MessageParam]) -> None:
    pass


async def process_query(
    anthropic: Anthropic,
    curr: Any,
    bot: telegram.Bot,
    chat_config: ChatConfig,
    chat_id: int,
    system: str,
    messages: list[MessageParam],
    job_queue,
    scope: InterruptionScope,
    on_update: Callable[[list[MessageParam]], Awaitable[Any]] | None = None,
) -> tuple[list[MessageParam], list[ClaudeCallInfo] | None, float]:
    """Process a query using Claude and available tools.

    Returns:
        Tuple of (message_params, claude_calls)
        where claude_calls contains num input tokens and time till first token
        for all claude calls - initial and after each tool.
    """

    start = time.monotonic()
    all_local_tool_objects = get_tools_for_config(chat_config, curr, chat_id, bot)
    local_tools: list[ClaudeTool] = [
        tool.spec().to_claude_tool() for tool in all_local_tool_objects
    ]
    async with AsyncExitStack() as stack:
        for local_tool in all_local_tool_objects:
            if scope.is_interrupted:
                logger.warning("Interruption before generation started")
                return [], None, 0.0
            await stack.enter_async_context(local_tool.context())
        remote_tools = []
        for mcp_client in MCP_CLIENTS:
            if scope.is_interrupted:
                logger.warning("Interruption before generation started")
                return [], None, 0.0
            await stack.enter_async_context(mcp_client.context())
            remote_tools.extend(await mcp_client.list_tools())
        if scope.is_interrupted:
            logger.warning("Interruption before generation started")
            return [], None, 0.0
        tool_init_time = time.monotonic() - start
        msg, claude_calls = await _process_query_with_tools(
            anthropic,
            system,
            messages,
            claude_tools=local_tools + remote_tools,
            mcp_clients=MCP_CLIENTS,
            all_local_tool_objects={t.name: t for t in all_local_tool_objects},
            on_update=on_update if on_update is not None else dummy_on_update,
            scope=scope,
            model_name=chat_config.model_name,
            job_queue=job_queue,
        )
        return msg, claude_calls, tool_init_time
    raise ValueError("Should not reach here")


def add_caching_to_messages(
    raw_messages: list[MessageParam],
) -> list[NormalizedMessageParam]:
    """Add cache control to messages."""
    if not raw_messages:
        return []

    messages = [normalize_message_param(x) for x in raw_messages]
    messages = copy.deepcopy(messages)
    # Adding cache control to the last assistabt message. We can't add cache to
    # use message, as the user messages could be duplicated and that confuses
    # Claude.
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            msg["content"][-1]["cache_control"] = {"type": "ephemeral"}  # type: ignore
            break

    return messages


async def _process_query_with_tools(
    anthropic: Anthropic,
    system: str,
    messages: list[MessageParam],
    claude_tools: list[ClaudeTool],
    all_local_tool_objects: dict[str, LocalTool],
    mcp_clients: list[MCPClientBase],
    on_update: Callable[[list[MessageParam]], Awaitable[Any]],
    scope: InterruptionScope,
    job_queue: JobQueue,
    model_name: str = CLAUDE_MODEL_NAME,
) -> tuple[list[MessageParam], list[ClaudeCallInfo]]:
    remote_tool_to_mcp_client = {
        tool["name"]: mcp_client
        for mcp_client in mcp_clients
        for tool in (await mcp_client.list_tools())
    }

    async_client = get_async_anthropic_client()

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type(ANTHROPIC_EXCEPTIONS_TO_RETRY)
        & tenacity.retry_if_not_exception_type(RateLimitError),
        wait=tenacity.wait_exponential(multiplier=2, min=2, max=60),
        stop=tenacity.stop_after_attempt(5),
        before_sleep=lambda retry_state: logger.warning(
            f"API error, retrying in {retry_state.next_action and retry_state.next_action.sleep} seconds... "
            f"(Attempt {retry_state.attempt_number})"
        ),
    )
    def _get_retry_after(retry_state) -> float:
        exc = retry_state.outcome.exception()
        if isinstance(exc, RateLimitError) and exc.response:
            retry_after = exc.response.headers.get("retry-after")
            if retry_after:
                try:
                    return float(retry_after)
                except ValueError:
                    pass
        return 35.0

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type(RateLimitError),
        wait=_get_retry_after,
        stop=tenacity.stop_after_attempt(3),
        before_sleep=lambda retry_state: logger.warning(
            f"Rate limit hit, retrying in {_get_retry_after(retry_state):.0f}s... "
            f"(Attempt {retry_state.attempt_number})"
        ),
    )
    async def run_query_and_count_tokens() -> PartialSample:
        kwargs: dict = dict(
            system=[
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            model=model_name,
            messages=add_caching_to_messages(messages + extra_messages),
            tools=claude_tools,
            tool_choice=ToolChoiceAutoParam(
                type="auto", disable_parallel_tool_use=False
            ),
            # betas=["token-efficient-tools-2025-02-19"],
        )

        count = -2
        num_cached_tokens = -2
        all_text: list[str] = []
        response_time_secs: float | None = None
        start_time = time.monotonic()

        # Set up parallel interruption checking

        def get_interrupted_result():
            # Stream was cancelled due to interruption
            add_debug_message_to_queue(f"Stream cancelled due to interruption: {scope}")
            content: list[ContentBlock] = []
            if all_text:
                content.append(
                    TextBlock(
                        type="text",
                        text="".join(all_text) + " " + INTERRUPTION_MESSAGE,
                    )
                )

            return PartialSample(
                num_prompt_tokens=count,
                num_cached_tokens=num_cached_tokens,
                content=content,
                stop_reason="interrupt",
                seconds_till_start=response_time_secs,
            )

        if scope.is_interrupted:
            logger.warning("Interruption right before generation loop (soft)")
            return get_interrupted_result()

        try:
            stream_task = asyncio.current_task()
            check_interruption_started = asyncio.Event()
            sampling_is_done = asyncio.Event()

            async with async_client.beta.messages.stream(
                max_tokens=4096, **kwargs
            ) as stream:
                # Stream processing in the main task
                try:
                    # Use job_queue to schedule the interruption checker
                    job_queue.run_once(
                        check_interruption,
                        data=dict(
                            stream_task=stream_task,
                            scope=scope,
                            sampling_is_done=sampling_is_done,
                            check_interruption_started=check_interruption_started,
                        ),
                        when=0.0,
                    )
                    await check_interruption_started.wait()
                    if scope.is_interrupted:
                        logger.warning("Interruption right before generation loop")
                        return get_interrupted_result()
                    # Process the stream
                    async for chunk in stream:
                        if scope.is_interrupted:
                            logger.warning("Interruption within generation loop (soft)")
                            return get_interrupted_result()
                        if (
                            chunk.type == "content_block_delta"
                            and chunk.delta.type == "text_delta"
                        ):
                            text = chunk.delta.text
                            all_text.append(text)
                            await on_update(
                                [
                                    *extra_messages,
                                    {"role": "assistant", "content": "".join(all_text)},
                                ]
                            )
                        elif chunk.type == "message_start":
                            if response_time_secs is None:
                                response_time_secs = time.monotonic() - start_time
                            usage = chunk.message.usage
                            count = (
                                usage.input_tokens
                                + (usage.cache_creation_input_tokens or 0)
                                + (usage.cache_read_input_tokens or 0)
                            )
                            num_cached_tokens = usage.cache_read_input_tokens or 0

                    # Get the final message if stream completed normally
                    msg = await stream.get_final_message()
                    return PartialSample(
                        num_prompt_tokens=count,
                        num_cached_tokens=num_cached_tokens,
                        content=msg.content,
                        stop_reason="tool_use"
                        if msg.stop_reason == "tool_use"
                        else "eot",
                        seconds_till_start=response_time_secs,
                    )

                except asyncio.CancelledError:
                    logger.warning(
                        "Interruption within generation loop (CancelledError)"
                    )
                    return get_interrupted_result()
                finally:
                    logger.info("Exiting cancellable scope")
                    # Kill the check_interruption() job.
                    sampling_is_done.set()

        except ANTHROPIC_EXCEPTIONS_TO_RETRY as e:
            # This explicit catch ensures the tenacity decorator retries
            logger.warning(f"API error encountered: {type(e)} {e}")
            raise
        except APIStatusError as e:
            # This explicit catch ensures the tenacity decorator retries
            logger.warning(f"Weird API error encountered: {type(e)} {e}")
            raise

    # Process response and handle tool calls
    extra_messages: list[MessageParam] = []

    def convert_result_content_to_input_content(
        result_content: ContentBlock,
    ) -> TextBlockParam | ToolUseBlockParam:
        if isinstance(result_content, (TextBlock, BetaTextBlock)):
            return TextBlockParam(type="text", text=result_content.text)
        else:
            assert isinstance(
                result_content, (ToolUseBlock, BetaToolUseBlock)
            ), result_content
            return ToolUseBlockParam(
                type="tool_use",
                name=result_content.name,
                input=result_content.input,
                id=result_content.id,
            )

    claude_calls: list[ClaudeCallInfo] = []

    while True:
        logger.info(f"{len(messages)=}")

        try:
            partial_sample = await run_query_and_count_tokens()
        except tenacity.RetryError as e:
            logger.warning("Failed to retry sampling")
            partial_sample = PartialSample(
                num_prompt_tokens=-1,
                num_cached_tokens=-1,
                content=[
                    TextBlock(
                        type="text",
                        text=OVERLOAD_MESSAGE_TPL % e.last_attempt.exception(),
                    )
                ],
                stop_reason="interrupt",
                seconds_till_start=-1,
            )

        if not partial_sample.content and partial_sample.stop_reason == "interrupt":
            # Return nothing - no need to waste content.
            return [], claude_calls

        converted_content = [
            convert_result_content_to_input_content(content)
            for content in partial_sample.content
        ]
        extra_messages.append({"role": "assistant", "content": converted_content})
        # No need to call on_update here, as we are already doing it in the stream.
        tool_execution_results: list[ToolResultBlockParam] = []
        tool_execution_start_time = time.monotonic()
        for content in partial_sample.content:
            logger.debug(f"Content: {content}")
            if content.type == "text":
                pass
            elif content.type == "tool_use":
                assert isinstance(content, (ToolUseBlock, BetaToolUseBlock)), content

                tool_name = content.name
                tool_args = content.input
                assert isinstance(tool_args, dict), tool_args

                # Execute tool call
                if (mcp_client := remote_tool_to_mcp_client.get(tool_name)) is not None:
                    result = await mcp_client.call_tool(tool_name, tool_args)
                elif tool_name in all_local_tool_objects:
                    result = await all_local_tool_objects[tool_name](**tool_args)
                else:
                    result = ToolResult.error(f"Unknown tool: {tool_name}")
                # Show result to Claude.
                result_content: ToolResultBlockParam = {
                    "tool_use_id": content.id,
                    "type": "tool_result",
                    "content": result.get_content(),
                    "is_error": result.is_error,
                }
                tool_execution_results.append(result_content)
            else:
                logger.warning(f"Unknown content type: {content.type}")
        if tool_execution_results:
            tool_execution_time = time.monotonic() - tool_execution_start_time
            extra_messages.append({"role": "user", "content": tool_execution_results})
            await on_update(extra_messages)
        else:
            tool_execution_time = None
        claude_calls.append(
            ClaudeCallInfo(
                num_prompt_tokens=partial_sample.num_prompt_tokens,
                num_cached_tokens=partial_sample.num_cached_tokens,
                seconds_till_start=partial_sample.seconds_till_start,
                tool_execution_time=tool_execution_time,
            )
        )
        if partial_sample.stop_reason != "tool_use":
            logger.info(
                f"Claude finished sampling with stop reason: {partial_sample.stop_reason=}"
            )
            break

    return extra_messages, claude_calls


async def check_interruption(context: ContextTypes.DEFAULT_TYPE):
    """Check for interruption in a separate task and cancel the stream if interrupted."""
    logger.info("Starting check_interruption loop")
    stream_task = context.job.data["stream_task"]
    scope: InterruptionScope = context.job.data["scope"]
    sampling_is_done: asyncio.Event = context.job.data["sampling_is_done"]
    check_interruption_started: asyncio.Event = context.job.data[
        "check_interruption_started"
    ]

    check_interruption_started.set()
    try:
        while not scope.is_interrupted:
            await asyncio.sleep(0.1)  # Check every 100ms
            if sampling_is_done.is_set():
                return
        if scope.is_interrupted:
            logger.warning(
                f"Cancelling stream task from check_interruption for scope {scope.chat_id}"
            )
            stream_task.cancel()
            return
    except asyncio.CancelledError:
        # Just exit quietly if we were cancelled
        pass

import asyncio
import copy
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Protocol

import tenacity
from anthropic import APIStatusError, RateLimitError
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
from anthropic.types.beta import (
    BetaRedactedThinkingBlock,
    BetaRedactedThinkingBlockParam,
    BetaTextBlock,
    BetaThinkingBlock,
    BetaThinkingBlockParam,
    BetaToolUseBlock,
)

from yarvis_ptb.agent_config import AgentConfig
from yarvis_ptb.debug_chat import add_debug_message_to_queue
from yarvis_ptb.prompt_consts import INTERRUPTION_MESSAGE, SAMPLING_FAILED_MESSAGE_TPL
from yarvis_ptb.prompting import NormalizedMessageParam, normalize_message_param
from yarvis_ptb.ptb_util import (
    InterruptionScope,
    get_async_anthropic_client,
)
from yarvis_ptb.sampling import SamplingHooks, SamplingResult
from yarvis_ptb.settings.main import (
    CLAUDE_MODEL_NAME,
)
from yarvis_ptb.tools.bash_repl import BashRunTool
from yarvis_ptb.tools.collect_message_tool import CollectMessageTool
from yarvis_ptb.tools.editor_tool import EditorTool
from yarvis_ptb.tools.file_tools import build_chat_send_file_tools
from yarvis_ptb.tools.gcal_tools import get_calendar_tools
from yarvis_ptb.tools.gmail_tool import get_gmail_tools
from yarvis_ptb.tools.image_tools import build_image_tools
from yarvis_ptb.tools.memory_tools import build_memory_tools
from yarvis_ptb.tools.message_search_tool import build_message_search_tools
from yarvis_ptb.tools.message_tool import build_message_tools
from yarvis_ptb.tools.nest_tools import get_nest_tools
from yarvis_ptb.tools.python_repl import PythonREPLTool
from yarvis_ptb.tools.scheduling_tools import build_scheduling_tools
from yarvis_ptb.tools.subagent_tool import build_subagent_tools
from yarvis_ptb.tools.telegram_tools import get_telegram_tools
from yarvis_ptb.tools.todo_tools import build_todo_tools
from yarvis_ptb.tools.tool_output import GetToolOutputTool
from yarvis_ptb.tools.tool_spec import ClaudeTool, LocalTool, ToolResult
from yarvis_ptb.tools.whoop_tools import get_whoop_tools

logger = logging.getLogger(__name__)

TOOL_DEFAULT_TIMEOUT_SEC = 15
TOOL_MAX_TIMEOUT_SEC = 600
TOOL_EXECUTION_TIMEOUT_SEC = TOOL_MAX_TIMEOUT_SEC + 10  # hard limit per tool call


def _build_generic_tools() -> list[LocalTool]:
    """Fresh instances each call — these are stateful, must not be shared across requests."""
    return [PythonREPLTool(), BashRunTool(), EditorTool()]


TELEGRAM_TOOLS: list[LocalTool] = get_telegram_tools()

ANTON_DATA_TOOLS: list[LocalTool] = (
    get_calendar_tools() + get_gmail_tools() + get_whoop_tools() + get_nest_tools()
)


class JobQueueLike(Protocol):
    def run_once(
        self, callback: Any, *, data: Any = None, when: float = 0.0
    ) -> Any: ...


ADAPTIVE_THINKING_MODELS = {"claude-opus-4-6", "claude-sonnet-4-6"}

ANTHROPIC_EXCEPTIONS_TO_RETRY: tuple[type[APIStatusError], ...] = (APIStatusError,)


@dataclass
class ClaudeCallInfo:
    num_prompt_tokens: int
    num_cached_tokens: int
    num_cache_creation_tokens: int
    num_output_tokens: int
    seconds_till_start: float | None
    tool_execution_time: float | None

    @property
    def num_uncached_input_tokens(self) -> int:
        return max(
            0,
            self.num_prompt_tokens
            - self.num_cached_tokens
            - self.num_cache_creation_tokens,
        )

    def to_usage_dict(self, pricing: "ModelPricing | None" = None) -> dict:
        """Return a dict with the 4 token categories, timing, and optional cost."""
        d: dict = {
            "uncached_input": self.num_uncached_input_tokens,
            "cached_input": self.num_cached_tokens,
            "cache_creation": self.num_cache_creation_tokens,
            "output": self.num_output_tokens,
            "seconds_till_start": self.seconds_till_start,
            "tool_execution_time": self.tool_execution_time,
        }
        if pricing is not None:
            d["cost_usd"] = (
                self.num_uncached_input_tokens * pricing.input
                + self.num_cached_tokens * pricing.cache_read
                + self.num_cache_creation_tokens * pricing.cache_creation
                + self.num_output_tokens * pricing.output
            )
        return d


@dataclass
class ModelPricing:
    """Per-token pricing in dollars."""

    input: float
    cache_read: float
    cache_creation: float
    output: float


MODEL_PRICING: dict[str, ModelPricing] = {
    "claude-opus-4-6": ModelPricing(
        input=5.0 / 1e6,
        cache_read=0.50 / 1e6,
        cache_creation=6.25 / 1e6,
        output=25.0 / 1e6,
    ),
    "claude-sonnet-4-6": ModelPricing(
        input=3.0 / 1e6,
        cache_read=0.30 / 1e6,
        cache_creation=3.75 / 1e6,
        output=15.0 / 1e6,
    ),
    "claude-haiku-4-5-20251001": ModelPricing(
        input=1.0 / 1e6,
        cache_read=0.10 / 1e6,
        cache_creation=1.25 / 1e6,
        output=5.0 / 1e6,
    ),
}


def estimate_cost(calls: list[ClaudeCallInfo], model_name: str) -> float | None:
    """Estimate dollar cost from a list of ClaudeCallInfo for the given model."""
    b = cost_breakdown(calls, model_name)
    if b is None:
        return None
    return sum(b.values())


def cost_breakdown(
    calls: list[ClaudeCallInfo], model_name: str
) -> dict[str, float] | None:
    """Return per-category dollar costs: uncached_input, cached_input, cache_creation, output."""
    p = MODEL_PRICING.get(model_name)
    if p is None:
        return None
    return {
        "uncached_input": sum(c.num_uncached_input_tokens for c in calls) * p.input,
        "cached_input": sum(c.num_cached_tokens for c in calls) * p.cache_read,
        "cache_creation": sum(c.num_cache_creation_tokens for c in calls)
        * p.cache_creation,
        "output": sum(c.num_output_tokens for c in calls) * p.output,
    }


@dataclass
class PartialSample:
    content: list[ContentBlock]
    stop_reason: Literal["tool_use", "eot", "interrupt"]
    num_prompt_tokens: int
    num_cached_tokens: int
    num_cache_creation_tokens: int
    num_output_tokens: int
    seconds_till_start: float | None


def get_tools_for_agent_config(
    agent_config: AgentConfig, curr, chat_id, bot
) -> list[LocalTool]:
    """Build tools based on AgentConfig.

    Uses sampling.tool_subset as base, then adds tools implied by
    rendering/sampling config (memory, tool_output, messaging).
    """
    tool_subset = agent_config.sampling.tool_subset

    if tool_subset == "all":
        tool_classes = [
            "anton_google",
            "fs",
            "scheduling",
            "anton_message_search",
            "chat_send_file",
            "telegram",
            "image_editing",
            "subagent",
            "todo",
        ]
    else:
        # Specific tools requested — build from names
        return _get_tools_by_names(tool_subset, curr, chat_id, bot)

    # Add tools derived from config properties
    if agent_config.requires_memory_tools:
        tool_classes.append("memory")
    if agent_config.requires_tool_output_tool:
        tool_classes.append("tool_output")
    if agent_config.requires_messaging_tool:
        if agent_config.sampling.collect_messages:
            tool_classes.append("collect_messaging")
        else:
            tool_classes.append("messaging")

    return _build_tools_from_classes(tool_classes, curr, chat_id, bot)


def _build_tools_from_classes(
    tool_classes: list[str],
    curr,
    chat_id: int,
    bot,
) -> list[LocalTool]:
    """Build tool objects from a list of tool class names."""
    all_local_tool_objects = []
    for tool_class in tool_classes:
        if tool_class == "anton_google":
            all_local_tool_objects.extend(ANTON_DATA_TOOLS)
        elif tool_class == "fs":
            all_local_tool_objects.extend(_build_generic_tools())
        elif tool_class == "scheduling":
            all_local_tool_objects.extend(build_scheduling_tools(curr, chat_id))
        elif tool_class == "anton_message_search":
            all_local_tool_objects.extend(build_message_search_tools(chat_id))
        elif tool_class == "chat_send_file":
            all_local_tool_objects.extend(build_chat_send_file_tools(chat_id, bot))
        elif tool_class == "telegram":
            all_local_tool_objects.extend(TELEGRAM_TOOLS)
        elif tool_class == "messaging":
            all_local_tool_objects.extend(build_message_tools(bot, chat_id, curr))
        elif tool_class == "collect_messaging":
            all_local_tool_objects.append(CollectMessageTool())
        elif tool_class == "image_editing":
            all_local_tool_objects.extend(build_image_tools(chat_id, curr))
        elif tool_class == "memory":
            all_local_tool_objects.extend(build_memory_tools())
        elif tool_class == "subagent":
            all_local_tool_objects.extend(build_subagent_tools(curr, chat_id, bot))
        elif tool_class == "todo":
            all_local_tool_objects.extend(build_todo_tools())
        elif tool_class == "tool_output":
            all_local_tool_objects.append(GetToolOutputTool(curr))
        else:
            raise ValueError(f"Unknown tool class: {tool_class}")
    return all_local_tool_objects


async def process_query(
    system: str,
    messages: list[MessageParam],
    agent_config: AgentConfig,
    tools: list[LocalTool],
    hooks: SamplingHooks,
    job_queue: JobQueueLike,
    scope: InterruptionScope,
) -> SamplingResult:
    """Process a query using Claude and available tools.

    Tools should be pre-resolved by the caller using get_tools_for_agent_config().
    """
    config = agent_config.sampling
    model_name = config.resolve_model_name()

    start = time.monotonic()
    local_tools: list[ClaudeTool] = [tool.spec().to_claude_tool() for tool in tools]
    async with AsyncExitStack() as stack:
        for local_tool in tools:
            if hooks.is_interrupted:
                logger.warning("Interruption before generation started")
                return SamplingResult()
            await stack.enter_async_context(local_tool.context())
        if hooks.is_interrupted:
            logger.warning("Interruption before generation started")
            return SamplingResult()
        tool_init_time = time.monotonic() - start
        tool_map = {t.name: t for t in tools}
        msg, claude_calls = await _process_query_with_tools(
            system,
            messages,
            claude_tools=local_tools,
            all_local_tool_objects=tool_map,
            on_update=hooks.on_update,
            scope=scope,
            model_name=model_name,
            job_queue=job_queue,
            max_tokens=config.max_tokens,
            thinking=config.thinking,
        )
        # Collect subagent cost info if any subagents were used
        from yarvis_ptb.tools.subagent_tool import _SubagentBase

        subagent_usages: list[dict] = []
        for tool in tool_map.values():
            if isinstance(tool, _SubagentBase) and tool.subagent_usages:
                subagent_usages.extend(tool.subagent_usages)

        # Extract agent messages from the result
        agent_messages = _extract_agent_messages(msg, config.output_mode)

        return SamplingResult(
            message_params=msg,
            agent_messages=agent_messages,
            claude_calls=claude_calls,
            subagent_usages=subagent_usages,
            tool_init_time=tool_init_time,
        )


def _extract_agent_messages(
    message_params: list[MessageParam],
    output_mode: str,
) -> list[str]:
    """Extract the agent's output messages from message_params."""
    if output_mode == "tool_message":
        # Collect text from send_message tool calls
        messages: list[str] = []
        for msg in message_params:
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "send_message"
                    and isinstance(block.get("input"), dict)
                ):
                    text = block["input"].get("text") or block["input"].get(
                        "message", ""
                    )
                    if text:
                        messages.append(text)
        return messages
    else:
        # Extract from final assistant text
        for msg in reversed(message_params):
            if msg.get("role") == "assistant":
                content = msg.get("content", [])
                if isinstance(content, str):
                    return [content] if content else []
                texts = []
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                        and block.get("text")
                    ):
                        texts.append(block["text"])
                if texts:
                    return ["\n".join(texts)]
        return []


def add_caching_to_messages(
    raw_messages: list[MessageParam],
) -> list[NormalizedMessageParam]:
    """Add cache control to messages."""
    if not raw_messages:
        return []

    messages = [normalize_message_param(x) for x in raw_messages]
    messages = copy.deepcopy(messages)
    # Strip empty text blocks — the API rejects them
    for msg in messages:
        msg["content"] = [  # type: ignore
            b
            for b in msg["content"]
            if not (
                isinstance(b, dict) and b.get("type") == "text" and not b.get("text")
            )
        ]
    # Adding cache control to the last assistabt message. We can't add cache to
    # use message, as the user messages could be duplicated and that confuses
    # Claude.
    for msg in reversed(messages):
        if msg["role"] == "assistant" and msg["content"]:
            # Find the last non-empty content block to cache
            for block in reversed(list(msg["content"])):
                if block.get("type") != "text" or block.get("text"):  # type: ignore
                    block["cache_control"] = {"type": "ephemeral"}  # type: ignore
                    break
            else:
                # All blocks were empty text — skip to previous assistant message
                continue
            break

    return messages


async def _process_query_with_tools(
    system: str,
    messages: list[MessageParam],
    claude_tools: list[ClaudeTool],
    all_local_tool_objects: dict[str, LocalTool],
    on_update: Callable[[list[MessageParam]], Awaitable[Any]],
    scope: InterruptionScope,
    job_queue: JobQueueLike,
    model_name: str = CLAUDE_MODEL_NAME,
    max_tokens: int = 16000,
    thinking: str = "adaptive",
) -> tuple[list[MessageParam], list[ClaudeCallInfo]]:
    async_client = get_async_anthropic_client()

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
        retry=tenacity.retry_if_exception_type(ANTHROPIC_EXCEPTIONS_TO_RETRY)
        & tenacity.retry_if_not_exception_type(RateLimitError),
        wait=tenacity.wait_exponential(multiplier=2, min=2, max=60),
        stop=tenacity.stop_after_attempt(5),
        before_sleep=lambda retry_state: logger.warning(
            f"API error, retrying in {retry_state.next_action and retry_state.next_action.sleep} seconds... "
            f"(Attempt {retry_state.attempt_number})"
        ),
    )
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
        if thinking == "adaptive" and model_name in ADAPTIVE_THINKING_MODELS:
            kwargs["thinking"] = {"type": "adaptive"}

        count = -2
        num_cached_tokens = -2
        num_cache_creation_tokens = 0
        num_output_tokens = 0
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
                num_cache_creation_tokens=num_cache_creation_tokens,
                num_output_tokens=num_output_tokens,
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
                max_tokens=max_tokens, **kwargs
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
                            update_msg: MessageParam = {
                                "role": "assistant",
                                "content": "".join(all_text),
                            }
                            await on_update(
                                [
                                    *extra_messages,
                                    update_msg,
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
                            num_cache_creation_tokens = (
                                usage.cache_creation_input_tokens or 0
                            )

                    # Get the final message if stream completed normally
                    msg = await stream.get_final_message()
                    num_output_tokens = msg.usage.output_tokens
                    return PartialSample(
                        num_prompt_tokens=count,
                        num_cached_tokens=num_cached_tokens,
                        num_cache_creation_tokens=num_cache_creation_tokens,
                        num_output_tokens=num_output_tokens,
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
        if isinstance(result_content, BetaThinkingBlock):
            return BetaThinkingBlockParam(
                type="thinking",
                thinking=result_content.thinking,
                signature=result_content.signature,
            )
        if isinstance(result_content, BetaRedactedThinkingBlock):
            return BetaRedactedThinkingBlockParam(
                type="redacted_thinking", data=result_content.data
            )
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
        except Exception as e:
            if isinstance(e, tenacity.RetryError):
                exc_str = str(e.last_attempt.exception())
            else:
                exc_str = f"{type(e).__name__}: {e}"
            logger.warning(f"Sampling failed: {exc_str}")
            partial_sample = PartialSample(
                num_prompt_tokens=-1,
                num_cached_tokens=-1,
                num_cache_creation_tokens=0,
                num_output_tokens=0,
                content=[
                    TextBlock(type="text", text=SAMPLING_FAILED_MESSAGE_TPL % exc_str)
                ],
                stop_reason="interrupt",
                seconds_till_start=-1,
            )

        if not partial_sample.content and partial_sample.stop_reason == "interrupt":
            # Return nothing - no need to waste content.
            return [], claude_calls

        logger.info(
            f"Response content types: {[c.type for c in partial_sample.content]}"
        )
        converted_content = [
            convert_result_content_to_input_content(content)
            for content in partial_sample.content
        ]
        extra_messages.append({"role": "assistant", "content": converted_content})
        # No need to call on_update here, as we are already doing it in the stream.
        tool_execution_results: list[ToolResultBlockParam] = []
        should_stop_after = False
        tool_execution_start_time = time.monotonic()
        for content in partial_sample.content:
            logger.debug(f"Content: {content}")
            if content.type == "text":
                pass
            elif content.type in ("thinking", "redacted_thinking"):
                pass
            elif content.type == "tool_use":
                assert isinstance(content, (ToolUseBlock, BetaToolUseBlock)), content

                tool_name = content.name
                tool_args = content.input
                assert isinstance(tool_args, dict), tool_args

                # Execute tool call
                if tool_name in all_local_tool_objects:
                    try:
                        result = await asyncio.wait_for(
                            all_local_tool_objects[tool_name](**tool_args),
                            timeout=TOOL_EXECUTION_TIMEOUT_SEC,
                        )
                    except asyncio.TimeoutError:
                        result = ToolResult.error(
                            f"Tool {tool_name} timed out after {TOOL_EXECUTION_TIMEOUT_SEC}s"
                        )
                else:
                    result = ToolResult.error(f"Unknown tool: {tool_name}")
                # Show result to Claude.
                result_content: ToolResultBlockParam = {
                    "tool_use_id": content.id,
                    "type": "tool_result",
                    "content": result.get_content(),
                    "is_error": result.is_error,
                }
                if result.stop_after:
                    should_stop_after = True
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
                num_cache_creation_tokens=partial_sample.num_cache_creation_tokens,
                num_output_tokens=partial_sample.num_output_tokens,
                seconds_till_start=partial_sample.seconds_till_start,
                tool_execution_time=tool_execution_time,
            )
        )
        if should_stop_after:
            logger.info("Stopping sampling loop early due to stop_after flag")
            break
        if partial_sample.stop_reason != "tool_use":
            logger.info(
                f"Claude finished sampling with stop reason: {partial_sample.stop_reason=}"
            )
            break

    # Ensure all content fields are plain lists — pydantic-core ≥2.28 may
    # produce ValidatorIterator objects that are not JSON-serializable.
    for msg in extra_messages:
        c = msg.get("content")
        if c is not None and not isinstance(c, list):
            msg["content"] = list(c)  # type: ignore[typeddict-item]

    return extra_messages, claude_calls


class _DummyJobQueue:
    """Minimal stand-in for telegram JobQueue used by subagent (no interruption checking)."""

    def run_once(self, callback, *, data=None, when=0.0):
        # Subagent doesn't need interruption checking.
        # Signal that the check started so the stream doesn't hang.
        if data and "check_interruption_started" in data:
            data["check_interruption_started"].set()


def _get_tools_by_names(
    names: Literal["all"] | list[str], curr, chat_id: int, bot
) -> list[LocalTool]:
    """Pick specific tools by name from the full set of available tools.

    If names is "all", returns all available tools.
    Creates fresh instances of generic tools to avoid sharing state with the parent.
    """
    # Build all tool classes (excluding subagent to prevent recursion)
    all_classes = [
        "anton_google",
        "fs",
        "scheduling",
        "anton_message_search",
        "chat_send_file",
        "telegram",
        "image_editing",
        "memory",
        "todo",
        "tool_output",
    ]
    all_tools = _build_tools_from_classes(all_classes, curr, chat_id, bot)
    name_to_tool = {t.name: t for t in all_tools}

    # Override generic tools with fresh instances so subagent doesn't share
    # state (init/close) with the parent's singletons
    for tool in _build_generic_tools():
        name_to_tool[tool.name] = tool

    if names == "all":
        return list(name_to_tool.values())

    result = []
    for name in names:
        if name in name_to_tool:
            result.append(name_to_tool[name])
        else:
            logger.warning(f"Subagent requested unknown tool: {name}")
    return result


async def check_interruption(context: Any) -> None:
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

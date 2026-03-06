import asyncio
import base64
import datetime
import io
import json
import logging
import os
import time
import traceback
from dataclasses import asdict, is_dataclass
from typing import Any

from anthropic import Anthropic
from anthropic.types import (
    MessageParam,
)
from telegram import Bot, Update, constants
from telegram.ext import Application, CallbackContext

from yarvis_ptb import tool_sampler
from yarvis_ptb.agent_config import AgentConfig
from yarvis_ptb.debug_chat import (
    MessageAsFile,
    add_debug_message_to_queue,
    force_send_to_debug_chat,
    maybe_send_messages_to_debug_chat,
)
from yarvis_ptb.message_search import save_message_and_update_index
from yarvis_ptb.on_disk_memory import commit_memory
from yarvis_ptb.prompting import (
    COMPLEX_ANTON_PROMPT,
    build_claude_input,
    build_context_info,
    convert_db_messages_to_claude_messages,
    render_claude_response_short,
    render_mesage_param_exact,
)
from yarvis_ptb.ptb_util import (
    AuthInfo,
    InterruptionScope,
    build_interruptable_scope,
    get_anthropic_client,
    reply_maybe_markdown,
    typing_action,
)
from yarvis_ptb.rendering_config import RenderingConfig
from yarvis_ptb.sampling import SamplingConfig
from yarvis_ptb.settings import (
    BOT_USER_ID,
    DEFAULT_TIMEZONE,
    HISTORY_LENGTH_LONG_TURNS,
    SYSTEM_USER_ID,
    USER_ID_MAP,
)
from yarvis_ptb.storage import (
    IMAGE_B64_META_FIELD,
    DbMessage,
    Invocation,
    VariablesForChat,
    archive_marked_messages,
    deactivate_schedule,
    get_messages,
    get_schedules,
)
from yarvis_ptb.util import RateController, ensure

COMPLEX_CHAT_LOCK = asyncio.Lock()

DEFAULT_AGENT_CONFIG = AgentConfig(
    rendering=RenderingConfig(
        prompt_name=COMPLEX_ANTON_PROMPT,
        include_memories=True,
        max_history_length_turns=HISTORY_LENGTH_LONG_TURNS,
        tool_result_truncation_after_n_turns=5,
    ),
    sampling=SamplingConfig(
        model="opus",
        tool_subset="all",
        output_mode="tool_message",
    ),
)


logger = logging.getLogger(__name__)


class TelegramHooks:
    """SamplingHooks implementation for interactive Telegram sessions."""

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        application: Application,
        output_message: Any | None,
        output_mode: str,
        scope: InterruptionScope,
    ):
        self._bot = bot
        self._chat_id = chat_id
        self._application = application
        self._output_message = output_message
        self._output_mode = output_mode
        self._scope = scope
        self._rate_controller = RateController(wait_between_events_secs=1.0)
        self._num_messages_sent_to_debug_chat = 0

    @property
    def output_message(self):
        return self._output_message

    @property
    def num_messages_sent_to_debug_chat(self) -> int:
        return self._num_messages_sent_to_debug_chat

    async def on_update(self, message_params: list[MessageParam]) -> None:
        if self._rate_controller.can_run():
            if self._output_message is not None:
                response_text = (
                    render_claude_response_short(message_params, remove_thinking=False)
                    + "\n**processing...**"
                )
                if self._output_mode == "tool_message":
                    response_text = format_as_quote(response_text)
                self._output_message = await reply_maybe_markdown(
                    self._bot,
                    self._chat_id,
                    message=self._output_message,
                    text=response_text,
                )
            new_complete_debug_messages = (
                len(message_params) - self._num_messages_sent_to_debug_chat - 1
            )
            if new_complete_debug_messages:
                add_debug_message_to_queue(
                    message_params[:-1],
                    skip_first_n=self._num_messages_sent_to_debug_chat,
                )
                self._num_messages_sent_to_debug_chat += new_complete_debug_messages
                ensure(self._application.job_queue).run_once(
                    maybe_send_messages_to_debug_chat, when=1
                )

    @property
    def is_interrupted(self) -> bool:
        return self._scope.is_interrupted


class DataclassJSONEncoder(json.JSONEncoder):
    data_class_float_format: str | None = ".2f"

    def default(self, o):
        obj = o
        del o
        if is_dataclass(obj):
            assert not isinstance(obj, type), obj
            return apply_float_format(asdict(obj), self.data_class_float_format)

        return super().default(obj)


def apply_float_format(obj, float_format: str | None):
    """Recursively apply float format to all float values in a dictionary.

    Note, maps float to string if float_format is not None.
    """
    if float_format is None:
        return obj
    if isinstance(obj, float):
        return format(obj, float_format)
    elif isinstance(obj, dict):
        return {k: apply_float_format(v, float_format) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [apply_float_format(x, float_format) for x in obj]
    return obj


def format_as_quote(text: str) -> str:
    """Format text as a markdown quote by adding '> ' prefix to each line."""
    return "> " + text.replace("\n", "\n> ")


async def handle_message_root_user_assistant(
    curr, auth: AuthInfo, update: Update, context: CallbackContext, is_voice: bool
) -> None:
    if not update.message:
        logger.warning(f"No message: {update}")
        return
    assert auth.user_id in USER_ID_MAP, auth.user_id

    if auth.is_root_user_debug_chat:
        # User send message to debug chat. Silently forward to the main chat and
        # respond as usual.
        chat_id = auth.root_user_id
        await context.bot.forward_message(
            chat_id=chat_id,
            from_chat_id=update.message.chat_id,
            message_id=update.message.message_id,
            disable_notification=False,
        )
    else:
        chat_id = update.message.chat_id
    agent_config = DEFAULT_AGENT_CONFIG

    chat_vars = VariablesForChat(curr=curr, chat_id=chat_id)
    logger.info(f"{chat_vars.variables=}")

    if chat_vars.get(chat_vars.KILL_SWITCH):
        await update.message.reply_text("Kill switch on")
        return

    # Check for any active reply_timeout schedules and deactivate them
    # since the user has replied
    scheduled_invocations = get_schedules(curr, chat_id)
    for sched in scheduled_invocations:
        if sched.meta.get("invocation_type") == "reply_timeout":
            logger.info(f"Deactivating reply timeout schedule: {sched.schedule_id}")
            deactivate_schedule(curr, sched)

    # Debug logging for message type detection
    logger.info(
        f"Message type check - text: {bool(update.message.text)}, "
        f"photo: {bool(update.message.photo)}, "
        f"document: {bool(update.message.document)}, "
        f"audio: {bool(update.message.audio)}, "
        f"video: {bool(update.message.video)}"
    )

    if update.message.text:
        initial_db_message = DbMessage(
            chat_id=chat_id,
            created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
            user_id=ensure(update.message.from_user).id,
            message=update.message.text,
            meta=dict(is_voice=is_voice),
        )
    elif update.message.photo:
        image_file_ref = await update.message.effective_attachment[-1].get_file()
        image_buffer = io.BytesIO()
        await image_file_ref.download_to_memory(image_buffer)
        initial_db_message = DbMessage(
            chat_id=chat_id,
            created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
            user_id=ensure(update.message.from_user).id,
            message=update.message.caption or "",
            meta={
                IMAGE_B64_META_FIELD: base64.b64encode(image_buffer.getvalue()).decode()
            },
        )
    elif update.message.document or update.message.audio or update.message.video:
        # Handle file uploads (documents, audio, video)
        file_obj = (
            update.message.document or update.message.audio or update.message.video
        )
        file_ref = await file_obj.get_file()

        # Create /tmp directory if it doesn't exist
        os.makedirs("/tmp", exist_ok=True)

        # Determine file name and type
        if update.message.document:
            file_name = file_obj.file_name or f"file_{file_obj.file_id}"
            file_type = "Document"
        elif update.message.audio:
            # Audio files may have title, performer, or just use file_id
            file_name = (
                getattr(file_obj, "file_name", None) or f"audio_{file_obj.file_id}.m4a"
            )
            file_type = "Audio"
        else:  # video
            file_name = (
                getattr(file_obj, "file_name", None) or f"video_{file_obj.file_id}.mp4"
            )
            file_type = "Video"

        # Save file to /tmp with original filename
        file_path = os.path.join("/tmp", file_name)
        await file_ref.download_to_drive(file_path)

        logger.info(
            f"{file_type} file uploaded: {file_path} (size: {file_obj.file_size} bytes)"
        )

        # Build message informing Claude about the file
        caption = update.message.caption or ""
        file_info = f"[{file_type} uploaded: {file_name} ({file_obj.file_size} bytes) saved to {file_path}]"
        message_text = f"{file_info}\n{caption}" if caption else file_info

        initial_db_message = DbMessage(
            chat_id=chat_id,
            created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
            user_id=ensure(update.message.from_user).id,
            message=message_text,
            meta={
                "uploaded_file": {
                    "file_path": file_path,
                    "file_name": file_name,
                    "file_size": file_obj.file_size,
                    "mime_type": getattr(file_obj, "mime_type", None),
                    "file_type": file_type.lower(),
                }
            },
        )
    else:
        logger.warning(f"Unsupported message type: {update.message}")
        return

    # Capture quote-reply context if the user is replying to a specific message
    if update.message.reply_to_message:
        reply_msg = update.message.reply_to_message
        reply_text = reply_msg.text or reply_msg.caption or ""
        if reply_text:
            initial_db_message.meta = initial_db_message.meta or {}
            initial_db_message.meta["reply_to"] = {
                "text": reply_text,
                "from": reply_msg.from_user.first_name
                if reply_msg.from_user
                else "Unknown",
                "date": reply_msg.date.isoformat() if reply_msg.date else None,
            }

    await process_multi_message_claude_invocation(
        curr=curr,
        application=context.application,
        bot=context.bot,
        chat_id=chat_id,
        agent_config=agent_config,
        invocation=Invocation(
            invocation_type="reply", reply_to_message_id=ensure(update.message).id
        ),
        initial_db_message=initial_db_message,
        telegram_message_id=update.message.id,
    )


async def process_multi_message_claude_invocation(
    curr,
    application: Application,
    bot: Bot,
    chat_id: int,
    invocation: Invocation,
    *,
    agent_config: AgentConfig,
    initial_db_message: DbMessage | None = None,
    skip_db: bool = False,
    telegram_message_id: int | None = None,
):
    async with COMPLEX_CHAT_LOCK:
        return await _process_multi_message_claude_invocation_no_lock(
            curr,
            application,
            bot,
            chat_id,
            invocation,
            agent_config=agent_config,
            initial_db_message=initial_db_message,
            skip_db=skip_db,
            telegram_message_id=telegram_message_id,
        )


async def _process_multi_message_claude_invocation_no_lock(
    curr,
    application: Application,
    bot: Bot,
    chat_id: int,
    invocation: Invocation,
    *,
    agent_config: AgentConfig,
    initial_db_message: DbMessage | None = None,
    skip_db: bool = False,
    forced_now_date: datetime.datetime | None = None,
    telegram_message_id: int | None = None,
):
    async with typing_action(bot, chat_id):
        await _process_multi_message_claude_invocation_inner(
            curr,
            application,
            bot,
            chat_id,
            invocation,
            agent_config=agent_config,
            initial_db_message=initial_db_message,
            skip_db=skip_db,
            forced_now_date=forced_now_date,
            telegram_message_id=telegram_message_id,
        )


async def _process_multi_message_claude_invocation_inner(
    curr,
    application: Application,
    bot: Bot,
    chat_id: int,
    invocation: Invocation,
    *,
    agent_config: AgentConfig,
    initial_db_message: DbMessage | None = None,
    skip_db: bool = False,
    forced_now_date: datetime.datetime | None = None,
    telegram_message_id: int | None = None,
):
    start = time.monotonic()
    client = get_anthropic_client()
    rendering_config = agent_config.rendering
    sampling_config = agent_config.sampling
    output_mode = sampling_config.output_mode
    model_name = sampling_config.resolve_model_name()

    do_streaming = (
        invocation.invocation_type != "schedule" and output_mode != "tool_message"
    )
    if invocation.invocation_type != "schedule" and invocation.reply_to_message_id:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=invocation.reply_to_message_id,
            reaction=constants.ReactionEmoji.EYES,
        )  # type: ignore

    if do_streaming:
        output_message = await reply_maybe_markdown(
            bot, chat_id, "**Processing...**", disable_notification=True
        )
    else:
        output_message = None

    now_date = (forced_now_date or datetime.datetime.now()).astimezone(DEFAULT_TIMEZONE)

    max_history_length_turns = rendering_config.max_history_length_turns
    db_messages = get_messages(curr, chat_id=chat_id, limit=max_history_length_turns)
    if initial_db_message is not None:
        initial_db_message.created_at = now_date
        if initial_db_message.user_id == SYSTEM_USER_ID:
            add_debug_message_to_queue(f"**SYSTEM:**\n" + initial_db_message.message)
        else:
            add_debug_message_to_queue(f"**ANTON:**\n" + initial_db_message.message)
            db_messages = [*db_messages, initial_db_message][-max_history_length_turns:]

    scheduled_invocations = get_schedules(curr, chat_id)

    logger.debug("Sending message to Claude")
    system, message_params = build_claude_input(
        db_messages,
        rendering_config,
        invocation=invocation,
        scheduled_invocations=scheduled_invocations,
        forced_now_date=now_date,
    )

    context_message = build_context_info(
        invocation=invocation,
        scheduled_invocations=scheduled_invocations,
        rendering_config=rendering_config,
        forced_now_date=now_date,
    )
    add_debug_message_to_queue(f"**CONTEXT:**:\n```\n{context_message}\n```")

    if invocation.invocation_type == "context_overflow":
        verbatim_claude_input = dict(system=system, messages=message_params)
        add_debug_message_to_queue(
            MessageAsFile(
                message=json.dumps(verbatim_claude_input, indent=2),
                file_prefix="context_overflow_full_claude_input_",
                file_suffix=".json",
            )
        )
    ensure(application.job_queue).run_once(maybe_send_messages_to_debug_chat, when=1)

    sizes = compute_token_counts(
        client,
        system,
        db_messages,
        message_params,
    )

    pre_call_time = time.monotonic() - start

    # Resolve tools
    all_tools = tool_sampler.get_tools_for_agent_config(
        agent_config, curr, chat_id, bot
    )

    with build_interruptable_scope(chat_id, message_id=telegram_message_id) as scope:
        hooks = TelegramHooks(
            bot=bot,
            chat_id=chat_id,
            application=application,
            output_message=output_message,
            output_mode=output_mode,
            scope=scope,
        )
        logger.info(
            f"Divining into tool_sampler.process_query: {chat_id=} {telegram_message_id=}"
        )
        try:
            result = await tool_sampler.process_query(
                system=system,
                messages=message_params,
                agent_config=agent_config,
                tools=all_tools,
                hooks=hooks,
                job_queue=application.job_queue,
                scope=scope,
            )
        except Exception:
            tb = traceback.format_exc()
            logger.exception("process_query crashed")
            await force_send_to_debug_chat(f"**process_query CRASHED**\n```\n{tb}\n```")
            error_line = tb.splitlines()[-1]
            if hooks.output_message is not None:
                await reply_maybe_markdown(
                    bot,
                    chat_id,
                    f"Generation failed: {error_line}",
                    message=hooks.output_message,
                    final_update=True,
                )
            if not skip_db:
                if initial_db_message is not None:
                    save_message_and_update_index(curr, initial_db_message)
                error_msg = DbMessage(
                    chat_id=chat_id,
                    created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
                    user_id=SYSTEM_USER_ID,
                    message=f"Generation failed: {error_line}",
                )
                save_message_and_update_index(curr, error_msg)
            return

    message_params = result.message_params
    claude_calls = result.claude_calls

    prompt_size: int | None
    cost: float | None = None
    if claude_calls:
        prompt_size = claude_calls[0].num_prompt_tokens if claude_calls else -2
        cost = tool_sampler.estimate_cost(claude_calls, model_name)
        sizes["claude_calls"] = claude_calls
        sizes["tool_init_time"] = result.tool_init_time
        sizes["pre_call_time"] = pre_call_time
        if cost is not None:
            sizes["estimated_cost_usd"] = f"${cost:.4f}"
        add_debug_message_to_queue(
            f"**SIZES:**:\n```\n{json.dumps(sizes, indent=2, sort_keys=True, cls=DataclassJSONEncoder)}\n```",
        )
    else:
        prompt_size = None

    if message_params:
        if output_mode == "tool_message":
            if hooks.output_message is not None:
                await bot.delete_message(
                    chat_id=chat_id, message_id=hooks.output_message.id
                )  # type: ignore
        else:
            response_text = render_claude_response_short(message_params)
            await reply_maybe_markdown(
                bot,
                chat_id,
                response_text,
                message=hooks.output_message,
                final_update=True,
            )
    else:
        if hooks.output_message is not None:
            await bot.delete_message(
                chat_id=chat_id, message_id=hooks.output_message.id
            )  # type: ignore
        if invocation.reply_to_message_id:
            await bot.set_message_reaction(
                chat_id=chat_id,
                message_id=invocation.reply_to_message_id,
                reaction=constants.ReactionEmoji.SHRUG,
            )  # type: ignore

    if rendering_config.include_memories and any(
        x["type"] == "tool_use"
        for mp in message_params
        if isinstance(mp["content"], list)
        for x in mp["content"]
    ):
        logger.info("Tool use detected. Committing memory.")
        commit_memory()

    if not skip_db:
        if initial_db_message is not None:
            save_message_and_update_index(curr, initial_db_message)
        bot_meta: dict = {"message_params": message_params}
        if claude_calls:
            pricing = tool_sampler.MODEL_PRICING.get(model_name)
            subagent_total_cost = sum(
                u.get("estimated_cost_usd", 0) or 0 for u in result.subagent_usages
            )
            bot_meta["usage"] = {
                "calls": [c.to_usage_dict(pricing) for c in claude_calls],
                "estimated_cost_usd": (cost or 0) + subagent_total_cost,
                "cost_breakdown_usd": tool_sampler.cost_breakdown(
                    claude_calls, model_name
                ),
            }
            if result.subagent_usages:
                bot_meta["usage"]["subagent_usages"] = result.subagent_usages
        db_message = DbMessage(
            chat_id=chat_id,
            created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
            user_id=BOT_USER_ID,
            message="USE_CONTENT_FROM_META",
            meta=bot_meta,
        )
        save_message_and_update_index(curr, db_message)

    archive_marked_messages(curr, chat_id)

    add_debug_message_to_queue(
        message_params, skip_first_n=hooks.num_messages_sent_to_debug_chat
    )
    ensure(application.job_queue).run_once(maybe_send_messages_to_debug_chat, when=1)


def compute_token_counts(
    client: Anthropic,
    system: str,
    db_messages: list[DbMessage],
    message_params: list[MessageParam],
) -> dict[str, Any]:
    messages_str = "\n".join(
        line
        for message_params in convert_db_messages_to_claude_messages(db_messages)
        for line in render_mesage_param_exact(message_params)
    )
    sizes = {
        "system_prompt": {
            "bytes": len(system),
        },
        "messages": {
            "bytes_fake": len(messages_str),
            "count": len(message_params),
            "count_db": len(db_messages),
        },
    }
    return sizes

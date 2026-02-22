import asyncio
import base64
import datetime
import io
import json
import logging
import os
import time
from dataclasses import asdict, is_dataclass
from typing import Any

from anthropic import Anthropic
from anthropic.types import (
    MessageParam,
)
from telegram import Bot, Update, constants
from telegram.ext import Application, CallbackContext

from yarvis_ptb import tool_sampler
from yarvis_ptb.chat_config import ChatConfig
from yarvis_ptb.debug_chat import (
    MessageAsFile,
    add_debug_message_to_queue,
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
    build_interruptable_scope,
    get_anthropic_client,
    reply_maybe_markdown,
    typing_action,
)
from yarvis_ptb.settings import (
    BOT_USER_ID,
    DEFAULT_TIMEZONE,
    HISTORY_LENGTH_LONG_SHRINKING_FACTOR,
    HISTORY_LENGTH_LONG_TOKENS,
    HISTORY_LENGTH_LONG_TURNS,
    LARGE_MESSAGE_SIZE_THRESHOLD,
    USER_ID_MAP,
)
from yarvis_ptb.settings.main import CONFIGURED_CHATS
from yarvis_ptb.storage import (
    IMAGE_B64_META_FIELD,
    DbMessage,
    Invocation,
    VariablesForChat,
    archive_marked_messages,
    get_messages,
    get_scheduled_invocations,
    mark_message_for_archive,
    set_non_active_invocation,
)
from yarvis_ptb.util import RateController, ensure

COMPLEX_CHAT_LOCK = asyncio.Lock()

COMPLEX_CHAT_PUT_CONTEXT_AT_THE_BEGINNING = False
COMPLEX_CHAT_PUT_CONTEXT_AT_THE_END = True


DEFAULT_COMPLEX_CHAT_CONFIG = ChatConfig(
    prompt_name=COMPLEX_ANTON_PROMPT,
    is_complex_chat=True,
    memory_access=True,
    tool_filter="all",
    max_history_length_turns_override=HISTORY_LENGTH_LONG_TURNS,
    tool_only_messaging=True,
    tool_result_truncation_after_n_turns=5,
)


logger = logging.getLogger(__name__)


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


def get_chat_config(auth: AuthInfo) -> ChatConfig:
    if auth.is_root_user_debug_chat or auth.is_root_user_complex_chat:
        chat_config = DEFAULT_COMPLEX_CHAT_CONFIG
    else:
        assert auth.group_chat_name, auth
        chat_config = CONFIGURED_CHATS[auth.group_chat_name]
    return chat_config


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
    chat_config = get_chat_config(auth)

    chat_vars = VariablesForChat(curr=curr, chat_id=chat_id)
    logger.info(f"{chat_vars.variables=}")

    if chat_vars.get(chat_vars.KILL_SWITCH):
        await update.message.reply_text("Kill switch on")
        return

    # Check for any active reply_timeout invocations and deactivate them
    # since the user has replied
    scheduled_invocations = get_scheduled_invocations(curr, chat_id)
    for invocation in scheduled_invocations:
        if invocation.meta.get("invocation_type") == "reply_timeout":
            logger.info(
                f"Deactivating reply timeout invocation: {invocation.scheduled_id}"
            )
            set_non_active_invocation(curr, invocation)

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

    await process_multi_message_claude_invocation(
        curr=curr,
        application=context.application,
        bot=context.bot,
        chat_id=chat_id,
        chat_config=chat_config,
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
    chat_config: ChatConfig,
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
            chat_config=chat_config,
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
    chat_config: ChatConfig,
    initial_db_message: DbMessage | None = None,
    skip_db: bool = False,
    # Datetime to use for "now" for the last user message and the context.
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
            chat_config=chat_config,
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
    chat_config: ChatConfig,
    initial_db_message: DbMessage | None = None,
    skip_db: bool = False,
    forced_now_date: datetime.datetime | None = None,
    telegram_message_id: int | None = None,
):
    start = time.monotonic()
    client = get_anthropic_client()
    do_streaming = (
        invocation.invocation_type != "schedule" and not chat_config.tool_only_messaging
    )
    if invocation.invocation_type != "schedule" and invocation.reply_to_message_id:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=invocation.reply_to_message_id,
            reaction=constants.ReactionEmoji.EYES,
        )  # type: ignore

    if do_streaming:
        # No notification. WIll notify final message only.
        output_message = await reply_maybe_markdown(
            bot, chat_id, "**Processing...**", disable_notification=True
        )
    else:
        output_message = None

    now_date = (forced_now_date or datetime.datetime.now()).astimezone(DEFAULT_TIMEZONE)

    max_history_length_turns = chat_config.max_history_length_turns
    db_messages = get_messages(curr, chat_id=chat_id, limit=max_history_length_turns)
    if initial_db_message is not None:
        # Re-do timestamp so that messages that were sent during previous message generation are sill sorted properly.
        initial_db_message.created_at = now_date
        add_debug_message_to_queue("**ANTON:**\n" + initial_db_message.message)
        db_messages = [*db_messages, initial_db_message][-max_history_length_turns:]

    scheduled_invocations = get_scheduled_invocations(curr, chat_id)

    # Send the message to Claude
    logger.debug("Sending message to Claude")
    system, message_params = build_claude_input(
        db_messages,
        chat_config,
        invocation=invocation,
        put_context_at_the_beginning=COMPLEX_CHAT_PUT_CONTEXT_AT_THE_BEGINNING,
        put_context_at_the_end=COMPLEX_CHAT_PUT_CONTEXT_AT_THE_END,
        scheduled_invocations=scheduled_invocations,
        forced_now_date=now_date,
    )

    # Note, this context is only sent for debug; we are trying to make it match
    # whaterver is used in build_claude_input, but they could deiverge.
    context_message = build_context_info(
        invocation=invocation,
        scheduled_invocations=scheduled_invocations,
        chat_config=chat_config,
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

    rate_controller = RateController(wait_between_events_secs=1.0)
    num_messages_sent_to_debug_chat = 0

    async def update_message_output(message_params: list[MessageParam]) -> None:
        nonlocal output_message
        nonlocal num_messages_sent_to_debug_chat
        if rate_controller.can_run():
            if output_message is not None:
                response_text = (
                    render_claude_response_short(message_params, remove_thinking=False)
                    + "\n**processing...**"
                )

                # In tool_only_messaging mode, format the response as a quote
                if chat_config.tool_only_messaging:
                    response_text = format_as_quote(response_text)

                output_message = await reply_maybe_markdown(
                    bot,
                    chat_id,
                    message=output_message,
                    text=response_text,
                )
            new_complete_debug_messages = (
                len(message_params) - num_messages_sent_to_debug_chat - 1
            )
            if new_complete_debug_messages:
                add_debug_message_to_queue(
                    message_params[:-1], skip_first_n=num_messages_sent_to_debug_chat
                )
                num_messages_sent_to_debug_chat += new_complete_debug_messages
                ensure(application.job_queue).run_once(
                    maybe_send_messages_to_debug_chat, when=1
                )

    sizes = compute_token_counts(
        client,
        system,
        db_messages,
        message_params,
    )

    pre_call_time = time.monotonic() - start

    with build_interruptable_scope(chat_id, message_id=telegram_message_id) as scope:
        logger.info(
            f"Divining into tool_sampler.process_query: {chat_id=} {telegram_message_id=}"
        )
        (
            message_params,
            maybe_claude_calls,
            tool_init_time,
        ) = await tool_sampler.process_query(
            client,
            curr=curr,
            bot=bot,
            chat_config=chat_config,
            chat_id=chat_id,
            system=system,
            messages=message_params,
            on_update=update_message_output,
            scope=scope,
            job_queue=application.job_queue,
        )

    prompt_size: int | None
    if (claude_calls := maybe_claude_calls) is not None:
        prompt_size = claude_calls[0].num_prompt_tokens if claude_calls else -2
        sizes["claude_calls"] = claude_calls
        sizes["tool_init_time"] = tool_init_time
        sizes["pre_call_time"] = pre_call_time
        add_debug_message_to_queue(
            f"**SIZES:**:\n```\n{json.dumps(sizes, indent=2, sort_keys=True, cls=DataclassJSONEncoder)}\n```",
        )
    else:
        prompt_size = None

    if message_params:
        if chat_config.tool_only_messaging:
            if output_message is not None:
                await bot.delete_message(chat_id=chat_id, message_id=output_message.id)  # type: ignore
        else:
            # Send short version of the message to the main chat.
            response_text = render_claude_response_short(message_params)
            await reply_maybe_markdown(
                bot,
                chat_id,
                response_text,
                message=output_message,
                final_update=True,
            )
    else:
        # Empty messages params -> interruption
        if output_message is not None:
            await bot.delete_message(chat_id=chat_id, message_id=output_message.id)  # type: ignore
        if invocation.reply_to_message_id:
            await bot.set_message_reaction(
                chat_id=chat_id,
                message_id=invocation.reply_to_message_id,
                reaction=constants.ReactionEmoji.SHRUG,
            )  # type: ignore

    if chat_config.memory_access and any(
        x["type"] == "tool_use"
        for mp in message_params
        if isinstance(mp["content"], list)
        for x in mp["content"]
    ):
        logger.info("Tool use detected. Committing memory.")
        commit_memory()

    if not skip_db:
        if initial_db_message is not None:
            # Saving to db only at the end.
            save_message_and_update_index(curr, initial_db_message)
        db_message = DbMessage(
            chat_id=chat_id,
            created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
            user_id=BOT_USER_ID,
            message="USE_CONTENT_FROM_META",
            meta={"message_params": message_params},
        )
        save_message_and_update_index(curr, db_message)

    archive_marked_messages(curr, chat_id)

    # Send full version to the debug chat.
    add_debug_message_to_queue(
        message_params, skip_first_n=num_messages_sent_to_debug_chat
    )
    ensure(application.job_queue).run_once(maybe_send_messages_to_debug_chat, when=1)

    if (
        prompt_size is not None
        and prompt_size > HISTORY_LENGTH_LONG_TOKENS
        and invocation.invocation_type != "context_overflow"
    ):
        # Calculate message sizes and the total size.
        message_sizes = [
            (msg, len(json.dumps(msg.meta or {}) + msg.message)) for msg in db_messages
        ]
        total_size = sum(size for _, size in message_sizes)
        large_message_threshold = total_size * LARGE_MESSAGE_SIZE_THRESHOLD

        # Check for large messages
        large_messages = [
            (msg, size) for msg, size in message_sizes if size > large_message_threshold
        ]

        if large_messages:
            # If we have messages >30%, only delete those
            ids_to_archive = [msg.message_id for msg, _ in large_messages]
            large_msg_info = [
                f"{size/1024:.1f}KB ({size/total_size*100:.1f}%)"
                for _, size in large_messages
            ]
            archiving_string = f"Large messages found: {', '.join(large_msg_info)}"
        else:
            # Otherwise use proportional deletion
            num_messages_to_kill = int(
                len(db_messages) * HISTORY_LENGTH_LONG_SHRINKING_FACTOR
            )
            ids_to_archive = [
                db_message.message_id
                for db_message in db_messages[:num_messages_to_kill]
            ]
            archiving_string = (
                f"Proportional truncation: {num_messages_to_kill}/{len(db_messages)}"
            )

        for message_id in ids_to_archive:
            assert message_id is not None, ids_to_archive
            mark_message_for_archive(curr, chat_id, message_id)
        await reply_maybe_markdown(
            bot,
            chat_id,
            f"**SYSTEM** Invocation: prompt truncation {prompt_size=} {HISTORY_LENGTH_LONG_TOKENS=};\n"
            f"Archiving strategy: {archiving_string}",
        )
        await _process_multi_message_claude_invocation_no_lock(
            curr=curr,
            application=application,
            bot=bot,
            chat_id=chat_id,
            chat_config=chat_config,
            invocation=Invocation(invocation_type="context_overflow"),
        )


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

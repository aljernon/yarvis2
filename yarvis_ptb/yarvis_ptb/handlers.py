import asyncio
import datetime
import logging
import os
import pprint
import subprocess
import traceback
from dataclasses import asdict, dataclass
from tempfile import NamedTemporaryFile, gettempprefix
from typing import Awaitable

import psycopg2
from anthropic.types import MessageParam
from telegram import Message, Update
from telegram.ext import Application, CallbackContext, ContextTypes
from typing_extensions import Callable

from yarvis_ptb.complex_chat import (
    DEFAULT_AGENT_CONFIG,
    handle_message_root_user_assistant,
    process_multi_message_claude_invocation,
)
from yarvis_ptb.daily_agent_update import run_daily_agent_update, should_run_dau
from yarvis_ptb.daily_self_reflect import (
    run_auto_reflect,
    run_force_reflect,
    should_auto_reflect,
    should_daily_reflect,
)
from yarvis_ptb.debug_chat import (
    add_debug_message_to_queue,
    maybe_send_messages_to_debug_chat,
)
from yarvis_ptb.on_disk_memory import read_autoload_memory
from yarvis_ptb.prompting import (
    build_claude_input,
    build_context_info,
    convert_db_messages_to_claude_messages,
    render_mesage_param_exact,
)
from yarvis_ptb.ptb_util import (
    AuthInfo,
    auth_decorator,
    auth_decorator_all_complex_chats,
    auth_decorator_complex_chat,
    get_anthropic_client,
    hard_restart,
    interrupt_all,
    reply_maybe_markdown,
)
from yarvis_ptb.settings import (
    DEFAULT_TIMEZONE,
    HISTORY_LENGTH_TURNS,
    ROOT_USER_ID,
    SYSTEM_USER_ID,
)
from yarvis_ptb.settings.main import (
    CLAUDE_MODEL_NAME,
    CONFIGURED_CHATS,
    HISTORY_LENGTH_LONG_TURNS,
    KNOWN_USER_PRIVATE_CHAT_CONFIGS,
)
from yarvis_ptb.storage import (
    DbMessage,
    Invocation,
    VariablesForChat,
    advance_schedule,
    deactivate_schedule,
    get_memories,
    get_messages,
    get_schedule_by_id,
    get_schedules,
    hide_message_history,
    mark_message_for_archive,
    save_message,
)
from yarvis_ptb.tools.scheduling_tools import compute_next_run
from yarvis_ptb.util import ensure
from yarvis_ptb.whisper_transcription import transcribe_voice_message

logger = logging.getLogger(__name__)

TelegramHandler = Callable[[Update, CallbackContext], Awaitable[None]]

HANDLER_REGISTRY: list["RegisteredCommandHandler"] = []


@dataclass
class RegisteredCommandHandler:
    name: str
    description: str
    handler: TelegramHandler

    @classmethod
    def register(
        cls, cmd: str | None = None, description: str | None = None
    ) -> Callable:
        def wrapper(handler: TelegramHandler) -> TelegramHandler:
            global HANDLER_REGISTRY
            nonlocal cmd
            assert handler.__name__.startswith("handler_"), handler.__name__
            cmd = cmd or handler.__name__.removeprefix("handler_")
            HANDLER_REGISTRY.append(
                cls(
                    name=cmd,
                    description=description or cmd,
                    handler=handler,
                )
            )
            return handler

        return wrapper


def call_claude(
    application: Application, system: str, history: list[MessageParam], config=None
) -> str:
    client = get_anthropic_client()
    model = CLAUDE_MODEL_NAME
    if config:
        model = config.model_name

    response = client.messages.create(
        system=system,
        model=model,
        max_tokens=1000,
        messages=history,
    )

    # Extract Claude's response
    if not response.content:
        logger.error(f"empty response {response=}")
        return "<Claude API returned empty message>"
    return response.content[0].text  # pyright: ignore


@RegisteredCommandHandler.register("cal", "Create a calendar event")
@auth_decorator
async def handler_calendar(auth: AuthInfo, update: Update, context: CallbackContext):
    if not auth.is_root_user_complex_chat:
        return

    user_message = ensure(update.message).text

    client = get_anthropic_client()

    user_message = f"""

Below a description of event from a user. I'd like you to parse it and output a link to create an event in google calendar. Be succinct.

```
{user_message}
```
"""

    try:
        # Send the message to Claude
        logger.debug("Sending message to Claude")
        # Default to standard model for this simple helper
        response = client.messages.create(
            model=CLAUDE_MODEL_NAME,
            max_tokens=1000,
            messages=[{"role": "user", "content": user_message}],
        )

        # Extract Claude's response
        claude_response = response.content[0].text  # pyright: ignore
        logger.debug(f"Received response from Claude: {claude_response[:20]}...")

        # Send Claude's response back to the user
        await ensure(update.message).reply_text(claude_response)
        logger.info("Sent Claude's response back to user")
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}")
        await ensure(update.message).reply_text(f"An error occurred: {str(e)}")


@RegisteredCommandHandler.register(description="Interupt current execution")
@auth_decorator_all_complex_chats
async def handler_interrupt(update: Update, context: CallbackContext):
    logger.info("Entering handler_interupt")
    scopes = interrupt_all()
    await reply_maybe_markdown(
        context.bot,
        ensure(update.message).chat_id,
        f"Sent interrupt to the following scopes: {scopes}",
    )


@RegisteredCommandHandler.register(description="Show system prompt and histrory")
@auth_decorator_complex_chat
async def handler_show_prompt(update: Update, context: CallbackContext):
    with context.bot_data["conn"].cursor() as curr:
        # chat_id = ensure(update.message).chat_id
        messages = get_messages(curr, chat_id=ROOT_USER_ID, limit=HISTORY_LENGTH_TURNS)
        scheduled_invocations = get_schedules(curr, ROOT_USER_ID)

    agent_config = DEFAULT_AGENT_CONFIG
    system_prompt, history = build_claude_input(
        messages=messages,
        rendering_config=agent_config.rendering,
        scheduled_invocations=scheduled_invocations,
    )
    formatted = [system_prompt]
    for rec in history:
        formatted.extend(render_mesage_param_exact(rec))

    await send_text_as_file(
        ensure(update.message), "system_prompt", "\n".join(formatted)
    )


@RegisteredCommandHandler.register(description="Show last message full")
@auth_decorator_complex_chat
async def handler_last(update: Update, context: CallbackContext):
    with context.bot_data["conn"].cursor() as curr:
        chat_id = ensure(update.message).chat_id
        messages = get_messages(curr, chat_id=ROOT_USER_ID, limit=HISTORY_LENGTH_TURNS)

    if not messages:
        return

    message_params = convert_db_messages_to_claude_messages(messages[-1:])
    chunks = []
    for rec in message_params:
        chunks.extend(render_mesage_param_exact(rec))
    await reply_maybe_markdown(context.bot, chat_id, "\n".join(chunks))


@RegisteredCommandHandler.register()
@auth_decorator
async def handler_show_auth(auth: AuthInfo, update: Update, context: CallbackContext):
    if not auth.is_root_in_the_chat:
        return
    auth_str = pprint.pformat(asdict(auth), indent=2)
    response = f"```python\n{auth_str}\n```"
    chat_id = ensure(update.message).chat_id
    bot = context.bot
    await reply_maybe_markdown(bot, chat_id, response)


async def send_text_as_file(
    message: Message, prefix: str, content: str, ext: str = ".txt"
):
    date = datetime.datetime.now().date()
    with NamedTemporaryFile(
        mode="w", prefix=f"{prefix}_{date}_{gettempprefix()}", suffix=ext
    ) as stream:
        stream.write(content)
        stream.flush()
        await message.reply_document(stream.name)


@RegisteredCommandHandler.register()
@auth_decorator_complex_chat
async def handler_show_context(update: Update, context: CallbackContext):
    with context.bot_data["conn"].cursor() as curr:
        memories = get_memories(curr, ROOT_USER_ID)
        scheduled_invocations = get_schedules(curr, ROOT_USER_ID)

    context_info = build_context_info(
        rendering_config=DEFAULT_AGENT_CONFIG.rendering,
        scheduled_invocations=scheduled_invocations,
        invocation=Invocation(invocation_type="reply"),
    )
    await send_text_as_file(ensure(update.message), "context", context_info)


@RegisteredCommandHandler.register()
@auth_decorator
async def handler_erase_history(
    auth: AuthInfo, update: Update, context: CallbackContext
):
    if not auth.is_message_from_root:
        return
    message = ensure(update.message)
    with context.bot_data["conn"].cursor() as curr:
        hide_message_history(curr, message.chat_id)
    await message.reply_text("Set message visibility to false")


@RegisteredCommandHandler.register()
@auth_decorator_complex_chat
async def handler_compress_history(update: Update, context: CallbackContext):
    message = ensure(update.message)
    chat_id = message.chat_id
    with context.bot_data["conn"].cursor() as curr:
        db_messages = get_messages(
            curr, chat_id=chat_id, limit=HISTORY_LENGTH_LONG_TURNS
        )
        for db_message in db_messages:
            mark_message_for_archive(curr, chat_id, ensure(db_message.message_id))
        await message.reply_text(
            f"Marked {len(db_messages)} messages for archive. You can now compress them."
        )
        await process_multi_message_claude_invocation(
            curr,
            context.application,
            context.bot,
            agent_config=DEFAULT_AGENT_CONFIG,
            chat_id=chat_id,
            invocation=Invocation(invocation_type="context_overflow"),
            initial_db_message=None,
        )


@RegisteredCommandHandler.register(
    description="Print last N (arg) knowledge files (default 5)"
)
@auth_decorator_complex_chat
async def handler_memory(update: Update, context: CallbackContext):
    memories = list(read_autoload_memory().items())

    try:
        limit = int(ensure(ensure(update.message).text).split()[-1])
    except ValueError:
        limit = 5
    if limit <= 0:
        limit = len(memories) + 1

    for name, text in memories[-limit:]:
        lines = []
        lines.append(f"=== {name}")
        lines.append(text)
        await ensure(update.message).reply_text("\n".join(lines))
        await asyncio.sleep(1.0)


@RegisteredCommandHandler.register(
    description="Try Sync Core Knowledge Repository and logseq"
)
@auth_decorator_complex_chat
async def handler_sync(update: Update, context: CallbackContext):
    try:
        subprocess.check_call(["git", "pull"], cwd="/app/repo")
        subprocess.check_call(["git", "pull"], cwd="/app/core_knowledge")
    except Exception as e:
        # Get full traceback
        error_traceback = "".join(
            traceback.format_exception(type(e), e, e.__traceback__)
        )

        # Log the full traceback
        logger.exception(f"An error occurred:\n{error_traceback}")

        # Send user-friendly error message with traceback
        error_message = (
            f"An error occurred:\n\n"
            f"Error: {str(e)}\n"
            f"Traceback:\n```\n{error_traceback}```"
        )
        await reply_maybe_markdown(
            context.bot, ensure(update.message).chat_id, error_message
        )
    else:
        await reply_maybe_markdown(
            context.bot,
            ensure(update.message).chat_id,
            "Core Knowledge Repository and repo synced",
        )


@RegisteredCommandHandler.register(description="Show active TODOs from logseq")
@auth_decorator_complex_chat
async def handler_todo(update: Update, context: CallbackContext):
    message = ensure(update.message)

    try:
        # Pull latest logseq updates
        subprocess.check_call(["git", "pull"], cwd="/app/logseq")

        # Search for active TODOs and other task items, excluding version and backup files
        result = subprocess.run(
            [
                "grep",
                "-r",
                "--include=*.md",
                "--exclude-dir=version-files",
                "--exclude-dir=bak",
                "-n",
                r"^\s*-\s*\(TODO\|LATER\|DOING\|WAITING\)\s",
                "/app/logseq/",
            ],
            capture_output=True,
            text=True,
            shell=False,
        )

        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().split("\n")

            # Group by file and format nicely
            todos_by_file = {}
            for line in lines:
                if ":" in line:
                    # Split only on first two colons (file:line:content)
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        file_path, line_num, content = parts
                        file_name = os.path.basename(file_path)

                        # Clean up the file name (remove .md extension, format date)
                        if file_name.endswith(".md"):
                            file_name = file_name[:-3]
                        if file_name.startswith("2026_") or file_name.startswith(
                            "2025_"
                        ):
                            # Convert date format to readable
                            try:
                                date_obj = datetime.datetime.strptime(
                                    file_name, "%Y_%m_%d"
                                )
                                file_name = date_obj.strftime("%Y-%m-%d")
                            except ValueError:
                                pass  # Keep original name if parsing fails

                        if file_name not in todos_by_file:
                            todos_by_file[file_name] = []
                        todos_by_file[file_name].append(content.strip())

            # Format response
            if todos_by_file:
                response_lines = ["**Active TODOs from Logseq:**\n"]

                # Sort files by date (most recent first)
                sorted_files = sorted(todos_by_file.keys(), reverse=True)

                for file_name in sorted_files:
                    response_lines.append(f"**{file_name}:**")
                    for todo in todos_by_file[file_name]:
                        response_lines.append(f"  {todo}")
                    response_lines.append("")  # Empty line between files

                response = "\n".join(response_lines)

                # If response is too long, truncate and mention
                if len(response) > 3000:
                    response = (
                        response[:2800] + "\n\n... (truncated, more TODOs available)"
                    )

                await reply_maybe_markdown(context.bot, message.chat_id, response)
            else:
                await reply_maybe_markdown(
                    context.bot, message.chat_id, "No active TODOs found in logseq."
                )
        else:
            await reply_maybe_markdown(
                context.bot, message.chat_id, "No active TODOs found in logseq."
            )

    except Exception as e:
        error_traceback = "".join(
            traceback.format_exception(type(e), e, e.__traceback__)
        )
        logger.exception(f"An error occurred in todo handler:\n{error_traceback}")

        error_message = f"An error occurred while fetching TODOs:\n\n{str(e)}"
        await reply_maybe_markdown(context.bot, message.chat_id, error_message)


@RegisteredCommandHandler.register(
    description="Run self-reflection on recent conversations"
)
@auth_decorator_all_complex_chats
async def handler_force_reflect(update: Update, context: CallbackContext):
    chat_id = ensure(update.message).chat_id
    assert chat_id == ROOT_USER_ID
    message = ensure(update.message)

    # Parse optional max_turns argument: /force_reflect [N]
    max_turns = None
    if context.args:
        try:
            max_turns = int(context.args[0])
        except ValueError:
            await message.reply_text("Usage: /force_reflect [max_turns]")
            return

    status_msg = await message.reply_text(
        f"Reflecting (last {max_turns} turns)..." if max_turns else "Reflecting..."
    )
    try:
        with context.bot_data["conn"].cursor() as curr:
            response_text = await run_force_reflect(
                curr, chat_id, context.bot, max_turns=max_turns
            )
            context.bot_data["conn"].commit()
        await reply_maybe_markdown(context.bot, chat_id, response_text)
    except Exception as e:
        logger.exception("Reflection failed")
        await reply_maybe_markdown(context.bot, chat_id, f"Reflection failed: {e}")


@RegisteredCommandHandler.register(
    description="Kill the bot (revive via DB + heroku restart)"
)
@auth_decorator
async def handler_kill(auth: AuthInfo, update: Update, context: CallbackContext):
    if not auth.is_root_user_complex_chat:
        return
    message = ensure(update.message)
    with context.bot_data["conn"].cursor() as curr:
        # Delete any per-chat overrides that could shadow the global
        curr.execute(
            "DELETE FROM chat_variables WHERE name = 'KILL_SWITCH' AND chat_id IS NOT NULL"
        )
        chat_vars = VariablesForChat(curr, ROOT_USER_ID)
        chat_vars.set_global(chat_vars.KILL_SWITCH, True)
        context.bot_data["conn"].commit()
    await message.reply_text("Kill switch ON — restarting into dead loop")
    hard_restart()


@RegisteredCommandHandler.register(
    description="Trigger a schedule immediately by ID (e.g. /trigger 5)"
)
@auth_decorator
async def handler_trigger(auth: AuthInfo, update: Update, context: CallbackContext):
    if not auth.is_root_user_complex_chat:
        return
    message = ensure(update.message)
    args = message.text.split()[1:] if message.text else []
    if len(args) != 1 or not args[0].isdigit():
        await message.reply_text("Usage: /trigger <schedule_id>")
        return
    schedule_id = int(args[0])
    with context.bot_data["conn"].cursor() as curr:
        sched = get_schedule_by_id(curr, schedule_id)
        if sched is None or not sched.is_active:
            await message.reply_text(f"No active schedule with id {schedule_id}")
            return
        await message.reply_text(f"Triggering schedule {schedule_id}: {sched.title}")
        await process_multi_message_claude_invocation(
            curr,
            application=context.application,
            bot=context.bot,
            chat_id=sched.chat_id,
            agent_config=DEFAULT_AGENT_CONFIG,
            invocation=Invocation(invocation_type="schedule", db_invocation=sched),
        )


@RegisteredCommandHandler.register()
@auth_decorator_complex_chat
async def handler_restart(update: Update, context: CallbackContext):
    hard_restart()


@auth_decorator
async def handle_voice_message(
    auth: AuthInfo, update: Update, context: CallbackContext
) -> None:
    # Voice messages allowed in 1-on-1 chats: private chat with root, or
    # configured group chats with trigger_condition="root" (e.g. logseq)
    chat_config = CONFIGURED_CHATS.get(auth.group_chat_name or "")
    is_root_triggered_group = (
        auth.is_message_from_root
        and chat_config is not None
        and chat_config.trigger_condition == "root"
    )
    if not (auth.is_root_user_complex_chat or is_root_triggered_group):
        return
    if not update.message:
        logger.error("No message")
        return
    if not update.message.voice:
        logger.error("No voice message")
        return
    logger.info(f"Transcribing {update.message=}")
    text = await transcribe_voice_message(bot=context.bot, voice=update.message.voice)
    object.__setattr__(update.message, "text", text)
    await reply_maybe_markdown(
        context.bot, update.message.chat_id, f"**Transcribed:**\n{update.message.text}"
    )
    return await _handle_message(auth, update, context, is_voice=True)


@auth_decorator
async def handle_message(
    auth: AuthInfo, update: Update, context: CallbackContext
) -> None:
    logger.info(f"handle_message called - update.message: {update.message}")
    return await _handle_message(auth, update, context, is_voice=False)


async def _handle_message(
    auth: AuthInfo, update: Update, context: CallbackContext, is_voice: bool
) -> None:
    logger.info(
        f"_handle_message called - is_voice: {is_voice}, "
        f"has_document: {bool(update.message and update.message.document)}, "
        f"has_text: {bool(update.message and update.message.text)}, "
        f"has_photo: {bool(update.message and update.message.photo)}"
    )
    if not auth.known_user:
        logging.warning(f"Not answering to unknown user {auth.user=}")
        return
    with context.bot_data["conn"].cursor() as curr:
        try:
            if auth.is_root_user_complex_chat or auth.is_root_user_debug_chat:
                await handle_message_root_user_assistant(
                    curr, auth, update, context, is_voice=is_voice
                )
            elif (
                not auth.group_chat_id
                and auth.user_id in KNOWN_USER_PRIVATE_CHAT_CONFIGS
            ):
                await handle_message_root_user_assistant(
                    curr, auth, update, context, is_voice=is_voice
                )
            elif auth.group_chat_name and (
                chat_config := CONFIGURED_CHATS.get(auth.group_chat_name)
            ):
                if chat_config.trigger_condition == "root":
                    need_to_reply = auth.is_message_from_root
                else:
                    assert not is_voice
                    assert (
                        not chat_config.is_complex_chat
                    ), "Only root chat could be complex chat"
                    assert (
                        chat_config.chat_id is not None
                    ), "Misconfigured chat. Need chat_id for trigger_condition != root"
                    if chat_config.chat_id != auth.group_chat_id:
                        logging.error(f"UNKNOWN CHAT, NOT replying {auth=}")
                        return

                    if chat_config.trigger_condition == "mention":
                        need_to_reply = auth.bot_mentioned or auth.is_reply_to_bot
                    elif chat_config.trigger_condition == "all":
                        need_to_reply = True
                    else:
                        raise ValueError(
                            f"Unknown trigger_condition: {chat_config.trigger_condition}"
                        )
                logger.info(
                    f"Found chat config:\n{auth=}\n{chat_config=}\n{need_to_reply=}"
                )
                if need_to_reply:
                    await handle_message_root_user_assistant(
                        curr, auth, update, context, is_voice=is_voice
                    )
                elif (
                    chat_config.trigger_condition != "root"
                    and update.message
                    and update.message.text
                ):
                    # Saving all text messages we see.
                    logger.info("Saving message without replying")
                    initial_db_message = DbMessage(
                        chat_id=ensure(auth.group_chat_id),
                        created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
                        user_id=ensure(update.message.from_user).id,
                        message=update.message.text,
                    )
                    save_message(curr, initial_db_message)
                else:
                    logger.info("Ignoring the message completely (either photo or wtf)")
            else:
                logger.error(
                    f"UNKNOWN CHAT, NOT replying{auth.group_chat_name=} {sorted(CONFIGURED_CHATS)=}\n{auth=}"
                )
                return
        except Exception as e:
            # Get full traceback
            error_traceback = "".join(
                traceback.format_exception(type(e), e, e.__traceback__)
            )

            # Log the full traceback
            logger.exception(f"An error occurred:\n{error_traceback}")

            # Send user-friendly error message with traceback
            error_message = (
                f"An error occurred:\n\n"
                f"Error: {str(e)}\n"
                f"Traceback:\n```\n{error_traceback}```"
            )
            await reply_maybe_markdown(
                context.bot, ensure(update.message).chat_id, error_message
            )


async def app_start_callback(context: ContextTypes.DEFAULT_TYPE):
    # Add a system message to the complex chat that the bot has restated.
    with context.bot_data["conn"].cursor() as curr:
        git_commit = os.environ.get(
            "HEROKU_BUILD_COMMIT", os.environ.get("HEROKU_SLUG_COMMIT", "UNK")
        )[:6]
        db_message = DbMessage(
            chat_id=ROOT_USER_ID,
            created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
            user_id=SYSTEM_USER_ID,
            message=f"The container restarted. Code git commit: {git_commit}",
        )
        add_debug_message_to_queue(f"**SYSTEM**\n{db_message.message}")
        await maybe_send_messages_to_debug_chat(context.application)
        save_message(curr, db_message)


async def callback_minute(context: ContextTypes.DEFAULT_TYPE):
    try:
        with context.bot_data["conn"].cursor() as curr:
            pass
    except psycopg2.InterfaceError as e:
        if "connection already closed" in str(e):
            logger.exception(f"Connection closed: {e}")
            hard_restart()
    with context.bot_data["conn"].cursor() as curr:
        now = datetime.datetime.now(DEFAULT_TIMEZONE)
        schedules = get_schedules(curr)
        deltas = [
            (sched, (sched.next_run_at - now).total_seconds()) for sched in schedules
        ]
        logger.debug(f"Schedules: {now=} {schedules=} {deltas=}")
        for sched, delta in deltas:
            if delta <= 0:
                logger.info(
                    f"Invoking schedule {sched.schedule_id} for chat {sched.chat_id}"
                )
                if sched.schedule_type == "at":
                    deactivate_schedule(curr, sched)
                else:
                    # 'cron' or 'every' — advance to next run
                    next_run = compute_next_run(sched, now)
                    advance_schedule(curr, sched, next_run)
                # Not implemented for other chats.
                assert sched.chat_id == ROOT_USER_ID, sched
                # Build system message so history shows what triggered this invocation.
                invocation_details = f"Scheduled invocation: {sched.title}"
                if sched.schedule_type == "at":
                    invocation_details += (
                        f" (one-time, scheduled for {sched.next_run_at.isoformat()})"
                    )
                else:
                    invocation_details += (
                        f" ({sched.schedule_type}: {sched.schedule_spec})"
                    )
                if sched.context:
                    invocation_details += f"\nContext: {sched.context}"
                invocation_system_message = DbMessage(
                    chat_id=sched.chat_id,
                    created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
                    user_id=SYSTEM_USER_ID,
                    message=invocation_details,
                )
                await process_multi_message_claude_invocation(
                    curr,
                    application=context.application,
                    bot=context.bot,
                    chat_id=sched.chat_id,
                    agent_config=DEFAULT_AGENT_CONFIG,
                    invocation=Invocation(
                        invocation_type="schedule", db_invocation=sched
                    ),
                    initial_db_message=invocation_system_message,
                )
        # DAU: daily session rotation (2am)
        try:
            if should_run_dau(curr, ROOT_USER_ID):
                await run_daily_agent_update(
                    curr, ROOT_USER_ID, context.application, context.bot
                )
                context.bot_data["conn"].commit()
        except Exception:
            logger.exception("DAU failed")
            add_debug_message_to_queue(
                f"**DAU FAILED**\n```\n{traceback.format_exc()}\n```"
            )
            await maybe_send_messages_to_debug_chat(context.application)

        # Auto-reflection check (idle-triggered or daily 4am)
        try:
            should_reflect = await should_auto_reflect(
                curr, ROOT_USER_ID
            ) or should_daily_reflect(curr, ROOT_USER_ID)
            if should_reflect:
                await run_auto_reflect(
                    curr, ROOT_USER_ID, context.application, context.bot
                )
                context.bot_data["conn"].commit()
        except Exception:
            logger.exception("Auto-reflect failed")
            add_debug_message_to_queue(
                f"**AUTO-REFLECT FAILED**\n```\n{traceback.format_exc()}\n```"
            )
            await maybe_send_messages_to_debug_chat(context.application)

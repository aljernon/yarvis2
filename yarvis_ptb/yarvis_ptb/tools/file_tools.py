import base64
import logging
import mimetypes
import pathlib

import telegram

from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class SendPhotoTool(LocalTool):
    def __init__(self, chat_id: int, bot: telegram.Bot):
        self.chat_id = chat_id
        self.bot = bot

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="send_photo_to_user",
            description="Send an image file (jpg, png, or gif) from /tmp folder to the user. This does NOT include the content of the file in the context",
            args=[
                ArgSpec(
                    name="file_path",
                    type=str,
                    description="Path to the image file",
                    is_required=True,
                )
            ],
        )

    async def _execute(self, *, file_path: str, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"

        file_path_obj = pathlib.Path(file_path)
        if file_path_obj.suffix.lower() not in (".jpg", ".jpeg", ".gif", ".png"):
            return ToolResult.error(
                f"Invalid file extension - must be jpg, png, or gif: {file_path}"
            )

        if not file_path_obj.exists():
            return ToolResult.error("File not found: {file_path}")

        if not file_path.startswith("/tmp/"):
            return ToolResult.error(f"File must be in /tmp folder: {file_path}")

        try:
            if file_path_obj.suffix.lower == ".gif":
                await self.bot.send_animation(chat_id=self.chat_id, animation=file_path)
                await self.bot.send_document(chat_id=self.chat_id, document=file_path)
            else:
                await self.bot.send_photo(chat_id=self.chat_id, photo=file_path)
            return ToolResult(f"Successfully sent image: {file_path}")
        except Exception as e:
            logger.exception(f"Error sending image: {e}")
            return ToolResult.error(f"Error sending image: {str(e)}")


class SendFileTool(LocalTool):
    def __init__(self, chat_id: int, bot: telegram.Bot):
        self.chat_id = chat_id
        self.bot = bot

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="send_file_to_user",
            description="Send any file from /tmp folder to the user. This does NOT include the content of the file in the context",
            args=[
                ArgSpec(
                    name="file_path",
                    type=str,
                    description="Path to the file to send",
                    is_required=True,
                )
            ],
        )

    async def _execute(self, *, file_path: str, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"

        if not file_path.startswith("/tmp/"):
            return ToolResult.error(f"File must be in /tmp folder: {file_path}")

        if not pathlib.Path(file_path):
            return ToolResult.error("File not found: {file_path}")

        try:
            await self.bot.send_document(chat_id=self.chat_id, document=file_path)
            return ToolResult(f"Successfully sent file: {file_path}")
        except Exception as e:
            logger.exception(f"Error sending file: {e}")
            return ToolResult.error(
                f"Error sending file: {str(e)}; this is most likely bug in the bot. Report to Anton"
            )


class SeePhotoTool(LocalTool):
    def __init__(self, chat_id: int | None, bot: telegram.Bot):
        self.chat_id = chat_id
        self.bot = bot

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="add_image_file_from_path_to_context",
            description="Load an image from file system  and put the content in the context. The user will not see the file. Must be in /tmp folder. This could only be used for images on filesystem, e.g., downloaded files and screenshots.",
            args=[
                ArgSpec(
                    name="file_path",
                    type=str,
                    description="Path to the image file",
                    is_required=True,
                )
            ],
        )

    async def _execute(self, *, file_path: str, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"

        file_path_obj = pathlib.Path(file_path)
        if file_path_obj.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            return ToolResult.error(
                f"Invalid file extension - must be jpg, png, or gif: {file_path}"
            )

        if not file_path_obj.exists():
            return ToolResult.error(f"File not found: {file_path}")

        if not file_path.startswith("/tmp/"):
            return ToolResult.error(f"File must be in /tmp folder: {file_path}")

        datatype, _ = mimetypes.guess_type(file_path)
        if not datatype:
            return ToolResult.error(f"Unknown file type: {file_path}")

        with file_path_obj.open("rb") as image_buffer:
            b64_image = base64.b64encode(image_buffer.read()).decode()
        if len(b64_image) >= 5 * 1024 * 1024:
            return ToolResult.error(
                f"Image is too large for the Language model (more than 5Mb): {len(b64_image)} bytes"
            )
        if self.chat_id is not None:
            await self.bot.send_photo(chat_id=self.chat_id, photo=file_path)
        return ToolResult(
            f"content of: {file_path}",
            images=[{"type": "base64", "media_type": datatype, "data": b64_image}],
        )


def build_chat_send_file_tools(
    chat_id: int, bot: telegram.Bot, debug_chat_id: int | None
) -> list[LocalTool]:
    return [
        SendPhotoTool(chat_id, bot),
        SendFileTool(chat_id, bot),
        SeePhotoTool(debug_chat_id, bot),
    ]

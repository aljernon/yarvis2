import base64
import logging

from clam_ptb.storage import IMAGE_B64_META_FIELD, get_messages
from clam_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class SaveImageFromMessageTool(LocalTool):
    """Tool to save an image from a recent message to a file in /tmp"""

    def __init__(self, user_id: int, curr):
        self.user_id = user_id
        self.curr = curr

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="save_image_from_message",
            description="Save an image from a recent message to a file in /tmp folder. Searches recent messages in the database for images. Note, that the latest user's message not in the history yet, so you may need to use scheduling to access images there",
            args=[
                ArgSpec(
                    name="file_path",
                    type=str,
                    description="Path where to save the image in /tmp folder (e.g., '/tmp/my_image.jpg')",
                    is_required=True,
                ),
                ArgSpec(
                    name="message_index",
                    type=int,
                    description="Which recent message with image to use (0 = most recent, 1 = second most recent, etc.)",
                    is_required=False,
                ),
            ],
        )

    async def _execute(
        self, *, file_path: str, message_index: int = 0, **kwargs
    ) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"

        if not file_path.startswith("/tmp/"):
            return ToolResult.error(f"File must be in /tmp folder: {file_path}")

        # Get recent messages with images
        messages = get_messages(self.curr, chat_id=self.user_id, limit=50)

        # Find messages with images
        image_messages = []
        for msg in messages:
            if msg.meta and IMAGE_B64_META_FIELD in msg.meta:
                image_messages.append(msg)

        if not image_messages:
            return ToolResult.error("No recent messages with images found")

        if message_index >= len(image_messages):
            return ToolResult.error(
                f"Message index {message_index} out of range. Found {len(image_messages)} recent messages with images."
            )

        # Get the image data
        target_message = image_messages[message_index]
        image_b64 = target_message.meta[IMAGE_B64_META_FIELD]

        try:
            # Decode base64 image
            image_data = base64.b64decode(image_b64)

            # Save to file
            with open(file_path, "wb") as f:
                f.write(image_data)

            return ToolResult(f"Successfully saved image from message to: {file_path}")

        except Exception as e:
            logger.exception(f"Error saving image: {e}")
            return ToolResult.error(f"Error saving image: {str(e)}")


def build_image_tools(user_id: int, curr) -> list[LocalTool]:
    """Build image tools"""
    return [
        SaveImageFromMessageTool(user_id, curr),
    ]

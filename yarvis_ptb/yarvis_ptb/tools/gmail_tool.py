import asyncio
import json
import logging

from simplegmail import Gmail
from simplegmail.message import Message

from yarvis_ptb.settings import PROJECT_ROOT
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

CHUNK_SIZE = 2**12  # 4KB


def get_gmail():
    return Gmail(client_secret_file=str(PROJECT_ROOT / "credentials.json"))


class GmailToolBase(LocalTool):
    def __init__(self):
        self.gmail: Gmail

    async def init(self):
        self.gmail = get_gmail()


class GmailReadTool(GmailToolBase):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_read_mail",
            description=f"""Reads a specific email message content starting from given offset.
Returns JSON with fields:
- content: string ({CHUNK_SIZE} chars of message content starting from offset)
- total_length: int (total length of message content)
- has_more: boolean (whether there's more content after the returned slice)
- sender: string (email address of sender)
- subject: string (email subject)
- date: string (email date in ISO format)

Returns error if:
- Message ID not found
- Message has no plain text content
- Failed to read message content""",
            args=[
                ArgSpec(
                    name="message_id",
                    type=str,
                    description="The ID of the email message to read",
                ),
                ArgSpec(
                    name="offset",
                    type=int,
                    description="Starting position to read from (in characters)",
                    is_required=False,
                ),
            ],
        )

    async def _execute(
        self, *, message_id: str, offset: int = 0, **kwargs
    ) -> ToolResult:
        assert not kwargs, f"Unexpected arguments: {kwargs}"
        try:
            # Get all messages and find the one with matching ID
            message = get_message(self.gmail, message_id)

            if not message:
                return ToolResult.error(f"Message with ID {message_id} not found")

            content = message.plain
            if not content:
                if message.html:
                    import re

                    from bs4 import BeautifulSoup

                    soup = BeautifulSoup(message.html, "html.parser")
                    # Remove script and style elements
                    for element in soup(["script", "style"]):
                        element.decompose()
                    # Get text with newlines for structure
                    content = soup.get_text(separator="\n")
                    # Clean up excessive newlines
                    content = re.sub(r"\n\s*\n", "\n\n", content)
                    content = content.strip()
                else:
                    return ToolResult.error(
                        "Message has no content (neither plain text nor HTML)"
                    )

            content_slice = content[offset : offset + CHUNK_SIZE]

            return ToolResult.success(
                {
                    "content": content_slice,
                    "total_length": len(content),
                    "has_more": len(content) > offset + CHUNK_SIZE,
                    "sender": message.sender,
                    "subject": message.subject,
                    "date": message.date,
                }
            )
        except Exception as e:
            return ToolResult.error(f"Failed to read message: {str(e)}")


class GmailDownloadAttachmentTool(GmailToolBase):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_download_attachment",
            description="""Downloads an attachment from a Gmail message.
Returns JSON with fields:
- success: boolean (whether download was successful)
- path: string (path where file was saved, in /tmp)
- size: int (size of downloaded file in bytes)
- mime_type: string (MIME type of attachment)

Returns error if:
- Message ID not found
- Attachment index invalid
- Failed to download attachment""",
            args=[
                ArgSpec(
                    name="message_id",
                    type=str,
                    description="The ID of the email message",
                ),
                ArgSpec(
                    name="attachment_index",
                    type=int,
                    description="Index of the attachment to download (0-based)",
                ),
            ],
        )

    async def _execute(
        self, *, message_id: str, attachment_index: int = 0, **kwargs
    ) -> ToolResult:
        assert not kwargs, f"Unexpected arguments: {kwargs}"
        try:
            message = get_message(self.gmail, message_id)
            if not message:
                return ToolResult.error(f"Message with ID {message_id} not found")

            attachments = message.attachments
            if not attachments:
                return ToolResult.error("Message has no attachments")

            if attachment_index < 0 or attachment_index >= len(attachments):
                return ToolResult.error(
                    f"Invalid attachment index {attachment_index}. Message has {len(attachments)} attachments"
                )

            attachment = attachments[attachment_index]
            filename = attachment.filename.replace(" ", "_")  # Sanitize filename
            save_path = f"/tmp/{filename}"

            # Download and save the attachment
            with open(save_path, "wb") as f:
                f.write(attachment.data)

            return ToolResult.success(
                {
                    "success": True,
                    "path": save_path,
                    "size": len(attachment.data),
                    "mime_type": attachment.filetype,
                }
            )
        except Exception as e:
            return ToolResult.error(f"Failed to download attachment: {str(e)}")


class GmailSearchTool(GmailToolBase):
    def __init__(self):
        self.gmail: Gmail

    async def init(self):
        self.gmail = get_gmail()

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_search",
            description="""Searches Gmail messages using Gmail's native search operators.
Returns JSON with fields:
- messages: list of message objects, each containing:
  - id: string (message ID)
  - subject: string (email subject)
  - sender: string (sender's email)
  - date: string (email date in ISO format)
  - snippet: string (short preview of message content)
- total_found: int (total number of messages matching query)
- returned_count: int (number of messages returned, limited by 'limit' parameter)

Search query supports Gmail operators like:
- from:someone@example.com
- subject:meeting
- has:attachment
- newer_than:2d
- older_than:1w
- is:unread
- in:inbox
- label:work

Returns error if:
- Invalid search query
- Failed to fetch messages""",
            args=[
                ArgSpec(
                    name="query",
                    type=str,
                    description="Gmail search query string (supports Gmail's search operators)",
                ),
                ArgSpec(
                    name="limit",
                    type=int,
                    description="Maximum number of results to return. Default is 10",
                    is_required=False,
                ),
            ],
        )

    async def _execute(self, *, query: str, limit: int = 10, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected arguments: {kwargs}"
        try:
            messages = self.gmail.get_messages(query=query)
            results = []

            for msg in messages[:limit]:
                results.append(
                    {
                        "id": msg.id,
                        "subject": msg.subject,
                        "sender": msg.sender,
                        "date": msg.date,
                        "snippet": msg.snippet,
                    }
                )

            return ToolResult.success(
                {
                    "messages": results,
                    "total_found": len(messages),
                    "returned_count": len(results),
                }
            )
        except Exception as e:
            return ToolResult.error(f"Failed to search messages: {str(e)}")


def get_message(
    gmail: Gmail,
    message_id: str,
    attachments: str = "full",
    user_id: str = "me",
) -> Message:
    response = (
        gmail.service.users()
        .messages()
        .get(
            userId=user_id,
            id=message_id,
        )
        .execute()
    )

    message_refs = [response]

    [the_message] = gmail._get_messages_from_refs(user_id, message_refs, attachments)
    return the_message


async def test_gmail_tools():
    # Initialize logging
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Gmail tools test")

    # Test reading a known HTML-only email (Thai Lion Air confirmation)
    read_tool = GmailReadTool()
    async with read_tool.context():
        logger.info("Testing Gmail read with HTML-only email...")
        message_id = "194dafa9fd607916"  # Thai Lion Air confirmation
        read_result = await read_tool(message_id=message_id, offset=0)

        if not read_result.is_error:
            read_data = json.loads(read_result.text)
            logger.info(f"Successfully read message from {read_data['sender']}")
            logger.info(f"Subject: {read_data['subject']}")
            logger.info(f"Content length: {read_data['total_length']}")
            logger.info(f"Content preview: {read_data['content'][:200]}")
        else:
            logger.error(f"Error reading message: {read_result.text}")
            logger.info(
                "This error is expected with current code - need to implement HTML support"
            )


def get_gmail_tools() -> list[LocalTool]:
    return [
        klass()
        for klass in GmailToolBase.__subclasses__()
        if klass is not GmailToolBase
    ]


async def test_html_email():
    # Initialize logging
    logging.basicConfig(level=logging.INFO)
    logger.info("Testing HTML-only email read")

    # Test reading Thai Lion Air confirmation (HTML-only)
    read_tool = GmailReadTool()
    async with read_tool.context():
        message_id = "194dafa9fd607916"  # Thai Lion Air confirmation
        result = await read_tool(message_id=message_id)

        if result.is_error:
            assert not "invalid_grant" in str(result.text), result
            logger.error(f"Failed to read HTML content: {result.text}")
        else:
            data = json.loads(result.text)
            logger.info(f"Successfully read HTML email from: {data['sender']}")
            logger.info(f"Subject: {data['subject']}")
            logger.info(f"Content length: {data['total_length']}")
            logger.info(f"Content preview: {data['content'][:200]}")


if __name__ == "__main__":
    # python -m clam_ptb.tools.gmail_tool
    try:
        asyncio.run(test_html_email())
    except Exception as e:
        logger.error(f"Test failed with error: {e}")
        raise

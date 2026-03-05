"""A protocol that defines what per-user config should define."""

import dataclasses


@dataclasses.dataclass
class LocalSettings:
    ROOT_USER_ID: int
    # Map from user id to user name. As minumum should map ROOT_USER_ID to some string.
    USER_ID_MAP: dict[int, str]
    # Bot id as defined by telegram.
    BOT_REAL_USER_ID: int
    # A name of the bot for mentions.
    BOT_FULL_NAME: str
    # A group chat with bot with a lot of debug info
    FULL_LOG_CHAT_ID: int

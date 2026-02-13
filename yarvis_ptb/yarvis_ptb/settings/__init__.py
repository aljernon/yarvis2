import importlib
import os
from typing import cast

from yarvis_ptb.local_settings import LocalSettings
from yarvis_ptb.settings.main import *

if not os.environ.get("SETTINGS_NAME"):
    raise ValueError("SETTINGS_NAME environment variable must be set")

SETTINGS_NAME = os.environ["SETTINGS_NAME"]


# Dynamically import the settings module
settings_module = importlib.import_module(f"yarvis_ptb.settings.{SETTINGS_NAME}")
local_settings = cast(LocalSettings, getattr(settings_module, "LOCAL_SETTINGS"))


USER_ID_MAP = local_settings.USER_ID_MAP
ROOT_USER_ID = local_settings.ROOT_USER_ID
BOT_REAL_USER_ID = local_settings.BOT_REAL_USER_ID
BOT_FULL_NAME = local_settings.BOT_FULL_NAME
FULL_LOG_CHAT_ID = local_settings.FULL_LOG_CHAT_ID

ID_USER_MAP: dict[str, int] = {v: k for k, v in USER_ID_MAP.items()}

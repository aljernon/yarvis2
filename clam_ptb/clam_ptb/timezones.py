"""Timezone handling utilities."""

import json

import pytz

from clam_ptb.settings import DEFAULT_TIMEZONE_STR, PROJECT_ROOT

SETTINGS_MEMORY_PATH = PROJECT_ROOT / "core_knowledge/settings.json"


def get_complex_chat_timezone_str() -> str:
    """Get custom timezone string from settings in Core Knowledge Repository if set."""
    try:
        with open(SETTINGS_MEMORY_PATH) as f:
            settings = json.load(f)
            if isinstance(settings, dict) and "timezone" in settings:
                return settings["timezone"]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return DEFAULT_TIMEZONE_STR


def get_timezone(complex_chat: bool):
    tz_str = get_complex_chat_timezone_str() if complex_chat else DEFAULT_TIMEZONE_STR
    return pytz.timezone(tz_str)

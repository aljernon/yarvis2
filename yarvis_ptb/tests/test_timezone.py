"""Test timezone customization feature."""

import builtins
import datetime
import json
import unittest
from unittest import mock

import pytz

from yarvis_ptb.complex_chat import DEFAULT_COMPLEX_CHAT_CONFIG
from yarvis_ptb.prompting import build_context_info
from yarvis_ptb.settings import DEFAULT_TIMEZONE, DEFAULT_TIMEZONE_STR
from yarvis_ptb.timezones import (
    SETTINGS_MEMORY_PATH,
    get_complex_chat_timezone_str,
    get_timezone,
)


def mock_timezone_memory_path(settings):
    original_open = builtins.open

    def side_effect(fname, *args, **kwargs):
        if fname == SETTINGS_MEMORY_PATH:
            return mock.mock_open(read_data=json.dumps(settings))(
                fname, *args, **kwargs
            )
        return original_open(fname, *args, **kwargs)

    return mock.patch("builtins.open", side_effect)


class TestTimezone(unittest.TestCase):
    """Test timezone customization feature."""

    def test_timezone_default_fallback(self):
        """Test that missing settings.json results in default timezone."""
        settings = {}  # Empty dict = no timezone set

        with mock_timezone_memory_path(settings):
            self.assertEqual(get_complex_chat_timezone_str(), DEFAULT_TIMEZONE_STR)
            self.assertEqual(get_timezone(False), DEFAULT_TIMEZONE)
            self.assertEqual(get_timezone(True), DEFAULT_TIMEZONE)

    def test_timezone_customization(self):
        """Test that custom timezone is used when set."""
        test_timezone = "Asia/Bangkok"
        settings = {"timezone": test_timezone}

        with mock_timezone_memory_path(settings):
            self.assertEqual(get_complex_chat_timezone_str(), test_timezone)
            self.assertEqual(get_timezone(False), DEFAULT_TIMEZONE)
            self.assertEqual(get_timezone(True), pytz.timezone(test_timezone))

    def test_context_timezone_display(self):
        """Test that context info uses custom timezone for display."""
        test_timezone = "Asia/Bangkok"
        settings = {"timezone": test_timezone}
        fixed_time = datetime.datetime(2025, 2, 2, 22, 40, 0, tzinfo=pytz.UTC)

        with (
            mock_timezone_memory_path(settings),
            mock.patch("datetime.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fixed_time

            context = build_context_info(
                invocation=None,
                scheduled_invocations=None,
                chat_config=DEFAULT_COMPLEX_CHAT_CONFIG,
            )

            # Bangkok time should be ahead of UTC
            self.assertIn("2025-02-03", context)  # Next day in Bangkok
            self.assertIn(test_timezone.split("/")[1], context)


if __name__ == "__main__":
    print("Running timezone tests with proper mocking - no real files will be accessed")
    unittest.main()

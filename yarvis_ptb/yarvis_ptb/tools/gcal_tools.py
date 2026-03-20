"""Google Calendar helper — provides authenticated GoogleCalendar instance.

Tools were removed in favor of using gcsa directly via the Python REPL.
See workspace/skills/calendar-scheduling/SKILL.md for usage patterns.
"""

import functools
import logging

from gcsa.google_calendar import GoogleCalendar

from yarvis_ptb.settings import PROJECT_ROOT

logger = logging.getLogger(__name__)


@functools.cache
def get_calendar(open_browser=None):
    credentials_filename = "credentials.json"
    calendar = GoogleCalendar(
        "anton.v.bakhtin@gmail.com",
        credentials_path=str(PROJECT_ROOT / credentials_filename),
        token_path=str(PROJECT_ROOT / "token.pickle"),
        authentication_flow_port=8081,
        open_browser=open_browser,
    )
    return calendar


if __name__ == "__main__":
    # Used by update_tokens.sh to refresh token.pickle
    cal = get_calendar()
    events = list(cal.get_events())[:1]
    print(f"Calendar OK, got {len(events)} event(s)")

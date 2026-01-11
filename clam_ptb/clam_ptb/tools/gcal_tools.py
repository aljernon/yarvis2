import datetime
import functools
import itertools
import json
import logging

import google.auth.exceptions
from gcsa.event import Event
from gcsa.google_calendar import GoogleCalendar

from clam_ptb.settings import DEFAULT_TIMEZONE, DEFAULT_TIMEZONE_STR, PROJECT_ROOT
from clam_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

MAX_EVENTS = 20

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


# event = Event(
#     'Breakfast',
#     start=(1 / Jan / 2019)[9:00],
#     recurrence=[
#         Recurrence.rule(freq=DAILY),
#         Recurrence.exclude_rule(by_week_day=[SU, SA]),
#         Recurrence.exclude_times([
#             (19 / Apr / 2019)[9:00],
#             (22 / Apr / 2019)[9:00]
#         ])
#     ],
#     minutes_before_email_reminder=50
# )

# calendar.add_event(event)


class ListCalendarEventsTool(LocalTool):
    def __init__(self):
        self.calendar = get_calendar()

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_calendar_events",
            description=f"""
Get calendar events from the Gooogle Calendar of the user. Default timezone is {DEFAULT_TIMEZONE_STR}.

It's better to use time_min and time_max to limit the number of events returned. Otherwise first {MAX_EVENTS} events are returns.
Returns a list of events as json objects. Each object has the following keys:
    * summary (str) - Event title
    * description (str | None) - Event description text
    * start (str) - Start time in ISO format
    * end (str | None) - End time in ISO format, null for events without end time
    * event_id (str) - Unique event identifier
    * location (str | None) - Location text if specified
    * attendees (list[str] | None) - List of attendee email addresses
    * recurring_event_id (str | None) - Recurring event identifier if event is an instance of recurring event
    * recurrence (str | list[str] | None) - Recurrence rule if event is recurring

Note about recurring events:

If no_expand_recurring is set to False (default), recurring events will be
expanded into instances. Events that are part of the same recurring event will
have the same recurring_event_id.

If no_expand_recurring is set to True, only single one-off events and one event
per recurring events will be returned. The start time of the event will
correspond to the first instance of the recurring event. Note, that time_min and
time_max will show a recurring event if any of its instances fall within the range.
""".strip(),
            args=[
                ArgSpec(
                    name="time_min",
                    type=str,
                    description="Starting datetime in iso format. Required if no_expand_recurring is set",
                    is_required=False,
                ),
                ArgSpec(
                    name="time_max",
                    type=str,
                    description="Ending datetime in iso format.",
                    is_required=False,
                ),
                ArgSpec(
                    name="order_by",
                    type=str,
                    description='Order of the events. Possible values: "startTime", "updated". Default is startTime if no_expand_recurring is not set. If no_expand_recurring is set, this field is ignored.',
                    is_required=False,
                ),
                ArgSpec(
                    name="no_expand_recurring",
                    type=bool,
                    description="Whether to expand recurring events into instances and only return single one-off events and instances of recurring events, but not the underlying recurring events themselves.",
                    is_required=False,
                ),
                ArgSpec(
                    name="query",
                    type=str,
                    description="Free text search terms to find events that match these terms in any field, except for extended properties.",
                    is_required=False,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        kwargs = dict(kwargs)
        if time_min := kwargs.pop("time_min", None):
            time_min = datetime.datetime.fromisoformat(time_min).astimezone(
                DEFAULT_TIMEZONE
            )
        if time_max := kwargs.pop("time_max", None):
            time_max = datetime.datetime.fromisoformat(time_max).astimezone(
                DEFAULT_TIMEZONE
            )
        get_events_kwargs = {
            "timezone": DEFAULT_TIMEZONE_STR,
            "time_min": time_min,
            "time_max": time_max,
            "order_by": kwargs.pop("order_by", "startTime"),
            "single_events": not kwargs.pop("no_expand_recurring", False),
            "query": kwargs.pop("query", None),
        }
        if not get_events_kwargs["single_events"]:
            if not get_events_kwargs["time_min"]:
                return ToolResult.error("time_min is required for recurring events")
            del get_events_kwargs["order_by"]
        assert not kwargs, f"Unknown arguments: {kwargs}"
        events: list[Event] = list(
            itertools.islice(
                get_calendar(open_browser=False).get_events(**get_events_kwargs),
                0,
                MAX_EVENTS,
            )
        )
        event_dicts = [
            dict(
                summary=event.summary,
                description=event.description,
                start=event.start.isoformat() if event.start else None,
                end=event.end.isoformat() if event.end else None,
                event_id=event.event_id,
                location=event.location,
                attendees=[a.email for a in event.attendees]
                if event.attendees
                else None,
                recurring_event_id=event.recurring_event_id,
                recurrence=event.recurrence,
            )
            for event in events
        ]
        return ToolResult(text=json.dumps(event_dicts))


class AddCalendarEventTool(LocalTool):
    def __init__(self):
        self.calendar = get_calendar(open_browser=False)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="add_event",
            description="""Add calendar event.""",
            args=[
                ArgSpec(
                    name="summary",
                    type=str,
                    description="Event summary/title",
                    is_required=True,
                ),
                ArgSpec(
                    name="start",
                    type=str,
                    description="Start time in ISO format",
                    is_required=True,
                ),
                ArgSpec(
                    name="end",
                    type=str,
                    description="End time in ISO format",
                    is_required=True,
                ),
                ArgSpec(
                    name="description",
                    type=str,
                    description="Event description",
                    is_required=False,
                ),
                ArgSpec(
                    name="location",
                    type=str,
                    description="Event location",
                    is_required=False,
                ),
                ArgSpec(
                    name="attendees",
                    type=str,
                    description="Comma separated list of attendee email addresses",
                    is_required=False,
                ),
                ArgSpec(
                    name="color_id",
                    type=str,
                    description="Color ID for the event (1-11). Common values: 8=Gray/Graphite, 1=Lavender, 2=Sage, 3=Grape, 4=Flamingo, 5=Banana, 6=Tangerine, 7=Peacock, 9=Blueberry, 10=Basil, 11=Tomato",
                    is_required=False,
                ),
            ],
        )

    async def _execute(
        self,
        *,
        summary: str,
        start: str,
        end: str,
        description: str | None = None,
        location: str | None = None,
        attendees: str | None = None,
        color_id: str | None = None,
        **kwargs,
    ) -> ToolResult:
        assert not kwargs, f"Unknown arguments: {kwargs}"
        del kwargs
        # Convert times
        start_date = datetime.datetime.fromisoformat(start).astimezone(DEFAULT_TIMEZONE)
        end_date = datetime.datetime.fromisoformat(end).astimezone(DEFAULT_TIMEZONE)
        event = Event(
            summary=summary.strip(),
            start=start_date,
            end=end_date,
            location=location,
            attendees=attendees.split(",") if attendees else None,
            description=description,
            color_id=color_id,
        )
        created = self.calendar.add_event(event)
        return ToolResult(f"Created event {created.event_id}: {created.summary}")


class UpdateCalendarEventTool(LocalTool):
    def __init__(self):
        self.calendar = get_calendar(open_browser=False)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="modify_event",
            description="""Modify a calendar event.""",
            args=[
                ArgSpec(
                    name="event_id",
                    type=str,
                    description="Event ID if modifying existing event",
                    is_required=True,
                ),
                ArgSpec(
                    name="summary",
                    type=str,
                    description="Event summary/title",
                    is_required=False,
                ),
                ArgSpec(
                    name="start",
                    type=str,
                    description="Start time in ISO format",
                    is_required=False,
                ),
                ArgSpec(
                    name="end",
                    type=str,
                    description="End time in ISO format",
                    is_required=False,
                ),
                ArgSpec(
                    name="description",
                    type=str,
                    description="Event description",
                    is_required=False,
                ),
                ArgSpec(
                    name="location",
                    type=str,
                    description="Event location",
                    is_required=False,
                ),
                ArgSpec(
                    name="attendees",
                    type=str,
                    description="Comma separated list of attendee email addresses",
                    is_required=False,
                ),
                ArgSpec(
                    name="color_id",
                    type=str,
                    description="Color ID for the event (1-11). Common values: 8=Gray/Graphite, 1=Lavender, 2=Sage, 3=Grape, 4=Flamingo, 5=Banana, 6=Tangerine, 7=Peacock, 9=Blueberry, 10=Basil, 11=Tomato",
                    is_required=False,
                ),
            ],
        )

    async def _execute(
        self,
        *,
        event_id: str,
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        description: str | None = None,
        location: str | None = None,
        attendees: str | None = None,
        color_id: str | None = None,
        **kwargs,
    ) -> ToolResult:
        assert not kwargs, f"Unknown arguments: {kwargs}"
        del kwargs

        # Convert times
        start_date = (
            datetime.datetime.fromisoformat(start).astimezone(DEFAULT_TIMEZONE)
            if start is not None
            else None
        )
        end_date = (
            datetime.datetime.fromisoformat(end).astimezone(DEFAULT_TIMEZONE)
            if end is not None
            else None
        )

        # Get existing event to verify [cl] prefix
        existing = self.calendar.get_event(event_id)

        event = Event(
            summary=(summary or existing.summary).strip(),
            start=start_date or existing.start,
            end=end_date or existing.end,
            event_id=event_id,
            description=description or existing.description,
            location=location or existing.location,
            attendees=attendees.split(",")
            if attendees is not None
            else existing.attendees,
            color_id=color_id or existing.color_id,
        )
        updated = self.calendar.update_event(event)
        return ToolResult(f"Updated event {updated.event_id}: {updated.summary}")


class DeleteCalendarEventTool(LocalTool):
    def __init__(self):
        self.calendar = get_calendar(open_browser=False)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="delete_event",
            description="Delete a calendar event by ID.",
            args=[
                ArgSpec(
                    name="event_id",
                    type=str,
                    description="Event ID to delete",
                    is_required=True,
                )
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        event_id = kwargs["event_id"]

        # Get existing event to verify [cl] prefix
        existing = self.calendar.get_event(event_id)

        self.calendar.delete_event(event_id)
        return ToolResult(f"Deleted event {event_id}: {existing.summary}")


def get_calendar_tools() -> list[LocalTool]:
    try:
        get_calendar(open_browser=False)
    except google.auth.exceptions.RefreshError:
        logger.exception("Failed to get calendar. Disabling calendar tools.")
        return []
    return [
        ListCalendarEventsTool(),
        AddCalendarEventTool(),
        UpdateCalendarEventTool(),
        DeleteCalendarEventTool(),
    ]


async def test_get_calendar_events():
    print(
        "============================= Calling gcal directly ============================="
    )
    for event in list(
        get_calendar().get_events(
            order_by="updated", timezone=DEFAULT_TIMEZONE_STR, single_events=True
        )
    )[-50:]:
        print(event)
    print("============================= Tool spec =============================")
    print(ListCalendarEventsTool().spec())
    print(
        "============================= Tool result no args ============================="
    )
    res = await ListCalendarEventsTool()()
    for line in json.loads(res.text):
        print(line)
    print(
        "============================= Tool result no args (no_expand_recurring) ============================="
    )
    res = await ListCalendarEventsTool()(
        no_expand_recurring=True,
        time_min=(datetime.datetime.now() - datetime.timedelta(days=2)).isoformat(),
    )
    for line in json.loads(res.text):
        print(line)


if __name__ == "__main__":
    import asyncio

    asyncio.run(test_get_calendar_events())

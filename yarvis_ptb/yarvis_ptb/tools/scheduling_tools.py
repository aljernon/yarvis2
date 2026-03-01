import datetime
import logging
import re

from croniter import croniter

from yarvis_ptb.settings import DEFAULT_TIMEZONE
from yarvis_ptb.storage import (
    DbSchedule,
    deactivate_schedule,
    get_schedule_by_id,
    get_schedules,
    save_schedule,
)
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


INTERVAL_PATTERN = re.compile(r"^(\d+)\s*(s|m|h|d|w)$")
INTERVAL_MULTIPLIERS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def parse_interval(spec: str) -> datetime.timedelta:
    """Parse a human-readable interval like '30m', '2h', '1d' into a timedelta."""
    match = INTERVAL_PATTERN.match(spec.strip())
    if not match:
        raise ValueError(
            f"Invalid interval format '{spec}'. Use e.g. '30s', '5m', '2h', '1d', '1w'"
        )
    value = int(match.group(1))
    unit = match.group(2)
    return datetime.timedelta(seconds=value * INTERVAL_MULTIPLIERS[unit])


def compute_next_run(schedule: DbSchedule, now: datetime.datetime) -> datetime.datetime:
    """Compute the next run time for a recurring schedule."""
    if schedule.schedule_type == "every":
        interval = parse_interval(schedule.schedule_spec)
        next_run = schedule.next_run_at + interval
        # If we're behind, skip forward to the next future run
        while next_run <= now:
            next_run += interval
        return next_run
    elif schedule.schedule_type == "cron":
        cron = croniter(schedule.schedule_spec, now.astimezone(DEFAULT_TIMEZONE))
        return cron.get_next(datetime.datetime).astimezone(DEFAULT_TIMEZONE)
    else:
        raise ValueError(
            f"Cannot compute next run for schedule_type={schedule.schedule_type}"
        )


async def cancel_schedule_fn(curr, chat_id: int, scheduled_id: int) -> ToolResult:
    schedules = get_schedules(curr, chat_id)
    try:
        [the_schedule] = [x for x in schedules if x.schedule_id == scheduled_id]
    except ValueError:
        return ToolResult.error(
            text=f"Could not find active schedule with id {scheduled_id}",
        )
    deactivate_schedule(curr, the_schedule)
    return ToolResult(text=f"Cancelled schedule {scheduled_id}")


async def get_schedule_details_fn(curr, chat_id: int, scheduled_id: int) -> ToolResult:
    schedule = get_schedule_by_id(curr, scheduled_id)
    if schedule is None:
        return ToolResult.error(text=f"No schedule found with id {scheduled_id}")
    if schedule.chat_id != chat_id:
        return ToolResult.error(
            text=f"Schedule {scheduled_id} belongs to a different chat"
        )
    details = {
        "schedule_id": schedule.schedule_id,
        "reason": schedule.reason,
        "context": schedule.context,
        "schedule_type": schedule.schedule_type,
        "schedule_spec": schedule.schedule_spec,
        "next_run_at": schedule.next_run_at.isoformat(),
        "is_active": schedule.is_active,
    }
    return ToolResult.success(details)


async def schedule_fn(
    curr,
    chat_id: int,
    reason: str,
    at: str | None = None,
    cron: str | None = None,
    every: str | None = None,
    context: str | None = None,
) -> ToolResult:
    # Validate exactly one schedule type
    provided = [
        (k, v)
        for k, v in [("at", at), ("cron", cron), ("every", every)]
        if v is not None
    ]
    if len(provided) != 1:
        return ToolResult.error(
            text="Exactly one of 'at', 'cron', or 'every' must be provided"
        )

    schedule_type, spec_value = provided[0]
    now = datetime.datetime.now(DEFAULT_TIMEZONE)

    if schedule_type == "at":
        try:
            dt = datetime.datetime.fromisoformat(spec_value)
        except Exception as e:
            return ToolResult.error(text=f"Error parsing datetime '{spec_value}': {e}")
        if dt.tzinfo is None:
            dt = DEFAULT_TIMEZONE.localize(dt)
        if dt < now:
            return ToolResult.error(text=f"Cannot schedule in the past: {dt}")
        next_run_at = dt
        schedule_spec = None

    elif schedule_type == "cron":
        if not croniter.is_valid(spec_value):
            return ToolResult.error(text=f"Invalid cron expression: '{spec_value}'")
        try:
            cron_iter = croniter(spec_value, now.astimezone(DEFAULT_TIMEZONE))
            next_run_at = cron_iter.get_next(datetime.datetime).astimezone(
                DEFAULT_TIMEZONE
            )
        except Exception as e:
            return ToolResult.error(text=f"Error computing next cron run: {e}")
        schedule_spec = spec_value

    elif schedule_type == "every":
        try:
            interval = parse_interval(spec_value)
        except ValueError as e:
            return ToolResult.error(text=str(e))
        next_run_at = now + interval
        schedule_spec = spec_value

    schedule_id = save_schedule(
        curr,
        DbSchedule(
            next_run_at=next_run_at,
            reason=reason,
            chat_id=chat_id,
            schedule_type=schedule_type,
            schedule_spec=schedule_spec,
            context=context,
        ),
    )

    type_desc = {
        "at": f"one-time at {next_run_at.isoformat()}",
        "cron": f'cron "{schedule_spec}"; next at {next_run_at.isoformat()}',
        "every": f"every {schedule_spec}; next at {next_run_at.isoformat()}",
    }[schedule_type]

    return ToolResult(
        text=f"Scheduled {type_desc} scheduled_id={schedule_id}",
    )


class SchedulingTool(LocalTool):
    def __init__(self, curr, chat_id):
        self.curr = curr
        self.chat_id = chat_id


class CancelScheduleTool(SchedulingTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="cancel_schedule",
            description="Cancel a scheduled invocation",
            args=[
                ArgSpec(
                    name="scheduled_id",
                    type=int,
                    description="The id of the schedule to cancel",
                    is_required=True,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        return await cancel_schedule_fn(self.curr, self.chat_id, **kwargs)


class GetScheduleDetailsTool(SchedulingTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_schedule_details",
            description="Get full details of a schedule including its context field",
            args=[
                ArgSpec(
                    name="scheduled_id",
                    type=int,
                    description="The id of the schedule to inspect",
                    is_required=True,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        return await get_schedule_details_fn(self.curr, self.chat_id, **kwargs)


class ScheduleTool(SchedulingTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="schedule",
            description=f"Schedule an invocation. Provide exactly one of 'at', 'cron', or 'every'. Default timezone is {DEFAULT_TIMEZONE}.",
            args=[
                ArgSpec(
                    name="at",
                    type=str,
                    description="ISO datetime for a one-time schedule (e.g. '2026-03-01T10:00:00')",
                    is_required=False,
                ),
                ArgSpec(
                    name="cron",
                    type=str,
                    description="Cron expression for recurring schedule (e.g. '0 7 * * 1-5' for weekdays at 7am)",
                    is_required=False,
                ),
                ArgSpec(
                    name="every",
                    type=str,
                    description="Interval for recurring schedule (e.g. '30m', '2h', '1d')",
                    is_required=False,
                ),
                ArgSpec(
                    name="reason",
                    type=str,
                    description="Short description shown in system prompt at every turn",
                    is_required=True,
                ),
                ArgSpec(
                    name="context",
                    type=str,
                    description="Longer context text, hidden from prompt but shown at invocation time. Use for detailed instructions.",
                    is_required=False,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        return await schedule_fn(self.curr, self.chat_id, **kwargs)


def build_scheduling_tools(curr, chat_id) -> list[LocalTool]:
    return [
        CancelScheduleTool(curr, chat_id),
        GetScheduleDetailsTool(curr, chat_id),
        ScheduleTool(curr, chat_id),
    ]

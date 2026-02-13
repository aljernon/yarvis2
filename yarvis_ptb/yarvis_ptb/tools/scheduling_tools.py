import datetime
import logging

from yarvis_ptb.settings import DEFAULT_TIMEZONE
from yarvis_ptb.storage import (
    DbScheduledInvocation,
    get_scheduled_invocations,
    save_invocation,
    set_non_active_invocation,
)
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


async def cancel_schedule(curr, chat_id: int, scheduled_id: int) -> ToolResult:
    invokations = get_scheduled_invocations(curr, chat_id)
    try:
        [the_invocation] = [x for x in invokations if x.scheduled_id == scheduled_id]
    except ValueError:
        return ToolResult.error(
            text=f"Could not find scheduled invocation with id {scheduled_id}",
        )
    set_non_active_invocation(curr, the_invocation)
    return ToolResult(text="Canceling scheduled invocation")


async def schedule(
    curr, chat_id: int, datetime_str: str, reason: str, is_recurring: bool = False
) -> ToolResult:
    try:
        dt = datetime.datetime.fromisoformat(datetime_str)
    except Exception as e:
        return ToolResult.error(
            text=f"Got an error trying to parse datetime {datetime_str}: {e}",
        )

    if dt.tzinfo is None:
        dt = DEFAULT_TIMEZONE.localize(dt)

    if dt < datetime.datetime.now(DEFAULT_TIMEZONE):
        return ToolResult.error(
            text=f"Got an error trying to schedule in the past {dt}",
        )

    scheduled_id = save_invocation(
        curr,
        DbScheduledInvocation(
            scheduled_at=dt, reason=reason, chat_id=chat_id, is_recurring=is_recurring
        ),
    )

    if is_recurring:
        return ToolResult(
            text=f"Scheduled recurring invocation starting at {dt.isoformat()} scheduled_id={scheduled_id}",
        )
    else:
        return ToolResult(
            text=f"Scheduled invocation at {dt.isoformat()} scheduled_id={scheduled_id}",
        )


class SchedulingTool(LocalTool):
    def __init__(self, curr, chat_id):
        self.curr = curr
        self.chat_id = chat_id


class CancelScheduleTool(SchedulingTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="cancel_schedule",
            description="Removed scheduled invocation",
            args=[
                ArgSpec(
                    name="scheduled_id",
                    type=int,
                    description="The id of the scheduled invocation to cancel",
                    is_required=True,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        return await cancel_schedule(self.curr, self.chat_id, **kwargs)


class ScheduleTool(SchedulingTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="schedule",
            description=f"Schedule invocation at specific time. Default timezone is {DEFAULT_TIMEZONE} if not provided",
            args=[
                ArgSpec(
                    name="datetime_str",
                    type=str,
                    description="Datetime in ISO format",
                    is_required=True,
                ),
                ArgSpec(
                    name="reason",
                    type=str,
                    description="A hint regarding why this invocation was scheduled",
                    is_required=True,
                ),
                ArgSpec(
                    name="is_recurring",
                    type=bool,
                    description="Whether to create a recurring invocation. If true, will invoke agent every day at the specified time starting with the initial invocation.",
                    is_required=False,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        return await schedule(self.curr, self.chat_id, **kwargs)


def build_scheduling_tools(curr, chat_id) -> list[LocalTool]:
    tools = [
        CancelScheduleTool(curr, chat_id),
        ScheduleTool(curr, chat_id),
    ]
    return tools

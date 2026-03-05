import json

from clam_ptb.settings import (
    LOCATION_PATH,
)
from clam_ptb.tools.tool_spec import LocalTool, ToolResult, ToolSpec


class GetLocationTool(LocalTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_location",
            description="Get the current location of the user as json dict (fields: lat, lon, recorded_at). The data comes from the user's phone. If not found, returns null",
            args=[],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        if not LOCATION_PATH.exists():
            ret = None
        else:
            with open(LOCATION_PATH) as f:
                coords = json.load(f)
            ret = {
                "lat": coords["lat"],
                "lon": coords["lon"],
                "recorded_at": coords["recorded_at"],
            }
        return ToolResult.success(ret)

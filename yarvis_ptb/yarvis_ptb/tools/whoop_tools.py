import json
import logging
from datetime import datetime, timedelta

import requests

from yarvis_ptb.settings import PROJECT_ROOT
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

WHOOP_CONFIG_PATH = PROJECT_ROOT / "whoop_config.json"
WHOOP_TOKEN_PATH = PROJECT_ROOT / "whoop_token.json"

WHOOP_API_BASE = "https://api.prod.whoop.com/developer/v2"


def _load_whoop_token() -> dict:
    """Load and return the token data, refreshing if expired."""
    with open(WHOOP_TOKEN_PATH) as f:
        token_data = json.load(f)

    # Check expiry and refresh if needed
    created_at = token_data.get("created_at")
    expires_in = token_data.get("expires_in", 3600)
    if created_at:
        from datetime import timezone

        created = datetime.fromisoformat(created_at)
        if datetime.now(timezone.utc) >= created + timedelta(seconds=expires_in - 60):
            token_data = _refresh_token(token_data)

    return token_data


def _refresh_token(token_data: dict) -> dict:
    """Refresh the access token using the refresh token."""
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise ValueError("No refresh token available. Re-run whoop_auth.py.")

    with open(WHOOP_CONFIG_PATH) as f:
        config = json.load(f)

    resp = requests.post(
        "https://api.prod.whoop.com/oauth/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
        },
    )
    resp.raise_for_status()
    new_token = resp.json()

    save_data = {
        "access_token": new_token["access_token"],
        "expires_in": new_token.get("expires_in", 3600),
        "refresh_token": new_token.get("refresh_token", refresh_token),
        "scopes": new_token.get("scope", "").split() or token_data.get("scopes", []),
        "token_type": new_token.get("token_type", "bearer"),
        "created_at": datetime.now(tz=__import__("datetime").timezone.utc).isoformat(),
    }
    with open(WHOOP_TOKEN_PATH, "w") as f:
        json.dump(save_data, f, indent=2)
    return save_data


def _whoop_api_get(path: str, params: dict | None = None) -> requests.Response:
    """Make an authenticated GET request to the Whoop API."""
    token_data = _load_whoop_token()
    headers = {"Authorization": f"Bearer {token_data['access_token']}"}
    return requests.get(f"{WHOOP_API_BASE}{path}", headers=headers, params=params)


class WhoopDataTool(LocalTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_whoop_data",
            description=(
                "Get health data from the user's Whoop fitness tracker. "
                "Can retrieve recovery scores, sleep data, workout data, and daily strain cycles."
            ),
            args=[
                ArgSpec(
                    name="data_type",
                    type=str,
                    description=(
                        'Type of data to retrieve. One of: "recovery", "sleep", "workouts", "cycles".'
                    ),
                    is_required=True,
                ),
                ArgSpec(
                    name="days",
                    type=int,
                    description="Number of days back to query. Default is 1 (today only).",
                    is_required=False,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        data_type = kwargs["data_type"]
        days = kwargs.get("days", 1)

        ENDPOINTS = {
            "recovery": "/recovery",
            "sleep": "/activity/sleep",
            "workouts": "/activity/workout",
            "cycles": "/cycle",
        }  # Whoop API v2 paths

        if data_type not in ENDPOINTS:
            return ToolResult.error(
                f"Invalid data_type: {data_type}. Must be one of: {', '.join(ENDPOINTS)}."
            )

        start = (datetime.now() - timedelta(days=days)).strftime(
            "%Y-%m-%dT00:00:00.000Z"
        )
        end = datetime.now().strftime("%Y-%m-%dT23:59:59.999Z")

        try:
            resp = _whoop_api_get(
                ENDPOINTS[data_type], params={"start": start, "end": end}
            )
            if resp.status_code == 404:
                return ToolResult.success(
                    f"No {data_type} data available from Whoop API."
                )
            resp.raise_for_status()
            data = resp.json()
            records = data.get("records", data)

            if not records:
                return ToolResult.success(
                    f"No {data_type} data found for the last {days} day(s)."
                )

            return ToolResult.success(json.dumps(records, indent=2))
        except FileNotFoundError:
            return ToolResult.error(
                "Whoop token files not found. Run whoop_auth.py to authenticate."
            )
        except Exception as e:
            return ToolResult.error(f"Failed to fetch {data_type} data: {e}")


def get_whoop_tools() -> list[LocalTool]:
    if not WHOOP_TOKEN_PATH.exists():
        logger.info(
            "Whoop token file not found at %s. Disabling Whoop tools.", WHOOP_TOKEN_PATH
        )
        return []
    return [WhoopDataTool()]

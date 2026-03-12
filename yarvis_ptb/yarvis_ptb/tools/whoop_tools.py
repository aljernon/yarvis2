import json
import logging
import os
from datetime import datetime, timedelta, timezone

import psycopg2
import requests

from yarvis_ptb.settings import PROJECT_ROOT
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

WHOOP_CONFIG_PATH = PROJECT_ROOT / "whoop_config.json"

WHOOP_API_BASE = "https://api.prod.whoop.com/developer/v2"

DB_VAR_NAME = "whoop_refresh_token"

# In-memory access token cache
_cached_access_token: str | None = None
_cached_token_expiry: datetime | None = None


def _get_refresh_token_from_db() -> str | None:
    """Read the Whoop refresh token from the database."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return None
    try:
        conn = psycopg2.connect(database_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM chat_variables WHERE name = %s",
                (DB_VAR_NAME,),
            )
            row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        logger.exception("Failed to read Whoop refresh token from DB")
        return None


def _save_refresh_token_to_db(refresh_token: str) -> None:
    """Save the Whoop refresh token to the database."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.warning("DATABASE_URL not set, cannot save Whoop refresh token")
        return
    try:
        conn = psycopg2.connect(database_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO chat_variables (name, value, datatype)
                VALUES (%s, %s, 'str')
                ON CONFLICT (name)
                DO UPDATE SET value = EXCLUDED.value, datatype = EXCLUDED.datatype
                """,
                (DB_VAR_NAME, refresh_token),
            )
        conn.close()
    except Exception:
        logger.exception("Failed to save Whoop refresh token to DB")


def _get_access_token() -> str:
    """Get a valid access token, refreshing if needed. Cached in memory."""
    global _cached_access_token, _cached_token_expiry

    now = datetime.now(timezone.utc)
    if _cached_access_token and _cached_token_expiry and now < _cached_token_expiry:
        return _cached_access_token

    token_data = _refresh_token()
    access_token: str = token_data["access_token"]
    _cached_access_token = access_token
    _cached_token_expiry = now + timedelta(
        seconds=token_data.get("expires_in", 3600) - 60
    )
    return access_token


def _refresh_token() -> dict:
    """Refresh the access token using the refresh token from DB."""
    refresh_token = _get_refresh_token_from_db()
    if not refresh_token:
        raise ValueError(
            "No Whoop refresh token in database. Run whoop_auth.py to authenticate."
        )

    with open(WHOOP_CONFIG_PATH) as f:
        config = json.load(f)

    resp = requests.post(
        "https://api.prod.whoop.com/oauth/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "scope": "offline",
        },
    )
    if not resp.ok:
        logger.error(
            "Whoop token refresh failed: %s %s", resp.status_code, resp.text[:500]
        )
        resp.raise_for_status()
    new_token = resp.json()

    # Save new refresh token to DB (Whoop rotates refresh tokens)
    new_refresh = new_token.get("refresh_token")
    if new_refresh:
        _save_refresh_token_to_db(new_refresh)

    return new_token


def _whoop_api_get(path: str, params: dict | None = None) -> requests.Response:
    """Make an authenticated GET request to the Whoop API."""
    access_token = _get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}
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
        except Exception as e:
            return ToolResult.error(f"Failed to fetch {data_type} data: {e}")


WHOOP_REFRESH_INTERVAL = timedelta(hours=6)
_REFRESH_BACKOFF_INTERVAL = timedelta(hours=1)
_last_refresh: datetime | None = None
_last_refresh_failure: datetime | None = None


def maybe_refresh_whoop_token() -> None:
    """Proactively refresh the Whoop token to keep the refresh token alive.

    Called periodically from callback_minute. Whoop rotates refresh tokens
    on every use, and they expire quickly, so we refresh every 45 minutes.
    Backs off for 1 hour after a failure to avoid spamming.
    """
    global _last_refresh, _last_refresh_failure

    if not _get_refresh_token_from_db():
        return

    now = datetime.now(timezone.utc)

    # Back off after failure
    last_fail = _last_refresh_failure
    if last_fail is not None and (now - last_fail) < _REFRESH_BACKOFF_INTERVAL:
        return

    # Skip if refreshed recently
    if _last_refresh is not None and (now - _last_refresh) < WHOOP_REFRESH_INTERVAL:
        return

    try:
        logger.info("Refreshing Whoop token proactively")
        _refresh_token()
        _last_refresh = now
        _last_refresh_failure = None
        logger.info("Whoop token refreshed successfully")
    except Exception:
        _last_refresh_failure = now
        logger.exception("Whoop proactive token refresh failed")


def get_whoop_tools() -> list[LocalTool]:
    if not _get_refresh_token_from_db():
        logger.info("No Whoop refresh token in DB. Disabling Whoop tools.")
        return []
    return [WhoopDataTool()]

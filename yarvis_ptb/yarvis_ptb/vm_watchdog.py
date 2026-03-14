"""Monitor GCP VM health and auto-reset if unresponsive."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

GCP_PROJECT = "signal-api-project"
GCP_ZONE = "us-central1-a"
GCP_INSTANCE = "signal-api"

HEALTH_ENDPOINTS = [
    "http://100.108.7.78:8082/health",  # SMS accumulator
    "http://100.108.7.78:8081/health",  # Signal accumulator
]

HEALTH_TIMEOUT = 5
CONSECUTIVE_FAILURES_BEFORE_RESET = 3
COOLDOWN_AFTER_RESET = timedelta(minutes=10)

_consecutive_failures = 0
_last_reset: datetime | None = None


async def _check_health() -> bool:
    """Return True if at least one health endpoint responds."""
    async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
        for url in HEALTH_ENDPOINTS:
            try:
                resp = await client.get(url)
                if resp.is_success:
                    return True
            except Exception:
                continue
    return False


def _reset_vm() -> None:
    """Reset the GCP VM using the Compute API (sync — called via to_thread)."""
    key_json = os.environ.get("GCP_VM_KEY")
    if not key_json:
        logger.error("GCP_VM_KEY not set, cannot reset VM")
        return

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials = service_account.Credentials.from_service_account_info(
        json.loads(key_json),
        scopes=["https://www.googleapis.com/auth/compute"],
    )
    compute = build("compute", "v1", credentials=credentials)
    compute.instances().reset(
        project=GCP_PROJECT, zone=GCP_ZONE, instance=GCP_INSTANCE
    ).execute()
    logger.warning("VM %s reset triggered", GCP_INSTANCE)


async def maybe_reset_vm() -> str | None:
    """Check VM health and reset if unresponsive for too long.

    Called every minute from callback_minute.
    Resets after CONSECUTIVE_FAILURES_BEFORE_RESET consecutive failures.
    """
    global _consecutive_failures, _last_reset

    if not os.environ.get("GCP_VM_KEY"):
        return None

    # Cooldown after a reset
    now = datetime.now(timezone.utc)
    if _last_reset and (now - _last_reset) < COOLDOWN_AFTER_RESET:
        return None

    if await _check_health():
        if _consecutive_failures > 0:
            msg = (
                f"VM health restored after {_consecutive_failures} consecutive failures"
            )
            logger.info(msg)
            _consecutive_failures = 0
            return msg
        _consecutive_failures = 0
        return None

    _consecutive_failures += 1
    logger.warning(
        "VM health check failed (%d/%d)",
        _consecutive_failures,
        CONSECUTIVE_FAILURES_BEFORE_RESET,
    )

    if _consecutive_failures >= CONSECUTIVE_FAILURES_BEFORE_RESET:
        try:
            _reset_vm()
            _last_reset = now
            _consecutive_failures = 0
            return f"**VM {GCP_INSTANCE} was unresponsive — reset triggered**"
        except Exception:
            logger.exception("Failed to reset VM")
            return f"**VM {GCP_INSTANCE} unresponsive and reset FAILED**"

    return None

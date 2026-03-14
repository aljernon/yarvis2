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
_gave_up = False


def _get_compute_client():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    key_json = os.environ.get("GCP_VM_KEY")
    assert key_json, "GCP_VM_KEY not set"
    credentials = service_account.Credentials.from_service_account_info(
        json.loads(key_json),
        scopes=["https://www.googleapis.com/auth/compute"],
    )
    return build("compute", "v1", credentials=credentials)


def _get_socks_proxy() -> str | None:
    """Return the Tailscale SOCKS5 proxy URL if configured."""
    return os.environ.get("TAILSCALE_SOCKS5_PROXY") or os.environ.get("ALL_PROXY")


async def _check_health() -> bool:
    """Return True if at least one health endpoint responds."""
    proxy = _get_socks_proxy()
    async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT, proxy=proxy) as client:
        for url in HEALTH_ENDPOINTS:
            try:
                resp = await client.get(url)
                if resp.is_success:
                    return True
            except Exception:
                continue
    return False


def _is_vm_running() -> bool:
    """Check if the VM is in RUNNING state via GCP API."""
    try:
        compute = _get_compute_client()
        result = (
            compute.instances()
            .get(project=GCP_PROJECT, zone=GCP_ZONE, instance=GCP_INSTANCE)
            .execute()
        )
        return result.get("status") == "RUNNING"
    except Exception:
        logger.exception("Failed to check VM status")
        return False


def _reset_vm() -> None:
    """Reset the GCP VM using the Compute API."""
    compute = _get_compute_client()
    compute.instances().reset(
        project=GCP_PROJECT, zone=GCP_ZONE, instance=GCP_INSTANCE
    ).execute()
    logger.warning("VM %s reset triggered", GCP_INSTANCE)


async def maybe_reset_vm() -> str | None:
    """Check VM health and reset if unresponsive for too long.

    Called every minute from callback_minute.
    Resets after CONSECUTIVE_FAILURES_BEFORE_RESET consecutive failures.
    If the VM is RUNNING after a reset but still unreachable, assumes
    a network issue and stops resetting.
    """
    global _consecutive_failures, _last_reset, _gave_up

    if not os.environ.get("GCP_VM_KEY"):
        return None

    # Cooldown after a reset
    now = datetime.now(timezone.utc)
    if _last_reset and (now - _last_reset) < COOLDOWN_AFTER_RESET:
        return None

    if await _check_health():
        if _consecutive_failures > 0 or _gave_up:
            msg = (
                f"VM health restored after {_consecutive_failures} consecutive failures"
            )
            if _gave_up:
                msg += " (was in network-issue mode)"
            logger.info(msg)
            _consecutive_failures = 0
            _gave_up = False
            return msg
        _consecutive_failures = 0
        return None

    # Already gave up — just wait for health to recover
    if _gave_up:
        return None

    _consecutive_failures += 1
    logger.warning(
        "VM health check failed (%d/%d)",
        _consecutive_failures,
        CONSECUTIVE_FAILURES_BEFORE_RESET,
    )

    if _consecutive_failures >= CONSECUTIVE_FAILURES_BEFORE_RESET:
        # Before resetting, check if VM is already running (= network issue)
        if _last_reset and _is_vm_running():
            _gave_up = True
            msg = (
                f"**VM {GCP_INSTANCE} is RUNNING but unreachable — "
                f"likely a network issue, not resetting**"
            )
            logger.warning(msg)
            return msg

        try:
            _reset_vm()
            _last_reset = now
            _consecutive_failures = 0
            return f"**VM {GCP_INSTANCE} was unresponsive — reset triggered**"
        except Exception:
            logger.exception("Failed to reset VM")
            return f"**VM {GCP_INSTANCE} unresponsive and reset FAILED**"

    return None

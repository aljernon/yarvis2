"""Periodic message event checker.

Fetches new Signal/SMS/Telegram/Gmail messages since the last check.
If new messages are found, creates a notification in chat history.
"""

import datetime
import logging
import math
import os

import httpx
import pytz
from simplegmail import Gmail

from yarvis_ptb.settings import DEFAULT_TIMEZONE, PROJECT_ROOT, SYSTEM_USER_ID
from yarvis_ptb.storage import DbMessage, VariablesForChat, save_message
from yarvis_ptb.telegram_client import get_recent_messages, telegram_session
from yarvis_ptb.timezones import get_complex_chat_timezone_str

logger = logging.getLogger(__name__)

LAST_CHECK_VAR = "LAST_MESSAGE_CHECK_DATE"
CHECK_INTERVAL_MINUTES = 5

# Filtering config: DMs visible by default, groups hidden unless whitelisted.
FILTER_CONFIG = {
    "signal": {
        "whitelisted_groups": [],
        "blacklisted_contacts": [],
    },
    "sms": {
        "whitelisted_groups": [],
        "blacklisted_contacts": [],
    },
    "telegram": {
        "whitelisted_groups": [
            -1003713018935,  # Ф (family, supergroup ID)
            -705787714,  # Ф (family, group ID)
        ],
        "blacklisted_contacts": [
            "ya42352",  # bot chat
            "clam",  # dev/test chats
        ],
    },
    "gmail": {
        "whitelisted_groups": [],
        "blacklisted_contacts": [],
    },
}


def _should_keep(msg: dict) -> bool:
    """Filter a message based on FILTER_CONFIG. Returns True to keep."""
    cfg = FILTER_CONFIG.get(msg["source"], {})

    # Blacklisted contact check (partial match on from or partner)
    for bl in cfg.get("blacklisted_contacts", []):
        bl_lower = bl.lower()
        if bl_lower in msg["from"].lower() or bl_lower in msg["partner"].lower():
            return False

    # Group chat: hidden unless whitelisted
    if msg.get("is_group"):
        whitelist = cfg.get("whitelisted_groups", [])
        chat_id = msg.get("chat_id")
        partner = msg["partner"]
        # Telegram: whitelist by chat_id (int)
        if chat_id is not None and chat_id in whitelist:
            return True
        # Signal/SMS: whitelist by group name (partial match)
        if any(isinstance(w, str) and w.lower() in partner.lower() for w in whitelist):
            return True
        return False

    return True


def _socks5_proxy() -> str | None:
    return os.environ.get("TAILSCALE_SOCKS5_PROXY")


def _parse_ts(s: str) -> datetime.datetime | None:
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------


async def _fetch_signal(
    since: datetime.datetime, until: datetime.datetime
) -> list[dict]:
    hours = max(math.ceil((until - since).total_seconds() / 3600), 1)
    async with httpx.AsyncClient(proxy=_socks5_proxy(), timeout=10) as client:
        resp = await client.get(
            "http://100.108.7.78:8081/messages",
            params={"hours": hours, "limit": 200},
        )
    resp.raise_for_status()
    raw = resp.json()

    # Build phone→name lookup
    phone_names: dict[str, str] = {}
    for msg in raw:
        if not msg.get("is_sync") and not msg.get("is_group"):
            num = msg.get("source_number", "")
            name = msg.get("source_name", "")
            if num and name:
                phone_names[num] = name

    results = []
    for msg in raw:
        ts = _parse_ts(msg.get("timestamp", ""))
        if ts is None or not (since < ts <= until):
            continue
        is_group = msg.get("is_group", False)
        if msg.get("is_sync"):
            direction = "outgoing"
            from_name = "Anton"
            if is_group:
                partner = msg.get("group_name") or msg.get("group_id", "?")
            else:
                dest = msg.get("destination_number", "")
                partner = (
                    msg.get("destination_name") or phone_names.get(dest, dest) or "?"
                )
        else:
            direction = "incoming"
            from_name = msg.get("source_name") or msg.get("source_number", "?")
            partner = msg.get("group_name") or from_name if is_group else from_name
        text = msg.get("message", "")
        if not text:
            continue
        results.append(
            {
                "source": "signal",
                "ts": ts,
                "from": from_name,
                "direction": direction,
                "partner": partner,
                "is_group": is_group,
                "text": text,
            }
        )
    return results


# ---------------------------------------------------------------------------
# SMS
# ---------------------------------------------------------------------------


async def _fetch_sms(since: datetime.datetime, until: datetime.datetime) -> list[dict]:
    hours = max(math.ceil((until - since).total_seconds() / 3600), 1)
    async with httpx.AsyncClient(proxy=_socks5_proxy(), timeout=10) as client:
        resp = await client.get(
            "http://100.108.7.78:8082/messages",
            params={"hours": hours, "limit": 200},
        )
    resp.raise_for_status()
    raw = resp.json()

    results = []
    for msg in raw:
        ts = _parse_ts(msg.get("timestamp", ""))
        if ts is None or not (since < ts <= until):
            continue
        direction = msg.get("direction", "unknown")
        if direction == "outgoing":
            from_name = "Anton"
            partner = msg.get("partner_name") or msg.get("partner_phone", "?")
        else:
            from_name = msg.get("sender_name") or msg.get("sender", "?")
            partner = from_name
        text = msg.get("message", "")
        if not text:
            continue
        results.append(
            {
                "source": "sms",
                "ts": ts,
                "from": from_name,
                "direction": direction,
                "partner": partner,
                "is_group": False,
                "text": text,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


async def _fetch_telegram(
    since: datetime.datetime, until: datetime.datetime
) -> list[dict]:
    hours = max(math.ceil((until - since).total_seconds() / 3600), 1)
    async with telegram_session() as client:
        raw = await get_recent_messages(client, hours=hours)

    results = []
    for msg in raw:
        ts = _parse_ts(msg.get("timestamp", ""))
        if ts is None or not (since < ts <= until):
            continue
        results.append(
            {
                "source": "telegram",
                "ts": ts,
                "from": msg["from_name"],
                "direction": msg["direction"],
                "partner": msg["conversation_partner"],
                "is_group": msg.get("is_group", False),
                "chat_id": msg.get("chat_id"),
                "text": msg["message"],
            }
        )
    return results


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


def _fetch_gmail(since: datetime.datetime, until: datetime.datetime) -> list[dict]:
    gmail = Gmail(client_secret_file=str(PROJECT_ROOT / "credentials.json"))
    epoch = int(since.timestamp())
    query = f"after:{epoch} in:inbox"
    messages = gmail.get_messages(query=query)

    results = []
    for msg in messages:
        ts = _parse_ts(str(msg.date)) if msg.date else None
        if ts is None or not (since < ts <= until):
            continue
        results.append(
            {
                "source": "gmail",
                "ts": ts,
                "from": str(msg.sender),
                "direction": "incoming",
                "partner": str(msg.sender),
                "is_group": False,
                "text": f"Subject: {msg.subject}",
            }
        )
    return results


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_notification(
    messages: list[dict],
    since: datetime.datetime,
    until: datetime.datetime,
    errors: list[str] | None = None,
) -> str:
    tz_name = get_complex_chat_timezone_str()
    tz = pytz.timezone(tz_name)
    fmt = "%H:%M:%S %Z"
    since_str = since.astimezone(tz).strftime(fmt)
    until_str = until.astimezone(tz).strftime(fmt)

    lines = [f"New messages ({since_str} – {until_str}):"]

    if errors:
        lines.append("\nFETCH ERRORS (data may be incomplete):")
        for err in errors:
            lines.append(f"  - {err}")

    by_source: dict[str, list[dict]] = {}
    for msg in messages:
        by_source.setdefault(msg["source"], []).append(msg)

    for source in ["signal", "sms", "telegram", "gmail"]:
        msgs = by_source.get(source, [])
        if not msgs:
            continue
        lines.append(f"\n{source.upper()}:")
        for msg in msgs:
            ts_str = msg["ts"].astimezone(tz).strftime("%H:%M:%S")
            text = msg["text"][:200] + "..." if len(msg["text"]) > 200 else msg["text"]
            if msg["direction"] == "outgoing":
                who = f"Anton → {msg['partner']}"
            else:
                who = f"{msg['from']} → Anton"
            lines.append(f"  [{ts_str}] {who}: {text}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------


async def check_new_messages(curr, chat_id: int) -> str | None:
    """Check for new messages across all channels.

    Returns the notification text if new messages were found, None otherwise.
    Updates LAST_MESSAGE_CHECK_DATE only when messages are found.
    """
    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    chat_vars = VariablesForChat(curr)
    since = chat_vars.get(LAST_CHECK_VAR)

    if since is None:
        chat_vars.put(LAST_CHECK_VAR, now)
        logger.info("Initialized LAST_MESSAGE_CHECK_DATE to %s", now.isoformat())
        return None

    all_messages: list[dict] = []
    errors: list[str] = []
    for label, fetcher in [
        ("Signal", _fetch_signal(since, now)),
        ("SMS", _fetch_sms(since, now)),
        ("Telegram", _fetch_telegram(since, now)),
    ]:
        try:
            all_messages.extend(await fetcher)
        except Exception as e:
            logger.warning(f"{label} fetch failed: {e}")
            errors.append(f"{label}: {e}")

    # Gmail uses sync simplegmail library
    try:
        all_messages.extend(_fetch_gmail(since, now))
    except Exception as e:
        logger.warning(f"Gmail fetch failed: {e}")
        errors.append(f"Gmail: {e}")

    all_messages = [m for m in all_messages if _should_keep(m)]

    if not all_messages and not errors:
        return None

    all_messages.sort(key=lambda m: m["ts"])
    notification = _format_notification(all_messages, since, now, errors=errors)

    save_message(
        curr,
        DbMessage(
            chat_id=chat_id,
            created_at=now,
            user_id=SYSTEM_USER_ID,
            message=notification,
            meta={"turn_type": "notification"},
        ),
    )

    chat_vars.put(LAST_CHECK_VAR, now)
    failed = ", ".join(e.split(":")[0] for e in errors) if errors else "none"
    logger.info(
        "Message events: %d messages, failed: %s (%s – %s)",
        len(all_messages),
        failed,
        since.isoformat(),
        now.isoformat(),
    )
    return notification


def should_check_messages(now: datetime.datetime, curr) -> bool:
    """Return True if it's time to check for new messages."""
    return now.minute % CHECK_INTERVAL_MINUTES == 0

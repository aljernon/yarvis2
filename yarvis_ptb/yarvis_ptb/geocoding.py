"""Reverse geocoding with a per-coord cache.

Call sites (dashboard, bot tool) should use `resolve_and_format(curr, lat, lon)`
which returns a short human-readable location string like
"INNSIDE New York · Chelsea, Manhattan, New York". The raw Google Geocoding
response is cached in the `geocode_cache` table keyed by rounded lat/lon so
a stationary phone doesn't re-hit the API on every ping.
"""

import datetime
import logging
import os

import psycopg2.extras
import pytz
import requests

from yarvis_ptb.timezones import get_timezone

logger = logging.getLogger(__name__)

# 6 decimals ≈ 0.11 m — basically GPS noise. The phone only pings on movement,
# so cache hit rate is low regardless; better to keep near-identical pings
# distinct than to over-collapse.
COORD_ROUND_DECIMALS = 6

# Short enough that a hung Google call doesn't block an invocation; the context
# block will render "Geocode: timeout" when this trips. Mitigated further by
# pre-warming the cache from the OwnTracks webhook on each new ping.
GEOCODE_TIMEOUT_SEC = 2


def _round_key(lat: float, lon: float) -> tuple[float, float]:
    return round(lat, COORD_ROUND_DECIMALS), round(lon, COORD_ROUND_DECIMALS)


def _get_cached(curr, lat_key: float, lon_key: float) -> dict | None:
    curr.execute(
        "SELECT raw FROM geocode_cache WHERE lat_key = %s AND lon_key = %s",
        (lat_key, lon_key),
    )
    row = curr.fetchone()
    return row[0] if row else None


def _fetch_and_cache(curr, lat_key: float, lon_key: float) -> dict:
    """Call Google Geocoding and cache on success. Raises on timeout / HTTP error."""
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY not configured")
    resp = requests.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={"latlng": f"{lat_key},{lon_key}", "key": api_key},
        timeout=GEOCODE_TIMEOUT_SEC,
    ).json()
    if resp.get("status") != "OK":
        # Don't cache errors — let the next call retry.
        logger.warning(
            "Geocode failed lat=%s lon=%s status=%s err=%s",
            lat_key,
            lon_key,
            resp.get("status"),
            resp.get("error_message"),
        )
        return resp
    curr.execute(
        """
        INSERT INTO geocode_cache (lat_key, lon_key, fetched_at, raw)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (lat_key, lon_key) DO UPDATE
        SET fetched_at = EXCLUDED.fetched_at, raw = EXCLUDED.raw
        """,
        (
            lat_key,
            lon_key,
            datetime.datetime.now(pytz.UTC),
            psycopg2.extras.Json(resp),
        ),
    )
    return resp


def get_cached_or_fetch(curr, lat: float, lon: float) -> dict:
    lat_key, lon_key = _round_key(lat, lon)
    cached = _get_cached(curr, lat_key, lon_key)
    if cached is not None:
        return cached
    return _fetch_and_cache(curr, lat_key, lon_key)


def _resolve_with_error(curr, lat: float, lon: float) -> tuple[str, str | None]:
    """Returns (formatted_geo_block, error_label). error_label is
    "timeout" / "unavailable" / None.
    """
    try:
        raw = get_cached_or_fetch(curr, lat, lon)
    except requests.Timeout:
        return "", "timeout"
    except Exception as e:
        logger.warning("Geocode failed lat=%s lon=%s: %s", lat, lon, e)
        return "", "unavailable"
    block = format_geocode(raw)
    if not block:
        return "", "unavailable"
    return block, None


def _first_component(results: list[dict], type_name: str) -> str | None:
    for r in results:
        for c in r.get("address_components", []):
            if type_name in c.get("types", []):
                return c.get("long_name")
    return None


def _first_result_with_type(results: list[dict], type_name: str) -> dict | None:
    for r in results:
        if type_name in r.get("types", []):
            return r
    return None


def format_geocode(raw: dict) -> str:
    """Pull best-guess fields out of a reverse-geocode response and format as
    a labeled multi-line string — the same shape that gets shown to Claude
    and rendered in the locations dashboard.
    """
    if raw.get("status") != "OK":
        return ""
    results = raw.get("results", [])
    if not results:
        return ""

    entity = _first_component(results, "premise") or ""

    # Best address: first result that has a `route` but no `premise` component —
    # keeps formatted_address as a clean "<num> <street>, city, ..." and avoids
    # the "INNSIDE New York, 132 W 27th St, ..." variant.
    address = ""
    for r in results:
        comps = r.get("address_components", [])
        has_premise = any("premise" in c.get("types", []) for c in comps)
        has_route = any("route" in c.get("types", []) for c in comps)
        if has_route and not has_premise:
            address = r.get("formatted_address", "")
            break

    area_parts: list[str] = []
    for t in ("neighborhood", "sublocality_level_1", "locality"):
        v = _first_component(results, t)
        if v and v not in area_parts:
            area_parts.append(v)
    area = ", ".join(area_parts)

    lines = []
    if entity:
        lines.append(f"Entity: {entity}")
    if address:
        lines.append(f"Address: {address}")
    if area:
        lines.append(f"Area: {area}")
    return "\n".join(lines)


def resolve_and_format(curr, lat: float, lon: float) -> str:
    """Cached reverse-geocode + format. Returns "" on API failure."""
    block, _err = _resolve_with_error(curr, lat, lon)
    return block


def _relative_time(tst: datetime.datetime) -> str:
    now = datetime.datetime.now(pytz.UTC)
    delta = now - tst
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def format_location_message(
    curr,
    lat: float,
    lon: float,
    tst: datetime.datetime,
    acc: float | None = None,
) -> str:
    """Full human-readable location block — the exact string we show to Claude
    and render in the dashboard Location column.
    """
    tz = get_timezone(complex_chat=True)
    local_tst = tst.astimezone(tz)
    time_str = local_tst.strftime("%Y-%m-%d %H:%M:%S %Z")
    header = (
        f"Here's user's location as reported by the phone at "
        f"{time_str} ({_relative_time(tst)}):"
    )
    geo_block, err = _resolve_with_error(curr, lat, lon)
    coords = f"Coords: {lat:.6f}, {lon:.6f}"
    if acc is not None:
        coords += f" (acc: {acc:.0f}m)"
    parts = [header]
    if geo_block:
        parts.append(geo_block)
    if err:
        parts.append(f"Geocode: {err}")
    parts.append(coords)
    return "\n".join(parts)

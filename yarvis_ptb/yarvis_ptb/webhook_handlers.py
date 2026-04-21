"""Custom webhook handlers (non-Telegram) added to the PTB tornado app."""

import base64
import datetime
import hmac
import json
import logging
import os

import psycopg2.extras
import pytz
import tornado.web

from yarvis_ptb.geocoding import get_cached_or_fetch
from yarvis_ptb.message_search import save_message_and_update_index
from yarvis_ptb.settings import ROOT_USER_ID, SYSTEM_USER_ID
from yarvis_ptb.storage import DbMessage
from yarvis_ptb.timezones import set_timezone
from yarvis_ptb.tools.scheduling_tools import reanchor_cron_schedules

logger = logging.getLogger(__name__)


class TimezoneHandler(tornado.web.RequestHandler):
    def initialize(self, conn):
        self.conn = conn

    def post(self):
        # Auth check
        secret = os.environ.get("WEBHOOK_SECRET")
        if not secret:
            logger.error("WEBHOOK_SECRET not configured")
            self.set_status(500)
            self.write({"error": "WEBHOOK_SECRET not configured"})
            return

        auth_header = self.request.headers.get("Authorization", "")
        if auth_header != f"Bearer {secret}":
            self.set_status(401)
            self.write({"error": "unauthorized"})
            return

        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Invalid JSON body: {self.request.body!r}")
            self.set_status(400)
            self.write({"error": "invalid JSON"})
            return

        logger.info(f"Timezone request body: {body}")
        new_tz = body.get("timezone")
        if not new_tz:
            self.set_status(400)
            self.write({"error": "missing 'timezone' field"})
            return

        # Validate timezone
        try:
            pytz.timezone(new_tz)
        except pytz.exceptions.UnknownTimeZoneError:
            self.set_status(400)
            self.write({"error": f"unknown timezone: {new_tz}"})
            return

        old_tz = set_timezone(new_tz)

        if old_tz == new_tz:
            logger.info(f"Timezone unchanged: {new_tz}")
            self.write({"old": old_tz, "new": new_tz, "changed": False})
            return

        # Re-anchor cron schedules + save a system message about it
        with self.conn.cursor() as curr:
            reanchored, fired_immediate = reanchor_cron_schedules(curr, ROOT_USER_ID)
            reanchor_note = (
                f" Re-anchored {reanchored} cron schedule(s) to the new timezone"
                f"; {fired_immediate} will fire once now to catch a skipped run."
                if reanchored
                else ""
            )
            save_message_and_update_index(
                curr,
                DbMessage(
                    chat_id=ROOT_USER_ID,
                    created_at=datetime.datetime.now(pytz.UTC),
                    user_id=SYSTEM_USER_ID,
                    message=(
                        f"User's phone timezone changed: {old_tz} \u2192 {new_tz}. "
                        f"System timezone updated accordingly. "
                        f"All scheduled invocations, time references, and "
                        f'"today"/"tomorrow" boundaries now use the new timezone.'
                        f"{reanchor_note}"
                    ),
                ),
            )

        logger.info(f"Timezone changed: {old_tz} -> {new_tz}")
        self.write({"old": old_tz, "new": new_tz, "changed": True})


class OwntracksHandler(tornado.web.RequestHandler):
    """Accepts OwnTracks HTTP posts.

    OwnTracks uses HTTP Basic auth natively, so we parse the Authorization
    header and compare the password half against OWNTRACKS_SECRET. The
    username is ignored.
    """

    def initialize(self, conn):
        self.conn = conn

    def post(self):
        secret = os.environ.get("OWNTRACKS_SECRET")
        if not secret:
            logger.error("OWNTRACKS_SECRET not configured")
            self.set_status(500)
            self.write({"error": "OWNTRACKS_SECRET not configured"})
            return

        auth_header = self.request.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            self.set_status(401)
            self.write({"error": "unauthorized"})
            return
        try:
            _user, _, password = (
                base64.b64decode(auth_header[6:]).decode().partition(":")
            )
        except Exception:
            self.set_status(401)
            self.write({"error": "unauthorized"})
            return
        if not hmac.compare_digest(password, secret):
            self.set_status(401)
            self.write({"error": "unauthorized"})
            return

        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"OwnTracks invalid JSON: {self.request.body!r}")
            self.set_status(400)
            self.write({"error": "invalid JSON"})
            return

        event_type = body.get("_type", "")
        # OwnTracks sends location pings, transitions, waypoint updates,
        # lwt/beacon/steps/cmd/etc. We only persist the ones with a lat/lon.
        if event_type not in ("location", "transition"):
            logger.info(f"OwnTracks ignoring _type={event_type!r}")
            self.write({"ok": True, "stored": False, "_type": event_type})
            return

        try:
            lat = float(body["lat"])
            lon = float(body["lon"])
            tst = datetime.datetime.fromtimestamp(int(body["tst"]), tz=pytz.UTC)
        except (KeyError, TypeError, ValueError):
            logger.warning(f"OwnTracks missing lat/lon/tst: {body!r}")
            self.set_status(400)
            self.write({"error": "missing lat/lon/tst"})
            return

        topic = self.request.headers.get("X-Limit-U", "") or body.get("topic", "")
        tid = body.get("tid")
        now = datetime.datetime.now(pytz.UTC)

        with self.conn.cursor() as curr:
            curr.execute(
                """
                INSERT INTO locations
                    (created_at, tst, lat, lon, acc, alt, vel, batt,
                     tid, topic, event_type, meta)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    now,
                    tst,
                    lat,
                    lon,
                    body.get("acc"),
                    body.get("alt"),
                    body.get("vel"),
                    body.get("batt"),
                    tid,
                    topic,
                    event_type,
                    psycopg2.extras.Json(body),
                ),
            )

            if event_type == "transition":
                event = body.get("event", "?")  # "enter" | "leave"
                desc = body.get("desc") or body.get("wtst") or "(unnamed waypoint)"
                save_message_and_update_index(
                    curr,
                    DbMessage(
                        chat_id=ROOT_USER_ID,
                        created_at=now,
                        user_id=SYSTEM_USER_ID,
                        message=(
                            f"Geofence {event}: {desc} "
                            f"({lat:.5f}, {lon:.5f}) at "
                            f"{tst.isoformat()}"
                        ),
                    ),
                )

            # Pre-warm the geocode cache so the next context build doesn't
            # have to wait on Google. Best-effort.
            try:
                get_cached_or_fetch(curr, lat, lon)
            except Exception as e:
                logger.warning("OwnTracks geocode prewarm failed: %s", e)

        logger.info(
            f"OwnTracks stored {event_type} lat={lat:.5f} lon={lon:.5f} tid={tid}"
        )
        self.write({"ok": True, "stored": True, "_type": event_type})

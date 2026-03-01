"""Custom webhook handlers (non-Telegram) added to the PTB tornado app."""

import datetime
import json
import logging
import os

import pytz
import tornado.web

from yarvis_ptb.message_search import save_message_and_update_index
from yarvis_ptb.settings import ROOT_USER_ID, SYSTEM_USER_ID
from yarvis_ptb.storage import DbMessage
from yarvis_ptb.timezones import set_timezone

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

        # Save system message
        with self.conn.cursor() as curr:
            save_message_and_update_index(
                curr,
                DbMessage(
                    chat_id=ROOT_USER_ID,
                    created_at=datetime.datetime.now(pytz.UTC),
                    user_id=SYSTEM_USER_ID,
                    message=(
                        f"System timezone updated: {old_tz} \u2192 {new_tz}. "
                        f"All scheduled invocations, time references, and "
                        f'"today"/"tomorrow" boundaries now use the new timezone.'
                    ),
                ),
            )

        logger.info(f"Timezone changed: {old_tz} -> {new_tz}")
        self.write({"old": old_tz, "new": new_tz, "changed": True})

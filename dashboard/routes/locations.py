"""Locations listing + resolve route."""

import os

import psycopg2.extras
import requests
from flask import Blueprint, jsonify

from dashboard.helpers import get_db
from yarvis_ptb.geocoding import format_location_message

bp = Blueprint("locations", __name__)


@bp.route("/api/locations")
def api_locations():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, created_at, tst, lat, lon, acc, alt, vel, batt,
                       tid, topic, event_type, meta
                FROM locations
                ORDER BY tst DESC
                LIMIT 100
            """)
            rows = cur.fetchall()

        locations = []
        with conn.cursor() as geo_cur:
            for r in rows:
                locations.append(
                    {
                        "id": r["id"],
                        "created_at": r["created_at"].isoformat()
                        if r["created_at"]
                        else None,
                        "tst": r["tst"].isoformat() if r["tst"] else None,
                        "lat": r["lat"],
                        "lon": r["lon"],
                        "acc": r["acc"],
                        "alt": r["alt"],
                        "vel": r["vel"],
                        "batt": r["batt"],
                        "tid": r["tid"],
                        "topic": r["topic"],
                        "event_type": r["event_type"],
                        "formatted": format_location_message(
                            geo_cur, r["lat"], r["lon"], r["tst"], r["acc"]
                        ),
                    }
                )

        return jsonify({"locations": locations})
    finally:
        conn.close()


@bp.route("/api/locations/<int:loc_id>/resolve", methods=["POST"])
def api_locations_resolve(loc_id: int):
    """The "magic" — reverse-geocode the given location via Google Maps."""
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        return jsonify({"error": "GOOGLE_PLACES_API_KEY not configured"}), 500

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT lat, lon FROM locations WHERE id = %s", (loc_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "not found"}), 404

        latlng = f"{row['lat']},{row['lon']}"

        geo_resp = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"latlng": latlng, "key": api_key},
            timeout=10,
        ).json()
        places_resp = requests.get(
            "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
            params={"location": latlng, "radius": 50, "key": api_key},
            timeout=10,
        ).json()

        return jsonify({"geocode": geo_resp, "places": places_resp})
    finally:
        conn.close()

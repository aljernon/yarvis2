#!/usr/bin/env python3
"""One-time OAuth2 flow to get Google Nest (SDM) API tokens.

Prerequisites:
  1. Register at https://console.nest.google.com/device-access ($5)
  2. Create a GCP OAuth 2.0 Web Application credential
  3. Set redirect URI to http://localhost:8765/callback
  4. Set NEST_CLIENT_ID, NEST_CLIENT_SECRET, and NEST_PROJECT_ID env vars (or in .env)

Usage:
  python nest_auth.py
"""

import json
import os
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent
load_dotenv(PROJECT_ROOT / ".env")

CLIENT_ID = os.environ.get("NEST_CLIENT_ID")
CLIENT_SECRET = os.environ.get("NEST_CLIENT_SECRET")
PROJECT_ID = os.environ.get("NEST_PROJECT_ID")
REDIRECT_URI = "http://localhost:8765/callback"
CONFIG_PATH = PROJECT_ROOT / "nest_config.json"
TOKEN_PATH = PROJECT_ROOT / "nest_token.json"

# SDM API requires these scopes
SCOPES = "https://www.googleapis.com/auth/sdm.service"
AUTH_URL = "https://nestservices.google.com/partnerconnections/{project_id}/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def main():
    if not CLIENT_ID or not CLIENT_SECRET or not PROJECT_ID:
        print(
            "ERROR: Set NEST_CLIENT_ID, NEST_CLIENT_SECRET, and NEST_PROJECT_ID env vars first."
        )
        sys.exit(1)

    auth_code_holder = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if "code" in query:
                auth_code_holder["code"] = query["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Auth successful! You can close this tab.</h1>")
            elif "error" in query:
                error_msg = f"OAuth error: {query['error'][0]}: {query.get('error_description', [''])[0]}"
                print(f"ERROR: {error_msg}")
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(error_msg.encode())
                sys.exit(1)
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing code parameter")

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("localhost", 8765), CallbackHandler)

    state = secrets.token_urlsafe(32)
    params = urlencode(
        {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    full_auth_url = AUTH_URL.format(project_id=PROJECT_ID) + "?" + params
    print("Opening browser for Nest authorization...")
    print(f"URL: {full_auth_url}")
    webbrowser.open(full_auth_url)

    print("Waiting for callback...")
    while "code" not in auth_code_holder:
        server.handle_request()

    code = auth_code_holder["code"]
    print("Got authorization code. Exchanging for token...")

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
        },
    )
    resp.raise_for_status()
    token_data = resp.json()

    config = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "project_id": PROJECT_ID,
        "redirect_uri": REDIRECT_URI,
    }
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    print(f"Config saved to {CONFIG_PATH}")

    from datetime import datetime, timezone

    token_save = {
        "access_token": token_data["access_token"],
        "expires_in": token_data.get("expires_in", 3600),
        "refresh_token": token_data.get("refresh_token"),
        "token_type": token_data.get("token_type", "Bearer"),
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    with open(TOKEN_PATH, "w") as f:
        json.dump(token_save, f, indent=2)
    print(f"Token saved to {TOKEN_PATH}")

    print("\nDone! Nest tools are now enabled.")
    print("Testing API access...")

    # Quick test: list devices
    headers = {"Authorization": f"Bearer {token_save['access_token']}"}
    test_resp = requests.get(
        f"https://smartdevicemanagement.googleapis.com/v1/enterprises/{PROJECT_ID}/devices",
        headers=headers,
    )
    if test_resp.ok:
        devices = test_resp.json().get("devices", [])
        print(f"Found {len(devices)} device(s):")
        for d in devices:
            dtype = d.get("type", "unknown").split(".")[-1]
            name = (
                d.get("traits", {})
                .get("sdm.devices.traits.Info", {})
                .get("customName", "unnamed")
            )
            print(f"  - {name} ({dtype})")
    else:
        print(f"API test failed ({test_resp.status_code}): {test_resp.text}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""One-time OAuth2 flow to get Whoop API tokens.

Prerequisites:
  1. Register an app at https://developer.whoop.com
  2. Set redirect URI to http://localhost:8765/callback
  3. Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET env vars (or in .env)

Usage:
  python whoop_auth.py
"""

import json
import os
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent
load_dotenv(PROJECT_ROOT / ".env")

CLIENT_ID = os.environ.get("WHOOP_CLIENT_ID")
CLIENT_SECRET = os.environ.get("WHOOP_CLIENT_SECRET")
REDIRECT_URI = "http://localhost:8765/callback"
CONFIG_PATH = PROJECT_ROOT / "whoop_config.json"
TOKEN_PATH = PROJECT_ROOT / "whoop_token.json"

SCOPES = "read:recovery read:sleep read:workout read:cycles read:profile read:body_measurement"
AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET env vars first.")
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

    # Build authorization URL (state param required, min 8 chars)
    state = secrets.token_urlsafe(32)
    params = (
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={SCOPES}"
        f"&state={state}"
    )
    full_auth_url = AUTH_URL + params
    print("Opening browser for Whoop authorization...")
    print(f"URL: {full_auth_url}")
    webbrowser.open(full_auth_url)

    # Wait for callback
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

    # Save config file (client credentials for whoopy's from_config)
    config = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
    }
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    print(f"Config saved to {CONFIG_PATH}")

    # Save token file in whoopy's TokenInfo format
    token_save = {
        "access_token": token_data["access_token"],
        "expires_in": token_data.get("expires_in", 3600),
        "refresh_token": token_data.get("refresh_token"),
        "scopes": token_data.get("scope", SCOPES).split(),
        "token_type": token_data.get("token_type", "Bearer"),
    }
    with open(TOKEN_PATH, "w") as f:
        json.dump(token_save, f, indent=2)
    print(f"Token saved to {TOKEN_PATH}")

    print("Done! Whoop tools are now enabled.")


if __name__ == "__main__":
    main()

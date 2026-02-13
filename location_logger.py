import json
import logging
import os
from datetime import datetime

import requests
from flask import Flask, Response, request

# Import the camera app package
from yarvis_ptb.yarvis_ptb.settings import LOCATION_PATH
from yarvis_ptb.yarvis_ptb.timezones import get_timezone

app = Flask(__name__)

logger = logging.getLogger(__name__)

logging.basicConfig(level=logging.INFO)

# Must be set.
CUSTOM_TELEGRAM_BOT_PORT = int(os.environ["CUSTOM_TELEGRAM_BOT_PORT"])
DEVICE_ID = "189627sTL#"


@app.route("/", methods=["GET", "POST"])
def log_request():
    # Get all URL parameters
    params = dict(request.args)

    # Check if it's a GET request with no parameters - serve info page
    if request.method == "GET" and not params:
        return "Location Logger Running", 200

    device_id = params["id"]
    if device_id != DEVICE_ID:
        logger.error(f"Invalid device ID: {device_id}")
        return "OK", 200

    params["recorded_at"] = (
        datetime.now().astimezone(get_timezone(complex_chat=True)).isoformat()
    )

    LOCATION_PATH.write_text(json.dumps(params))

    return "OK", 200


@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def proxy(path):
    # Skip proxying for the paths we handle directly
    if path.startswith("camera"):
        return "Not found", 404

    # Construct the URL for the telegram bot
    target_url = f"http://localhost:{CUSTOM_TELEGRAM_BOT_PORT}/{path}"

    # Forward the request
    resp = requests.request(
        method=request.method,
        url=target_url,
        headers={key: value for (key, value) in request.headers if key != "Host"},
        data=request.get_data(),
        cookies=request.cookies,
        allow_redirects=False,
    )

    # Forward the response
    excluded_headers = [
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
    ]
    headers = [
        (name, value)
        for (name, value) in resp.raw.headers.items()
        if name.lower() not in excluded_headers
    ]

    return Response(resp.content, resp.status_code, headers)


if __name__ == "__main__":
    # Get port from Heroku environment, default to 5000
    port = int(os.environ.get("PORT", 5000))
    # Run the app
    app.run(host="0.0.0.0", port=port)

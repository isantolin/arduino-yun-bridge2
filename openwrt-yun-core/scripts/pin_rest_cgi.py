#!/usr/bin/env python3
"""This file is part of Arduino Yun Ecosystem v2.

Copyright (C) 2025 Ignacio Santolin and contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

---

CGI script for YunWebUI v2 REST generic pin control
This script now uses MQTT to communicate with the bridge daemon, avoiding
serial port conflicts.

Expects POST /pin/<N> with JSON body {"state": "ON"|"OFF"}
Controls any digital pin. The 'pin' parameter is required in the URL.
"""
import json
import logging
import os
import re
import sys
from typing import Any, Dict

import paho.mqtt.client as mqtt
from yunrpc.utils import get_uci_config

# Configure logger to output to stdout
logger = logging.getLogger("yunbridge")
if not logger.hasHandlers():
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

# --- MQTT and Topic Configuration ---
CFG = get_uci_config()
MQTT_HOST = CFG.get("mqtt_host", "127.0.0.1")
MQTT_PORT = int(CFG.get("mqtt_port", 1883))
TOPIC_PREFIX = CFG.get("mqtt_topic", "br")  # Default topic prefix


def get_pin_from_path() -> str | None:
    """Extract the pin number from the PATH_INFO value."""

    path = os.environ.get("PATH_INFO", "")
    match = re.match(r"/pin/(\d+)", path)
    return match.group(1) if match else None


def send_response(status_code: int, data: Dict[str, Any]) -> None:
    """Emit a JSON response suitable for CGI scripts."""

    print(f"Status: {status_code}")
    print("Content-Type: application/json\n")
    print(json.dumps(data))


def main() -> None:
    """Main CGI script logic."""
    method = os.environ.get("REQUEST_METHOD", "GET").upper()
    pin = get_pin_from_path()
    logger.info(f"REST call: method={method}, pin={pin}")

    if not pin or not pin.isdigit():
        logger.error("Failed: pin parameter missing or invalid.")
        send_response(
            400,
            {
                "status": "error",
                "message": "Pin must be specified in the URL as /pin/<N>.",
            },
        )
        return

    if method == "GET":
        send_response(
            405,
            {
                "status": "error",
                "message": (
                    "GET method not supported. Pin status is available via "
                    "MQTT subscription."
                ),
            },
        )
        return

    if method == "POST":
        try:
            content_length = int(os.environ.get("CONTENT_LENGTH", 0))
            body = sys.stdin.read(content_length) if content_length > 0 else ""
            data: Dict[str, Any] = json.loads(body) if body else {}
            state = str(data.get("state", "")).upper()
        except (ValueError, json.JSONDecodeError):
            logger.exception("POST body parse error")
            send_response(
                400,
                {"status": "error", "message": "Invalid JSON body."},
            )
            return

        if state not in ("ON", "OFF"):
            send_response(
                400,
                {
                    "status": "error",
                    "message": 'State must be "ON" or "OFF".',
                },
            )
            return

        payload = "1" if state == "ON" else "0"
        topic = f"{TOPIC_PREFIX}/d/{pin}"

        try:
            client = mqtt.Client()
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            client.publish(topic, payload)
            client.disconnect()

            logger.info(
                "Success: Published to %s with payload %s", topic, payload
            )
            send_response(
                200,
                {
                    "status": "ok",
                    "pin": int(pin),
                    "state": state,
                    "message": (
                        f"Command to turn pin {pin} {state} sent via MQTT."
                    ),
                },
            )
        except Exception as exc:
            logger.exception("MQTT Error for pin %s: %s", pin, exc)
            send_response(
                500,
                {
                    "status": "error",
                    "message": (
                        f"Failed to send command for pin {pin} via MQTT: {exc}"
                    ),
                },
            )
        return

    send_response(
        405,
        {
            "status": "error",
            "message": f"Method {method} not allowed.",
        },
    )


if __name__ == "__main__":
    main()

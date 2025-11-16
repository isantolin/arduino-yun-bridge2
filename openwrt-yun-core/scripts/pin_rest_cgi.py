#!/usr/bin/env python3
"""CGI helper that toggles digital pins via MQTT using mosquitto_pub."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, Optional

from yunrpc.utils import get_uci_config

logger = logging.getLogger("yunbridge.pin_rest")
if not logger.hasHandlers():
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

CFG = get_uci_config()
MQTT_HOST = CFG.get("mqtt_host", "127.0.0.1")
MQTT_PORT = int(CFG.get("mqtt_port", 1883))
MQTT_USER = CFG.get("mqtt_user")
MQTT_PASS = CFG.get("mqtt_pass")
TOPIC_PREFIX = CFG.get("mqtt_topic", "br")

MOSQUITTO_PUB = os.environ.get("MOSQUITTO_PUB", "mosquitto_pub")
PUBLISH_RETRIES = max(1, int(os.environ.get("YUNBRIDGE_MQTT_RETRIES", "3")))
PUBLISH_TIMEOUT = max(
    1.0,
    float(os.environ.get("YUNBRIDGE_MQTT_TIMEOUT", "4.0")),
)
RETRY_DELAY = max(0.1, float(os.environ.get("YUNBRIDGE_MQTT_BACKOFF", "0.5")))


def _command_args(topic: str, payload: str) -> list[str]:
    args = [
        MOSQUITTO_PUB,
        "-h",
        MQTT_HOST,
        "-p",
        str(MQTT_PORT),
        "-t",
        topic,
        "-m",
        payload,
        "-q",
        "1",
    ]
    if MQTT_USER:
        args.extend(["-u", MQTT_USER])
    if MQTT_PASS:
        args.extend(["-P", MQTT_PASS])
    return args


def publish_with_retries(topic: str, payload: str) -> None:
    if shutil.which(MOSQUITTO_PUB) is None:
        raise FileNotFoundError(
            f"Executable '{MOSQUITTO_PUB}' not found in PATH"
        )

    last_error: Optional[Exception] = None
    for attempt in range(1, PUBLISH_RETRIES + 1):
        try:
            logger.debug(
                "mosquitto_pub attempt %d: %s -> %s",
                attempt,
                topic,
                payload,
            )
            subprocess.run(
                _command_args(topic, payload),
                check=True,
                timeout=PUBLISH_TIMEOUT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            logger.info("Published to %s with payload %s", topic, payload)
            return
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            last_error = exc
            logger.warning(
                "mosquitto_pub attempt %d failed: %s",
                attempt,
                getattr(exc, "stderr", exc),
            )
            if attempt < PUBLISH_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
    if last_error:
        raise last_error


def get_pin_from_path() -> str | None:
    path = os.environ.get("PATH_INFO", "")
    match = re.match(r"/pin/(\d+)", path)
    return match.group(1) if match else None


def send_response(status_code: int, data: Dict[str, Any]) -> None:
    print(f"Status: {status_code}")
    print("Content-Type: application/json\n")
    print(json.dumps(data))


def main() -> None:
    method = os.environ.get("REQUEST_METHOD", "GET").upper()
    pin = get_pin_from_path()
    logger.info("REST call: method=%s pin=%s", method, pin)

    if not pin or not pin.isdigit():
        send_response(
            400,
            {
                "status": "error",
                "message": "Pin must be specified in the URL as /pin/<N>.",
            },
        )
        return

    if method != "POST":
        send_response(
            405,
            {
                "status": "error",
                "message": (
                    "Only POST is supported. Subscribe via MQTT for state."
                ),
            },
        )
        return

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

    topic = f"{TOPIC_PREFIX}/d/{pin}"
    payload = "1" if state == "ON" else "0"

    try:
        publish_with_retries(topic, payload)
        send_response(
            200,
            {
                "status": "ok",
                "pin": int(pin),
                "state": state,
                "message": f"Command to turn pin {pin} {state} sent via MQTT.",
            },
        )
    except Exception as exc:  # pragma: no cover - protective guard
        logger.exception("MQTT publish failed for pin %s", pin)
        send_response(
            500,
            {
                "status": "error",
                "message": f"Failed to send command for pin {pin}: {exc}",
            },
        )


if __name__ == "__main__":
    main()

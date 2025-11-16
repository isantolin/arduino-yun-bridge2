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
import time
from typing import Any, Callable, Dict, Optional

import paho.mqtt.client as mqtt
from yunrpc.utils import get_uci_config
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

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

DEFAULT_RETRIES = max(1, int(os.environ.get("YUNBRIDGE_MQTT_RETRIES", "3")))
DEFAULT_PUBLISH_TIMEOUT = max(
    1.0,
    float(os.environ.get("YUNBRIDGE_MQTT_TIMEOUT", "4.0")),
)
DEFAULT_BACKOFF_BASE = max(
    0.1,
    float(os.environ.get("YUNBRIDGE_MQTT_BACKOFF", "0.5")),
)
MAX_BACKOFF_SECONDS = max(
    DEFAULT_BACKOFF_BASE,
    float(os.environ.get("YUNBRIDGE_MQTT_BACKOFF_MAX", "4.0")),
)


def _wait_for_publish(
    info: mqtt.MQTTMessageInfo,
    timeout: float,
    sleep_fn: Callable[[float], None],
) -> None:
    deadline = time.monotonic() + timeout
    while not info.is_published():
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"MQTT publish timed out after {timeout:.2f}s"
            )
        sleep_fn(0.05)


def _perform_publish(
    topic: str,
    payload: str,
    timeout: float,
    sleep_fn: Callable[[float], None],
) -> None:
    client = mqtt.Client()
    connected = False
    loop_started = False
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        connected = True
        client.loop_start()
        loop_started = True
        info = client.publish(topic, payload, qos=1, retain=False)
        _wait_for_publish(info, timeout, sleep_fn)
    finally:
        if loop_started:
            client.loop_stop()
        if connected:
            try:
                client.disconnect()
            except Exception:  # pragma: no cover - defensive close guard
                logger.debug("Ignoring MQTT disconnect error", exc_info=True)


def publish_with_retries(
    topic: str,
    payload: str,
    *,
    retries: Optional[int] = None,
    publish_timeout: Optional[float] = None,
    base_delay: Optional[float] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    attempts = retries or DEFAULT_RETRIES
    timeout = publish_timeout or DEFAULT_PUBLISH_TIMEOUT
    base = base_delay or DEFAULT_BACKOFF_BASE

    retryer = Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=base, max=MAX_BACKOFF_SECONDS),
        retry=retry_if_exception_type(Exception),
        reraise=True,
        sleep=sleep_fn,
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )

    logger.debug(
        (
            "Publishing to MQTT with retries=%d base_delay=%.2f "
            "timeout=%.2f topic=%s"
        ),
        attempts,
        base,
        timeout,
        topic,
    )
    retryer(_perform_publish, topic, payload, timeout, sleep_fn)
    logger.info(
        "MQTT publish to %s succeeded after %d attempt(s)",
        topic,
        retryer.statistics.get("attempt_number", 1),
    )


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
            publish_with_retries(topic, payload)
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

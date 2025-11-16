#!/usr/bin/env python3
"""CGI helper that toggles digital pins via MQTT using paho-mqtt."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional

try:  # pragma: no cover - replaced with test doubles when needed
    from paho.mqtt import client as mqtt  # type: ignore[import]
except ImportError:  # pragma: no cover - ensures attribute for monkeypatching
    class _MissingClient:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise RuntimeError(
                "paho-mqtt package not available; "
                "install it to use pin_rest_cgi"
            )

    mqtt = SimpleNamespace(Client=_MissingClient)  # type: ignore[assignment]

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


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


DEFAULT_RETRIES = _env_int("YUNBRIDGE_MQTT_RETRIES", 3)
DEFAULT_PUBLISH_TIMEOUT = _env_float("YUNBRIDGE_MQTT_TIMEOUT", 4.0, 0.0)
DEFAULT_BACKOFF_BASE = _env_float("YUNBRIDGE_MQTT_BACKOFF", 0.5, 0.0)
DEFAULT_POLL_INTERVAL = _env_float("YUNBRIDGE_MQTT_POLL_INTERVAL", 0.05, 0.001)


def publish_with_retries(
    topic: str,
    payload: str,
    retries: int = DEFAULT_RETRIES,
    publish_timeout: float = DEFAULT_PUBLISH_TIMEOUT,
    base_delay: float = DEFAULT_BACKOFF_BASE,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """Publish an MQTT message with retry and timeout semantics."""

    if retries <= 0:
        raise ValueError("retries must be a positive integer")
    if poll_interval <= 0:
        poll_interval = DEFAULT_POLL_INTERVAL

    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        client = mqtt.Client()
        if MQTT_USER:
            client.username_pw_set(MQTT_USER, MQTT_PASS)

        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            loop_start = getattr(client, "loop_start", None)
            if callable(loop_start):
                loop_start()

            result = client.publish(topic, payload, qos=1, retain=False)

            if publish_timeout <= 0:
                raise TimeoutError("MQTT publish timed out before completion")

            deadline = time.monotonic() + publish_timeout
            while not result.is_published():
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        (
                            "MQTT publish timed out after "
                            f"{publish_timeout} seconds"
                        )
                    )
                sleep_fn(poll_interval)

            logger.info("Published to %s with payload %s", topic, payload)
            return
        except Exception as exc:  # pragma: no cover - exercised via tests
            last_error = exc
            logger.warning("MQTT publish attempt %d failed: %s", attempt, exc)
        finally:
            for cleanup in ("loop_stop", "disconnect"):
                method = getattr(client, cleanup, None)
                if callable(method):
                    try:
                        method()
                    except Exception:  # pragma: no cover - defensive cleanup
                        logger.debug(
                            "Cleanup %s failed", cleanup, exc_info=True
                        )

        if attempt < retries:
            sleep_fn(base_delay * attempt)

    if last_error:
        raise last_error
    raise TimeoutError("MQTT publish failed without explicit error detail")


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

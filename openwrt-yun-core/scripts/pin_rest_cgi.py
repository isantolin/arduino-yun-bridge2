#!/usr/bin/env python3
"""CGI helper that toggles digital pins via MQTT using paho-mqtt."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from typing import Any

from types import SimpleNamespace

from paho.mqtt.client import Client, MQTTv5
from paho.mqtt.enums import CallbackAPIVersion

from yunbridge.config.logging import configure_logging
from yunbridge.config.settings import RuntimeConfig, load_runtime_config
from yunbridge.const import DEFAULT_MQTT_TOPIC


logger = logging.getLogger("yunbridge.pin_rest")


def _configure_fallback_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


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


def _resolve_tls_material(config: RuntimeConfig) -> SimpleNamespace | None:
    cafile = os.environ.get("YUNBRIDGE_PIN_CAFILE") or config.mqtt_cafile
    if not cafile:
        return None

    return SimpleNamespace(
        cafile=cafile,
        certfile=os.environ.get("YUNBRIDGE_PIN_CERTFILE") or config.mqtt_certfile,
        keyfile=os.environ.get("YUNBRIDGE_PIN_KEYFILE") or config.mqtt_keyfile,
    )


def publish_with_retries(
    topic: str,
    payload: str,
    config: RuntimeConfig,
    retries: int = DEFAULT_RETRIES,
    publish_timeout: float = DEFAULT_PUBLISH_TIMEOUT,
    base_delay: float = DEFAULT_BACKOFF_BASE,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """Publish an MQTT message with retry and timeout semantics."""
    tls_material = _resolve_tls_material(config) if config.mqtt_tls else None

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        client = Client(
            client_id=f"yunbridge_cgi_{time.time()}",
            protocol=MQTTv5,
            callback_api_version=CallbackAPIVersion.VERSION2,
        )

        if tls_material and tls_material.cafile:
            client.tls_set(
                ca_certs=tls_material.cafile,
                certfile=tls_material.certfile,
                keyfile=tls_material.keyfile,
            )

        if config.mqtt_user:
            client.username_pw_set(config.mqtt_user, config.mqtt_pass)

        try:
            client.connect(config.mqtt_host, config.mqtt_port, keepalive=10)
            client.loop_start()

            info = client.publish(topic, payload, qos=1)

            start_time = time.time()
            while not info.is_published():
                if time.time() - start_time > publish_timeout:
                    raise TimeoutError("Publish timed out")
                sleep_fn(poll_interval)

            client.loop_stop()
            client.disconnect()
            return

        except Exception as exc:
            last_error = exc
            logger.warning(
                "Publish attempt %d/%d failed: %s",
                attempt, retries, exc
            )
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

            if attempt < retries:
                sleep_fn(base_delay * (2 ** (attempt - 1)))

    raise RuntimeError(f"Failed to publish after {retries} attempts") from last_error


def get_pin_from_path() -> str | None:
    path = os.environ.get("PATH_INFO", "")
    match = re.match(r"/pin/(\d+)", path)
    return match.group(1) if match else None


def send_response(status_code: int, data: dict[str, Any]) -> None:
    sys.stdout.write(f"Status: {status_code}\n")
    sys.stdout.write("Content-Type: application/json\n\n")
    sys.stdout.write(json.dumps(data))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> None:
    try:
        config = load_runtime_config()
        configure_logging(config)
    except Exception as exc:  # pragma: no cover - configuration failures
        _configure_fallback_logging()
        logger.exception("Failed to load runtime configuration")
        send_response(
            500,
            {
                "status": "error",
                "message": f"Configuration error: {exc}",
            },
        )
        return

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
        content_length_raw = os.environ.get("CONTENT_LENGTH", "0")
        try:
            content_length = int(content_length_raw)
        except (TypeError, ValueError):
            content_length = 0

        if content_length > 0:
            body = sys.stdin.read(content_length)
            remainder = sys.stdin.read()
            if remainder:
                body += remainder
        else:
            body = sys.stdin.read()

        data: dict[str, Any] = json.loads(body) if body else {}
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

    topic = f"{config.mqtt_topic or DEFAULT_MQTT_TOPIC}/d/{pin}"
    payload = "1" if state == "ON" else "0"

    try:
        publish_with_retries(topic, payload, config)
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

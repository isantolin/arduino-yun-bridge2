#!/usr/bin/env python3
"""CGI helper that toggles digital pins via MQTT using paho-mqtt.

Refactored for OpenWrt 25.12 / Python 3.13:
- Replaced deprecated 'cgi' module with standard 'os.environ' parsing.
- Replaced manual retry loops with 'tenacity' library.
- Strict typing and logging.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import msgspec
from typing import Any

from paho.mqtt.client import Client, MQTTv5
from paho.mqtt.enums import CallbackAPIVersion
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import RuntimeConfig, load_runtime_config
from mcubridge.config.common import get_uci_config
from mcubridge.protocol import protocol


logger = logging.getLogger("mcubridge.pin_rest")


def _configure_fallback_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _safe_int(value: object, default: int) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return default


def _safe_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return default


# Load defaults from UCI once at module level
_UCI = get_uci_config()
DEFAULT_RETRIES = max(1, _safe_int(_UCI.get("pin_mqtt_retries"), 3))
DEFAULT_PUBLISH_TIMEOUT = max(0.0, _safe_float(_UCI.get("pin_mqtt_timeout"), 4.0))
DEFAULT_BACKOFF_BASE = max(0.0, _safe_float(_UCI.get("pin_mqtt_backoff"), 0.5))


def _configure_tls(client: Any, config: RuntimeConfig) -> None:
    if not getattr(config, "mqtt_tls", False):
        return

    cafile = getattr(config, "mqtt_cafile", None) or None
    certfile = getattr(config, "mqtt_certfile", None) or None
    keyfile = getattr(config, "mqtt_keyfile", None) or None
    tls_insecure = bool(getattr(config, "mqtt_tls_insecure", False))

    if (certfile and not keyfile) or (keyfile and not certfile):
        raise ValueError("TLS client auth requires both mqtt_certfile and mqtt_keyfile")

    if cafile and not os.path.exists(cafile):
        raise ValueError(f"TLS CA file does not exist: {cafile}")

    tls_kwargs: dict[str, Any] = {}
    if cafile:
        tls_kwargs["ca_certs"] = cafile
    if certfile:
        tls_kwargs["certfile"] = certfile
    if keyfile:
        tls_kwargs["keyfile"] = keyfile

    client.tls_set(**tls_kwargs)

    if tls_insecure and hasattr(client, "tls_insecure_set"):
        client.tls_insecure_set(True)


@retry(
    stop=stop_after_attempt(DEFAULT_RETRIES),
    wait=wait_exponential(multiplier=DEFAULT_BACKOFF_BASE, min=DEFAULT_BACKOFF_BASE, max=4.0),
    retry=retry_if_exception_type((OSError, ConnectionError, TimeoutError)),
    reraise=True,
)
def publish_safe(topic: str, payload: str, config: RuntimeConfig) -> None:
    """Publish an MQTT message with retry semantics managed by Tenacity.
    
    Creates a fresh connection per attempt to ensure recovery from broken
    socket states.
    """
    client = Client(
        client_id=f"mcubridge_cgi_{time.time()}",
        protocol=MQTTv5,
        callback_api_version=CallbackAPIVersion.VERSION2,
    )
    try:
        _configure_tls(client, config)
        if config.mqtt_user:
            client.username_pw_set(config.mqtt_user, config.mqtt_pass)

        client.connect(config.mqtt_host, config.mqtt_port, keepalive=10)
        client.loop_start()

        info = client.publish(topic, payload, qos=1)

        start_time = time.time()
        while not info.is_published():
            if time.time() - start_time > DEFAULT_PUBLISH_TIMEOUT:
                raise TimeoutError("Publish timed out")
            time.sleep(0.05)
            
    except Exception as exc:
        logger.warning("Publish attempt failed: %s", exc)
        raise  # Trigger Tenacity retry
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass


def get_pin_from_path() -> str | None:
    path = os.environ.get("PATH_INFO", "")
    match = re.match(r"/pin/(\d+)", path)
    return match.group(1) if match else None


def send_response(status_code: int, data: dict[str, Any]) -> None:
    sys.stdout.write(f"Status: {status_code}\n")
    sys.stdout.write("Content-Type: application/json\n\n")
    sys.stdout.write(msgspec.json.encode(data).decode("utf-8"))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> None:
    try:
        config = load_runtime_config()
        configure_logging(config)
    except Exception as exc:
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
                "message": ("Only POST is supported. Subscribe via MQTT for state."),
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
            # Handle potential buffering issues in some CGI environments
            if len(body) < content_length:
                body += sys.stdin.read()
        else:
            body = sys.stdin.read()

        data: dict[str, Any] = msgspec.json.decode(body) if body else {}
        state = str(data.get("state", "")).upper()
    except (ValueError, msgspec.DecodeError):
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

    topic = f"{config.mqtt_topic or protocol.MQTT_DEFAULT_TOPIC_PREFIX}/d/{pin}"
    payload = "1" if state == "ON" else "0"

    try:
        publish_safe(topic, payload, config)
        send_response(
            200,
            {
                "status": "ok",
                "pin": int(pin),
                "state": state,
                "message": f"Command to turn pin {pin} {state} sent via MQTT.",
            },
        )
    except Exception as exc:
        # Catch-all for final failure after retries
        logger.error("MQTT publish operation failed for pin %s after retries: %s", pin, exc)
        send_response(
            500,
            {
                "status": "error",
                "message": f"Failed to send command for pin {pin}: {exc}",
            },
        )


if __name__ == "__main__":
    main()

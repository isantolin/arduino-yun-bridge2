#!/usr/bin/env python3
"""CGI helper that toggles digital pins via MQTT using paho-mqtt."""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
import sys
import time
from types import ModuleType, SimpleNamespace
from typing import Any, Callable, Dict

try:  # pragma: no cover - replaced with test doubles when needed
    from paho.mqtt import client as mqtt_client
except (ImportError, Exception) as exc:  # pragma: no cover
    _missing_reason = repr(exc)

    class _MissingClient:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise RuntimeError(
                "paho-mqtt dependencies are unavailable; "
                "install the package to use pin_rest_cgi"
                f" ({_missing_reason})."
            )

    mqtt: ModuleType | SimpleNamespace = SimpleNamespace(
        Client=_MissingClient
    )
else:
    mqtt = mqtt_client

from tenacity import (
    Retrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from yunbridge.config.settings import RuntimeConfig, load_runtime_config
from yunbridge.config.tls import resolve_tls_material
from yunbridge.const import DEFAULT_MQTT_TOPIC


logger = logging.getLogger("yunbridge.pin_rest")
if not logger.hasHandlers():
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


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


def _resolve_tls_material(config: RuntimeConfig):
    if not config.mqtt_tls:
        raise RuntimeError(
            "MQTT TLS must remain enabled for pin_rest_cgi operation"
        )

    env_cert = os.environ.get("YUNBRIDGE_PIN_CERTFILE") or None
    env_key = os.environ.get("YUNBRIDGE_PIN_KEYFILE") or None
    env_cafile = os.environ.get("YUNBRIDGE_PIN_CAFILE") or None

    return resolve_tls_material(
        config,
        cafile_override=env_cafile,
        cert_override=env_cert,
        key_override=env_key,
    )


def _build_client(config: RuntimeConfig) -> Any:
    client = mqtt.Client(protocol=getattr(mqtt, "MQTTv5", 5))
    if config.mqtt_user:
        client.username_pw_set(config.mqtt_user, config.mqtt_pass)

    material = _resolve_tls_material(config)
    client.tls_set(
        ca_certs=material.cafile,
        certfile=material.certfile,
        keyfile=material.keyfile,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )
    client.tls_insecure_set(False)
    client.enable_logger(logger)
    return client


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

    if retries <= 0:
        raise ValueError("retries must be a positive integer")
    if poll_interval <= 0:
        poll_interval = DEFAULT_POLL_INTERVAL

    def _log_retry(state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome else None
        logger.warning(
            "MQTT publish attempt %d failed: %s",
            state.attempt_number,
            exc,
        )

    def _attempt() -> None:
        client = _build_client(config)

        try:
            client.connect(config.mqtt_host, config.mqtt_port, keepalive=60)
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

    adjusted_base = max(base_delay, 0.0)
    wait_kwargs: Dict[str, float] = {
        "multiplier": adjusted_base or 0.0,
        "min": adjusted_base or 0.0,
    }
    if adjusted_base > 0:
        wait_kwargs["max"] = adjusted_base * 8

    wait = wait_exponential(**wait_kwargs)

    retryer = Retrying(
        reraise=True,
        stop=stop_after_attempt(retries),
        wait=wait,
        retry=retry_if_exception_type(Exception),
        before_sleep=_log_retry,
        sleep=sleep_fn,
    )

    retryer(_attempt)


def get_pin_from_path() -> str | None:
    path = os.environ.get("PATH_INFO", "")
    match = re.match(r"/pin/(\d+)", path)
    return match.group(1) if match else None


def send_response(status_code: int, data: Dict[str, Any]) -> None:
    print(f"Status: {status_code}")
    print("Content-Type: application/json\n")
    print(json.dumps(data))


def main() -> None:
    try:
        config = load_runtime_config()
    except Exception as exc:  # pragma: no cover - configuration failures
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

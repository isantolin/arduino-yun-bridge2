#!/usr/bin/env python3
"""
Ayudante CGI que conmuta pines digitales vía MQTT usando paho-mqtt.

Refactorizado para OpenWrt 25.12 / Python 3.13:
- Reemplaza el módulo obsoleto 'cgi' por parsing estándar de 'os.environ'.
- Reemplaza bucles de reintento manuales con la librería 'tenacity'.
- Adopta el estándar WSGI vía 'wsgiref' para eliminar la gestión manual de HTTP.
- Tipado estricto y logging centralizado.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, List
from wsgiref.handlers import CGIHandler

import msgspec
from mcubridge.config.common import get_uci_config
from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol.topics import pin_topic
from paho.mqtt.client import Client, MQTTv5
from paho.mqtt.enums import CallbackAPIVersion
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("mcubridge.pin_rest")


def _configure_fallback_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

_UCI = get_uci_config()

# [SIL-2] Explicit boundary validation for UCI values
DEFAULT_PUBLISH_TIMEOUT = 4.0

try:
    retries = max(1, int(_UCI.get("pin_mqtt_retries", 3)))
    publish_timeout = max(0.0, float(_UCI.get("pin_mqtt_timeout", 4.0)))
    backoff_base = max(0.0, float(_UCI.get("pin_mqtt_backoff", 0.5)))
except (ValueError, TypeError):
    retries = 3
    publish_timeout = 4.0
    backoff_base = 0.5


@retry(
    stop=stop_after_attempt(retries),
    wait=wait_exponential(multiplier=backoff_base, min=backoff_base, max=4.0),
    retry=retry_if_exception_type((OSError, ConnectionError, TimeoutError)),
    reraise=True,
)
def publish_safe(topic: str, payload: str, config: Any) -> None:
    client = Client(
        client_id=f"mcubridge_cgi_{time.time()}",
        protocol=MQTTv5,
        callback_api_version=CallbackAPIVersion.VERSION2,
    )
    try:
        if config.tls_enabled:
            # Re-use the shared TLS context builder
            from mcubridge.util.mqtt_helper import configure_tls_context
            ctx = configure_tls_context(config)
            client.tls_set_context(ctx)  # type: ignore[reportUnknownMemberType]
            if config.mqtt_tls_insecure:
                client.tls_insecure_set(True)

        if config.mqtt_user:
            client.username_pw_set(config.mqtt_user, config.mqtt_pass)

        client.connect(config.mqtt_host, config.mqtt_port, keepalive=10)
        client.loop_start()

        info = client.publish(topic, payload, qos=1)

        start_time = time.time()
        while not info.is_published():
            if time.time() - start_time > publish_timeout:
                raise TimeoutError("Publish timed out")
            time.sleep(0.05)

    except Exception as exc:
        logger.warning("Publish attempt failed: %s", exc)
        raise
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass



def get_pin_from_path(environ: dict[str, Any]) -> str | None:
    path = environ.get("PATH_INFO", "")
    match = re.match(r"/pin/(\d+)", path)
    return match.group(1) if match else None



def json_response(start_response: Any, status: str, data: dict[str, Any]) -> List[bytes]:
    response_body = msgspec.json.encode(data)
    headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(response_body))),
    ]
    start_response(status, headers)
    return [response_body]



def application(environ: dict[str, Any], start_response: Any) -> List[bytes]:
    try:
        config = load_runtime_config()
        configure_logging(config)
    except Exception as exc:
        _configure_fallback_logging()
        logger.exception("Failed to load runtime configuration")
        return json_response(start_response, "500 Internal Server Error", {
            "status": "error",
            "message": f"Configuration error: {exc}",
        })

    method = environ.get("REQUEST_METHOD", "GET").upper()
    pin = get_pin_from_path(environ)
    if not pin or not pin.isdigit():
        return json_response(start_response, "400 Bad Request", {
            "status": "error", "message": "Pin must be specified in the URL as /pin/<N>.",
        })

    if method != "POST":
        return json_response(start_response, "405 Method Not Allowed", {
            "status": "error", "message": "Only POST is supported.",
        })

    try:
        content_length = int(environ.get("CONTENT_LENGTH", "0"))
        body = b""
        if content_length > 0:
            stream = environ.get("wsgi.input")
            if stream:
                body = stream.read(content_length)
        data: dict[str, Any] = msgspec.json.decode(body) if body else {}
        state = str(data.get("state", "")).upper()
    except (ValueError, msgspec.DecodeError):
        return json_response(start_response, "400 Bad Request", {"status": "error", "message": "Invalid JSON body."})

    if state not in ("ON", "OFF"):
        return json_response(
            start_response,
            "400 Bad Request",
            {"status": "error", "message": "Invalid state"}
        )

    topic = pin_topic(config.mqtt_topic, pin, "")
    payload = "1" if state == "ON" else "0"

    try:
        publish_safe(topic, payload, config)
        return json_response(start_response, "200 OK", {
            "status": "ok", "pin": int(pin), "state": state,
            "message": f"Command to turn pin {pin} {state} sent via MQTT.",
        })
    except Exception as exc:
        return json_response(start_response, "500 Internal Server Error", {
            "status": "error", "message": f"Failed to send command: {exc}",
        })

if __name__ == "__main__":
    CGIHandler().run(application)

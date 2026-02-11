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
import os
import re
import time
import msgspec
from wsgiref.handlers import CGIHandler
from typing import Any, List

from paho.mqtt.client import Client, MQTTv5
from paho.mqtt.enums import CallbackAPIVersion
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import RuntimeConfig, load_runtime_config
from mcubridge.config.common import get_uci_config
from mcubridge.protocol import protocol


logger = logging.getLogger("mcubridge.pin_rest")


def _configure_fallback_logging() -> None:
    """Configura un logging básico si falla la carga de configuración."""
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


# Cargar valores por defecto desde UCI una vez a nivel de módulo
_UCI = get_uci_config()
DEFAULT_RETRIES = max(1, _safe_int(_UCI.get("pin_mqtt_retries"), 3))
DEFAULT_PUBLISH_TIMEOUT = max(0.0, _safe_float(_UCI.get("pin_mqtt_timeout"), 4.0))
DEFAULT_BACKOFF_BASE = max(0.0, _safe_float(_UCI.get("pin_mqtt_backoff"), 0.5))


def _configure_tls(client: Any, config: RuntimeConfig) -> None:
    """Aplica configuración TLS al cliente MQTT si está habilitada."""
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
    """Publica un mensaje MQTT con semántica de reintento gestionada por Tenacity.

    Crea una conexión nueva por intento para asegurar recuperación de estados
    de socket rotos.
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
        raise
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass


def get_pin_from_path(environ: dict[str, Any]) -> str | None:
    """Extrae el número de pin del PATH_INFO (ej. /pin/13)."""
    path = environ.get("PATH_INFO", "")
    match = re.match(r"/pin/(\d+)", path)
    return match.group(1) if match else None


def json_response(start_response: Any, status: str, data: dict[str, Any]) -> List[bytes]:
    """Helper para enviar una respuesta JSON vía WSGI."""
    response_body = msgspec.json.encode(data)
    headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(response_body))),
    ]
    start_response(status, headers)
    return [response_body]


def application(environ: dict[str, Any], start_response: Any) -> List[bytes]:
    """Punto de Entrada de la Aplicación WSGI Estándar."""
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
    logger.info("REST call: method=%s pin=%s", method, pin)

    if not pin or not pin.isdigit():
        return json_response(start_response, "400 Bad Request", {
            "status": "error",
            "message": "Pin must be specified in the URL as /pin/<N>.",
        })

    if method != "POST":
        return json_response(start_response, "405 Method Not Allowed", {
            "status": "error",
            "message": "Only POST is supported. Subscribe via MQTT for state.",
        })

    try:
        content_length_raw = environ.get("CONTENT_LENGTH", "0")
        try:
            content_length = int(content_length_raw)
        except (TypeError, ValueError):
            content_length = 0

        body = b""
        if content_length > 0:
            stream = environ.get("wsgi.input")
            if stream:
                body = stream.read(content_length)

        data: dict[str, Any] = msgspec.json.decode(body) if body else {}
        state = str(data.get("state", "")).upper()
    except (ValueError, msgspec.DecodeError):
        logger.exception("POST body parse error")
        return json_response(start_response, "400 Bad Request", {
            "status": "error", "message": "Invalid JSON body."
        })

    if state not in ("ON", "OFF"):
        return json_response(start_response, "400 Bad Request", {
            "status": "error",
            "message": 'State must be "ON" or "OFF".',
        })

    topic = f"{config.mqtt_topic or protocol.MQTT_DEFAULT_TOPIC_PREFIX}/d/{pin}"
    payload = "1" if state == "ON" else "0"

    try:
        publish_safe(topic, payload, config)
        return json_response(start_response, "200 OK", {
            "status": "ok",
            "pin": int(pin),
            "state": state,
            "message": f"Command to turn pin {pin} {state} sent via MQTT.",
        })
    except Exception as exc:
        logger.error("MQTT publish operation failed for pin %s after retries: %s", pin, exc)
        return json_response(start_response, "500 Internal Server Error", {
            "status": "error",
            "message": f"Failed to send command for pin {pin}: {exc}",
        })


if __name__ == "__main__":
    # WSGI Ref CGI Handler hace este script compatible con servidores CGI estándar
    # como uhttpd en OpenWrt, manteniendo la lógica limpia como una app WSGI.
    CGIHandler().run(application)

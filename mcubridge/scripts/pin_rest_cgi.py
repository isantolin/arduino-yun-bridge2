#!/usr/bin/env python3
"""Modernized Pin REST CGI helper for MCU Bridge (SIL-2)."""

from __future__ import annotations

import logging
import re
from typing import Any
from wsgiref.handlers import CGIHandler

import msgspec
import paho.mqtt.publish as publish
import typer
from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol.structures import GenericResponsePacket, RuntimeConfig
from mcubridge.protocol.topics import Topic, topic_path

logger = logging.getLogger("mcubridge.pin_rest")

app = typer.Typer(add_completion=False)


def publish_sync(topic: str, payload: str, config: RuntimeConfig) -> None:
    """Synchronous MQTT publish for CGI context using direct library call."""
    tls_config: Any = None
    if tls_ctx := config.get_ssl_context():
        # paho.mqtt.publish.single takes a dict for tls or an SSLContext
        tls_config = {"context": tls_ctx}

    auth: Any = None
    if config.mqtt_user:
        auth = {"username": config.mqtt_user, "password": config.mqtt_pass}

    publish.single(
        topic,
        payload=payload,
        qos=1,
        hostname=config.mqtt_host,
        port=config.mqtt_port,
        auth=auth,
        tls=tls_config,
    )


def json_res(
    start_response: Any, status: str, response: GenericResponsePacket
) -> list[bytes]:
    body = msgspec.json.encode(response)
    headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ]
    start_response(status, headers)
    return [body]


def application(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    """WSGI application for pin control."""
    try:
        config = load_runtime_config()
        configure_logging(config)

        path = environ.get("PATH_INFO", "")
        if not (match := re.match(r"/pin/(\d+)", path)):
            return json_res(
                start_response,
                "400 Bad Request",
                GenericResponsePacket(status="error", message="Invalid path"),
            )

        pin = match.group(1)
        if environ.get("REQUEST_METHOD") != "POST":
            return json_res(
                start_response,
                "405 Method Not Allowed",
                GenericResponsePacket(status="error", message="Method not allowed"),
            )

        body_len = int(environ.get("CONTENT_LENGTH", "0"))
        body_data = environ["wsgi.input"].read(body_len)
        data: dict[str, Any] = msgspec.json.decode(body_data) if body_len else {}
        state = str(data.get("state", "")).upper()

        if state not in ("ON", "OFF"):
            return json_res(
                start_response,
                "400 Bad Request",
                GenericResponsePacket(status="error", message="Invalid state"),
            )

        topic = topic_path(config.mqtt_topic, Topic.DIGITAL, pin)
        publish_sync(topic, "1" if state == "ON" else "0", config)

        return json_res(
            start_response,
            "200 OK",
            GenericResponsePacket(status="ok", data={"pin": int(pin), "state": state}),
        )

    except (ValueError, KeyError, TypeError, OSError) as e:
        logger.exception("CGI Error")
        return json_res(
            start_response,
            "500 Internal Server Error",
            GenericResponsePacket(status="error", message=str(e)),
        )


@app.command()
def run_cgi() -> None:
    """Entry point for CGI execution."""
    CGIHandler().run(application)


if __name__ == "__main__":
    # If called without arguments, assume CGI environment
    import sys

    if len(sys.argv) == 1:
        run_cgi()
    else:
        app()

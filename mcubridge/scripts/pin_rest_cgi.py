#!/usr/bin/env python3
"""Modernized Pin REST CGI helper for MCU Bridge (SIL-2)."""

from __future__ import annotations

import logging
import re
from typing import Any
from wsgiref.handlers import CGIHandler

import socket
from google.protobuf import json_format
from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.structures import RuntimeConfig
from mcubridge.protocol.topics import Topic, topic_path

logger = logging.getLogger("mcubridge.pin_rest")


def publish_sync(topic: str, payload: str, config: RuntimeConfig) -> None:
    """Synchronous publish to local UNIX domain socket IPC."""
    msg = pb.CloudQueuedPublish(
        topic_name=topic,
        payload=payload.encode("utf-8"),
        qos=1,
    )
    data = msg.SerializeToString()
    prefix = len(data).to_bytes(4, byteorder="big")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect("/var/run/mcubridge.sock")
        sock.sendall(prefix + data)
    except OSError as exc:
        logger.error("Failed to connect to UNIX socket: %s", exc)
        raise
    finally:
        sock.close()


def json_res(start_response: Any, status: str, response: pb.PinControlResponse) -> list[bytes]:
    """Serialize PinControlResponse Protobuf message to JSON for CGI output."""
    body = json_format.MessageToJson(response, preserving_proto_field_name=True).encode("utf-8")
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
                pb.PinControlResponse(status="error", message="Invalid path"),
            )

        pin = int(match.group(1))
        if environ.get("REQUEST_METHOD") != "POST":
            return json_res(
                start_response,
                "405 Method Not Allowed",
                pb.PinControlResponse(status="error", message="Method not allowed"),
            )

        body_len = int(environ.get("CONTENT_LENGTH", "0"))
        body_data = environ["wsgi.input"].read(body_len)

        # [SIL-2] Parse request using Protobuf model via JSON mapping
        req = pb.PinControlRequest()
        if body_len:
            json_format.Parse(body_data, req)

        state = str(req.state).upper()

        if state not in ("ON", "OFF"):
            return json_res(
                start_response,
                "400 Bad Request",
                pb.PinControlResponse(status="error", message="Invalid state"),
            )

        topic = topic_path(config.topic_prefix, Topic.DIGITAL, str(pin))
        publish_sync(topic, "1" if state == "ON" else "0", config)

        return json_res(
            start_response,
            "200 OK",
            pb.PinControlResponse(
                status="ok",
                data=pb.PinControlData(pin=pin, state=state),
            ),
        )

    except (ValueError, KeyError, TypeError, OSError, json_format.ParseError) as e:
        logger.exception("CGI Error")
        return json_res(
            start_response,
            "500 Internal Server Error",
            pb.PinControlResponse(status="error", message=str(e)),
        )


def run_cgi() -> None:
    """Entry point for CGI execution."""
    CGIHandler().run(application)


if __name__ == "__main__":
    run_cgi()

#!/usr/bin/env python3
"""Modernized Pin REST CGI and Typer CLI helper for MCU Bridge (SIL-2)."""

from __future__ import annotations

import logging
import os
import re
from typing import Annotated, Any, cast
from wsgiref.handlers import CGIHandler

import asyncio
import typer
from grpclib.client import Channel
from mcubridge.protocol.mcubridge_grpc import LocalBridgeStub
import protovalidate
from google.protobuf import json_format
from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.structures import RuntimeConfig
from mcubridge.protocol.topics import Topic, topic_path

logger = logging.getLogger("mcubridge.pin_rest")

app = typer.Typer(help="Pin REST CGI and CLI interface for MCU Bridge.", add_completion=False)


def publish_sync(topic: str, payload: str, config: RuntimeConfig) -> None:
    """Synchronous publish to local UNIX domain socket IPC via gRPC."""

    async def _run():
        channel = Channel(path="/var/run/mcubridge.sock")
        stub = LocalBridgeStub(channel)
        try:
            msg = pb.CloudQueuedPublish(
                topic_name=topic,
                payload=payload.encode("utf-8"),
                qos=1,
            )
            await stub.Publish(msg)
        finally:
            channel.close()

    try:
        asyncio.run(_run())
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("Failed to publish via local gRPC IPC: %s", exc)
        raise


def json_res(start_response: Any, status: str, response: pb.PinControlResponse) -> list[bytes]:
    """Serialize PinControlResponse Protobuf message to JSON for CGI output."""
    body = json_format.MessageToJson(cast(Any, response), preserving_proto_field_name=True).encode("utf-8")
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

        # [SIL-2] Parse request and validate using Protobuf model via protovalidate
        req = pb.PinControlRequest()
        if body_len:
            json_format.Parse(body_data, cast(Any, req))

        try:
            protovalidate.validate(cast(Any, req))
        except (protovalidate.ValidationError, ValueError, TypeError) as val_err:
            return json_res(
                start_response,
                "400 Bad Request",
                pb.PinControlResponse(status="error", message=f"Invalid state: {val_err}"),
            )

        state = req.state.upper()
        pin_data = pb.PinControlData(pin=pin, state=state)
        try:
            protovalidate.validate(cast(Any, pin_data))
        except (protovalidate.ValidationError, ValueError, TypeError) as val_err:
            return json_res(
                start_response,
                "400 Bad Request",
                pb.PinControlResponse(status="error", message=f"Invalid pin data: {val_err}"),
            )

        topic = topic_path(config.topic_prefix, Topic.DIGITAL, str(pin_data.pin))
        publish_sync(topic, "1" if pin_data.state == "ON" else "0", config)

        return json_res(
            start_response,
            "200 OK",
            pb.PinControlResponse(
                status="ok",
                data=pin_data,
            ),
        )

    except (ValueError, KeyError, TypeError, OSError, json_format.ParseError) as e:
        logger.exception("CGI Error")
        return json_res(
            start_response,
            "500 Internal Server Error",
            pb.PinControlResponse(status="error", message=str(e)),
        )


@app.command()
def control(
    pin: Annotated[int, typer.Option("--pin", "-p", help="Pin number to set")] = 13,
    state: Annotated[str, typer.Option("--state", "-s", help="State (ON/OFF)")] = "ON",
) -> None:
    """CLI entry point for direct pin control."""
    config = load_runtime_config()
    configure_logging(config)
    pin_data = pb.PinControlData(pin=pin, state=state.upper())
    protovalidate.validate(cast(Any, pin_data))
    topic = topic_path(config.topic_prefix, Topic.DIGITAL, str(pin_data.pin))
    publish_sync(topic, "1" if pin_data.state == "ON" else "0", config)
    print(f"Pin {pin} set to {state.upper()} on topic {topic}")


@app.command()
def cgi() -> None:
    """Execute as CGI WSGI script."""
    CGIHandler().run(application)


def run_cgi() -> None:
    """Entry point for CGI execution."""
    if "GATEWAY_INTERFACE" in os.environ or "REQUEST_METHOD" in os.environ:
        CGIHandler().run(application)
    else:
        app()


if __name__ == "__main__":
    run_cgi()

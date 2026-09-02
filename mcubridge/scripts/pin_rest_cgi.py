#!/usr/bin/env python3
"""Modernized Pin REST CGI and Typer CLI helper for MCU Bridge (SIL-2)."""

from __future__ import annotations

import os
import re
from typing import Annotated, Any, cast
from wsgiref.handlers import CGIHandler

import asyncio
import structlog
import typer
from grpclib.client import Channel
from mcubridge.protocol.mcubridge_grpc import LocalBridgeStub
from google.protobuf import json_format
from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb

import importlib

try:
    ubus: Any = importlib.import_module("ubus")
except ImportError:
    ubus = None

logger = structlog.get_logger("mcubridge.pin_rest")

app = typer.Typer(help="Pin REST CGI and CLI interface for MCU Bridge.", add_completion=False)


def set_pin_digital_sync(pin: int, value: int) -> None:
    """Synchronous digital write via native OpenWrt UBUS (with local gRPC IPC fallback)."""
    if ubus is not None:
        try:
            conn: Any = ubus.connect()
            conn.call("mcubridge", "digital_write", {"pin": pin, "value": value})
            return
        except (OSError, RuntimeError) as exc:
            logger.debug("UBUS call failed; falling back to local gRPC socket", error=str(exc))

    async def _run():
        async with Channel(path="/var/run/mcubridge.sock") as channel:
            stub = LocalBridgeStub(channel)
            msg = pb.DigitalWrite(pin=pin, value=value)
            await stub.DigitalWrite(msg)

    try:
        asyncio.run(_run())
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("Failed to write digital pin via local IPC", error=str(exc))
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

        path = environ.get("PATH_INFO") or environ.get("REQUEST_URI") or ""
        match = re.search(r"/(?:pin|digital)/(\d+)", path)
        if not match:
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

        # [SIL-2] Parse request and validate using Protobuf model natively
        req = pb.PinControlRequest()
        if body_len:
            json_format.Parse(body_data, cast(Any, req))

        if not (0 <= pin <= 100):
            return json_res(
                start_response,
                "400 Bad Request",
                pb.PinControlResponse(status="error", message=f"Invalid pin: {pin}"),
            )

        state = req.state.upper()
        if state and state not in ("ON", "OFF", "HIGH", "LOW", "1", "0"):
            return json_res(
                start_response,
                "400 Bad Request",
                pb.PinControlResponse(status="error", message=f"Invalid state: {req.state}"),
            )

        normalized_state = "ON" if state in ("ON", "HIGH", "1") else "OFF"
        pin_data = pb.PinControlData(pin=pin, state=normalized_state)

        set_pin_digital_sync(pin_data.pin, 1 if pin_data.state == "ON" else 0)

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
    normalized_state = state.upper()
    if normalized_state not in ("ON", "OFF", "HIGH", "LOW", "1", "0"):
        raise ValueError(f"Invalid state: {state}")
    pin_data = pb.PinControlData(pin=pin, state="ON" if normalized_state in ("ON", "HIGH", "1") else "OFF")
    set_pin_digital_sync(pin_data.pin, 1 if pin_data.state == "ON" else 0)
    print(f"Pin {pin} set to {pin_data.state} via gRPC DigitalWrite")


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

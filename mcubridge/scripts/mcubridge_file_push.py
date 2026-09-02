#!/usr/bin/env python3
"""Modernized File Push utility for MCU Bridge (SIL-2)."""

from __future__ import annotations
import asyncio
import importlib
import sys
from pathlib import Path
from typing import Annotated, Any

import structlog
import typer
from grpclib.client import Channel

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.mcubridge_grpc import LocalBridgeStub

# [SIL-2] Structured logging towards syslog/stderr
logger = structlog.get_logger("mcubridge.file-push")
app = typer.Typer(help="Push files to MCU or Linux storage.", add_completion=False)


def push_file_ubus(target_path: str, data: bytes) -> bool:
    """Attempt fast-path file write via OpenWrt UBUS."""
    try:
        ubus_mod = importlib.import_module("ubus")
        conn = ubus_mod.connect()
        if not conn:
            return False
        try:
            data_str = data.decode("utf-8")
        except UnicodeDecodeError:
            data_str = data.hex()
        res: Any = conn.call("mcubridge", "file_write", {"path": target_path, "data": data_str})
        if isinstance(res, dict) and res.get("status") == "ok":
            logger.info("File push successful via UBUS", path=target_path, size=len(data))
            return True
        return False
    except (ImportError, OSError, RuntimeError, AttributeError):
        return False


def push_file(target_path: str, data: bytes) -> None:
    """Write file data using UBUS fast-path with local gRPC UNIX socket fallback."""
    if push_file_ubus(target_path, data):
        return

    async def _run() -> None:
        try:
            async with Channel(path="/var/run/mcubridge.sock") as channel:
                stub = LocalBridgeStub(channel)
                msg = pb.FileWrite(
                    path=target_path,
                    data=data,
                )
                await stub.FileWrite(msg)
                logger.info("File push successful via gRPC", path=target_path, size=len(data))
        except (OSError, RuntimeError, ValueError) as e:
            logger.error("File push failed", error=str(e), path=target_path)
            sys.exit(1)

    asyncio.run(_run())


@app.command()
def main(
    source: Annotated[Path, typer.Argument(help="Source file to push")],
    target: Annotated[str, typer.Argument(help="Target path on the bridge")],
    mcu: Annotated[bool, typer.Option(help="Target MCU storage")] = False,
) -> None:
    """Push file data to the bridge via UBUS or local gRPC IPC."""
    if not source.exists() or source.is_dir():
        logger.error("Source file does not exist", source=str(source))
        sys.exit(2)

    clean_target = target.lstrip("/")
    target_path = f"mcu/{clean_target}" if mcu else clean_target

    data = source.read_bytes()

    # [SIL-2] Binary payloads must be logged in HEXADECIMAL
    hexdump = data[:64].hex(" ").upper()
    if len(data) > 64:
        hexdump += "..."

    logger.info(
        "Pushing file",
        target_path=target_path,
        size=len(data),
        payload_hex=hexdump,
    )

    push_file(target_path, data)


if __name__ == "__main__":
    app()

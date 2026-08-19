#!/usr/bin/env python3
"""Modernized File Push utility for MCU Bridge (SIL-2)."""

from __future__ import annotations
from typing import Annotated

import asyncio
import sys
from pathlib import Path
import typer
from grpclib.client import Channel
from mcubridge.protocol.mcubridge_grpc import LocalBridgeStub
import structlog
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.topics import Topic, topic_path

# [SIL-2] Structured logging towards syslog/stderr
logger = structlog.get_logger("mcubridge.file-push")
app = typer.Typer(help="Push files to MCU or Linux storage.", add_completion=False)


def push_file(topic: str, data: bytes) -> None:
    """Publish file data using local gRPC UNIX socket IPC."""

    async def _run():
        try:
            async with Channel(path="/var/run/mcubridge.sock") as channel:
                stub = LocalBridgeStub(channel)
                msg = pb.CloudQueuedPublish(
                    topic_name=topic,
                    payload=data,
                    qos=1,
                )
                await stub.Publish(msg)
                logger.info("File push successful", topic=topic, size=len(data))
        except (OSError, RuntimeError, ValueError) as e:
            logger.error("File push failed", error=str(e), topic=topic)
            sys.exit(1)

    asyncio.run(_run())


@app.command()
def main(
    source: Annotated[Path, typer.Argument(help="Source file to push")],
    target: Annotated[str, typer.Argument(help="Target path on the bridge")],
    mcu: Annotated[bool, typer.Option(help="Target MCU storage")] = False,
) -> None:
    """Push file data to the bridge via CLOUD."""
    if not source.exists() or source.is_dir():
        logger.error("Source file does not exist", source=str(source))
        sys.exit(2)

    config = load_runtime_config()
    prefix = config.topic_prefix

    clean_target = target.lstrip("/")

    segments = ["write"]
    if mcu:
        segments.append("mcu")
    segments.append(clean_target)

    topic = topic_path(prefix, Topic.FILE, *segments)

    data = source.read_bytes()

    # [SIL-2] Binary payloads must be logged in HEXADECIMAL
    hexdump = data[:64].hex(" ").upper()
    if len(data) > 64:
        hexdump += "..."

    logger.info(
        "Pushing file",
        topic=topic,
        size=len(data),
        payload_hex=hexdump,
    )

    push_file(topic, data)


if __name__ == "__main__":
    app()

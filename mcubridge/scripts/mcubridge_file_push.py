#!/usr/bin/env python3
"""Modernized File Push utility for MCU Bridge (SIL-2)."""

from __future__ import annotations

import asyncio
import sys
import argparse
from pathlib import Path
from grpclib.client import Channel
from mcubridge.protocol.mcubridge_grpc import LocalBridgeStub
import structlog
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.topics import Topic, topic_path

# [SIL-2] Structured logging towards syslog/stderr
logger = structlog.get_logger("mcubridge.file-push")


def push_file(topic: str, data: bytes) -> None:
    """Publish file data using local gRPC UNIX socket IPC."""

    async def _run():
        channel = None
        try:
            channel = Channel(path="/var/run/mcubridge.sock")
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
        finally:
            if channel is not None:
                channel.close()

    asyncio.run(_run())


def main() -> None:
    """Push file data to the bridge via CLOUD."""
    parser = argparse.ArgumentParser(description="Push files to MCU or Linux storage.")
    parser.add_argument("source", type=Path, help="Source file to push")
    parser.add_argument("target", help="Target path on the bridge")
    parser.add_argument("--mcu", action="store_true", help="Target MCU storage")
    args = parser.parse_args()

    if not args.source.exists() or args.source.is_dir():
        logger.error("Source file does not exist", source=str(args.source))
        sys.exit(2)

    config = load_runtime_config()
    prefix = config.topic_prefix

    clean_target = args.target.lstrip("/")

    segments = ["write"]
    if args.mcu:
        segments.append("mcu")
    segments.append(clean_target)

    topic = topic_path(prefix, Topic.FILE, *segments)

    data = args.source.read_bytes()

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
    main()

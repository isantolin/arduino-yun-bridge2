#!/usr/bin/env python3
"""Unified e2e feature test for mcubridge using direct LocalBridgeStub through Gateway."""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

import structlog
import typer
from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger("all-features-test")


async def run_test(
    led_builtin_pin: int,
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:
    logger.info("--- Starting UNIFIED ALL-FEATURES E2E Test ---")
    async with bridge_session(host=host, port=port, device_id=device_id, topic_prefix=topic_prefix) as (_channel, stub):
        # 1. LED test
        logger.info("Testing LED (Digital Write)...")
        await stub.DigitalWrite(pb.DigitalWrite(pin=led_builtin_pin, value=1))
        await asyncio.sleep(0.5)
        await stub.DigitalWrite(pb.DigitalWrite(pin=led_builtin_pin, value=0))
        logger.info("LED test passed.")

        # 2. Analog Write test
        logger.info("Testing Analog Operations...")
        await stub.AnalogWrite(pb.AnalogWrite(pin=9, value=128))
        logger.info("Analog write completed.")

        # 3. DataStore test
        logger.info("Testing DataStore...")
        test_key = f"e2e_key_{uuid.uuid4().hex[:6]}"
        await stub.DatastorePut(pb.DatastorePut(key=test_key, value=b"hello"))
        ds_val = await stub.DatastoreGet(pb.DatastoreGet(key=test_key))
        assert ds_val.value == b"hello"
        logger.info("DataStore key verified.", key=test_key, value=ds_val.value.decode("utf-8"))

        # 4. Console test
        logger.info("Testing Console Write...")
        topic_cw = Topic.build(Topic.CONSOLE, "write", prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_cw, payload=b"ping", qos=1))
        logger.info("Console ping written.")

        # 5. FileIO test
        logger.info("Testing FileIO...")
        test_file = "test_file_all.txt"
        test_data = b"e2e-data-all"
        await stub.FileWrite(pb.FileWrite(path=test_file, data=test_data))
        file_res = await stub.FileRead(pb.FileRead(path=test_file))
        assert file_res.content == test_data
        await stub.FileRemove(pb.FileRemove(path=test_file))
        logger.info("FileIO verified.")

    logger.info("--- ALL-FEATURES TEST SUCCEEDED ---")


def main(
    led_builtin_pin: int,
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(led_builtin_pin, host, port, device_id, topic_prefix))


cli = typer.Typer(help="Unified e2e feature test using direct LocalBridgeStub through Gateway.", add_completion=False)


@cli.command()
def cli_main(
    led_builtin_pin: Annotated[int, typer.Option("--pin", help="Pin number from the target core's LED_BUILTIN")],
    host: Annotated[str | None, typer.Option("--host", help="Cloud Gateway host")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Cloud Gateway port")] = None,
    device_id: Annotated[str | None, typer.Option("--device-id", help="Target device ID")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(led_builtin_pin, host, port, device_id, topic_prefix)


if __name__ == "__main__":
    cli()

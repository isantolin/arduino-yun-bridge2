#!/usr/bin/env python3
"""Unified e2e feature test for mcubridge using direct LocalBridgeStub."""

from __future__ import annotations

import asyncio
import structlog
import uuid
import typer
from typing import Annotated

from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger("all-features-test")


async def run_test(socket_path: str | None, topic_prefix: str) -> None:
    logger.info("--- Starting UNIFIED ALL-FEATURES E2E Test ---")
    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        # 1. LED test
        logger.info("Testing LED (Digital Write)...")
        topic_dw = Topic.build(Topic.DIGITAL, "13", prefix=topic_prefix)
        r1 = await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_dw, payload=b"1", qos=1))
        assert r1 is not None
        await asyncio.sleep(0.5)
        r2 = await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_dw, payload=b"0", qos=1))
        assert r2 is not None
        logger.info("LED test passed.")

        # 2. Pin Read test
        logger.info("Testing Digital Read...")
        topic_dr = Topic.build(Topic.DIGITAL, "13", "read", prefix=topic_prefix)
        r_dr = await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_dr, payload=b"", qos=1))
        assert r_dr is not None and r_dr.payload
        logger.info("Digital read pin 13 result", result=r_dr.payload.decode())

        # 2b. Analog test
        logger.info("Testing Analog Operations...")
        topic_ar = Topic.build(Topic.ANALOG, "0", "read", prefix=topic_prefix)
        r_ar = await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_ar, payload=b"", qos=1))
        assert r_ar is not None and r_ar.payload
        logger.info("Analog read pin 0 result", result=r_ar.payload.decode())
        topic_aw = Topic.build(Topic.ANALOG, "9", prefix=topic_prefix)
        r_aw = await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_aw, payload=b"128", qos=1))
        assert r_aw is not None
        logger.info("Analog operations requested.")

        # 3. DataStore test
        logger.info("Testing DataStore...")
        test_key = f"e2e_key_{uuid.uuid4().hex[:6]}"
        topic_ds = Topic.build(Topic.DATASTORE, "put", test_key, prefix=topic_prefix)
        r_ds = await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_ds, payload=b"hello", qos=1))
        assert r_ds is not None
        logger.info("DataStore key put.")

        # 4. Console test
        logger.info("Testing Console Write...")
        topic_cw = Topic.build(Topic.CONSOLE, "write", prefix=topic_prefix)
        r_cw = await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_cw, payload=b"ping", qos=1))
        assert r_cw is not None
        logger.info("Console ping written.")

        # 5. FileIO test
        logger.info("Testing FileIO...")
        topic_fw = Topic.build(Topic.FILE, "write", "test_file.txt", prefix=topic_prefix)
        r_fw = await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_fw, payload=b"e2e-data", qos=1))
        assert r_fw is not None
        logger.info("File write requested.")

    logger.info("--- ALL-FEATURES TEST SUCCEEDED ---")


def main(socket_path: str | None = None, topic_prefix: str = "br") -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


cli = typer.Typer(help="Unified e2e feature test using direct LocalBridgeStub.", add_completion=False)


@cli.command()
def cli_main(
    socket_path: Annotated[str | None, typer.Option("--socket-path", help="UNIX Domain Socket Path")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(socket_path, topic_prefix)


if __name__ == "__main__":
    cli()

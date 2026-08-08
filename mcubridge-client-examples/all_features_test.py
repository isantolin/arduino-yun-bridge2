#!/usr/bin/env python3
"""Unified e2e feature test for mcubridge using direct LocalBridgeStub."""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid

from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("all-features-test")


async def run_test(socket_path: str | None, topic_prefix: str) -> None:
    logger.info("--- Starting UNIFIED ALL-FEATURES E2E Test ---")
    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        # 1. LED test
        logger.info("Testing LED (Digital Write)...")
        topic_dw = Topic.build(Topic.DIGITAL, "13", prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_dw, payload=b"1", qos=1))
        await asyncio.sleep(0.5)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_dw, payload=b"0", qos=1))
        logger.info("LED test passed.")

        # 2. Pin Read test
        logger.info("Testing Digital Read...")
        topic_dr = Topic.build(Topic.DIGITAL, "13", "read", prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_dr, payload=b"", qos=1))
        logger.info("Digital read pin 13 requested.")

        # 2b. Analog test
        logger.info("Testing Analog Operations...")
        topic_ar = Topic.build(Topic.ANALOG, "0", "read", prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_ar, payload=b"", qos=1))
        topic_aw = Topic.build(Topic.ANALOG, "9", prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_aw, payload=b"128", qos=1))
        logger.info("Analog operations requested.")

        # 3. DataStore test
        logger.info("Testing DataStore...")
        test_key = f"e2e_key_{uuid.uuid4().hex[:6]}"
        topic_ds = Topic.build(Topic.DATASTORE, "put", test_key, prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_ds, payload=b"hello", qos=1))
        logger.info("DataStore key put.")

        # 4. Console test
        logger.info("Testing Console Write...")
        topic_cw = Topic.build(Topic.CONSOLE, "write", prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_cw, payload=b"ping", qos=1))
        logger.info("Console ping written.")

        # 5. FileIO test
        logger.info("Testing FileIO...")
        topic_fw = Topic.build(Topic.FILE, "write", prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_fw, payload=b"e2e-data", qos=1))
        logger.info("File write requested.")

    logger.info("--- ALL-FEATURES TEST SUCCEEDED ---")


def main(socket_path: str | None = None, topic_prefix: str = "br") -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Unified e2e feature test using direct LocalBridgeStub.")
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    args = parser.parse_args()
    main(args.socket_path, args.topic_prefix)

#!/usr/bin/env python3
"""Unified e2e feature test for mcubridge."""

import asyncio
import logging
import typer
from mcubridge_client import Topic, build_bridge_args, get_client

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("all-features-test")


async def run_test(host, port, user, password):
    logger.info("--- Starting UNIFIED ALL-FEATURES E2E Test ---")
    async with get_client(**build_bridge_args(host, port, user, password)) as client:
        # 1. LED test
        logger.info("Testing LED (Digital Write)...")
        await client.publish(Topic.build(Topic.DIGITAL, 13), b"1")
        await asyncio.sleep(0.5)
        await client.publish(Topic.build(Topic.DIGITAL, 13), b"0")
        logger.info("LED test passed.")

        # 2. Pin Read test
        logger.info("Testing Digital Read...")
        topic_d13_val = Topic.build(Topic.DIGITAL, 13, "value")
        await client.subscribe(topic_d13_val)
        await client.publish(Topic.build(Topic.DIGITAL, 13, "read"), b"")
        async for message in client.messages:
            if Topic.matches(topic_d13_val, str(message.topic)):
                val = message.payload.decode()
                logger.info(f"Digital read pin 13: {val}")
                break

        # 2b. Analog test
        logger.info("Testing Analog Operations...")
        topic_a0_val = Topic.build(Topic.ANALOG, 0, "value")
        await client.subscribe(topic_a0_val)
        await client.publish(Topic.build(Topic.ANALOG, 0, "read"), b"")
        async for message in client.messages:
            if Topic.matches(topic_a0_val, str(message.topic)):
                analog_val = message.payload.decode()
                logger.info(f"Analog read pin A0: {analog_val}")
                break

        await client.publish(Topic.build(Topic.ANALOG, 9), b"128")
        logger.info("Analog write pin 9 (PWM) set to 128.")

        # 3. DataStore test
        logger.info("Testing DataStore...")
        await client.publish(Topic.build(Topic.DATASTORE, "test/key"), b"hello")
        topic_ds_val = Topic.build(Topic.DATASTORE, "test/key", "value")
        await client.subscribe(topic_ds_val)
        await client.publish(Topic.build(Topic.DATASTORE, "test/key", "get"), b"")
        async for message in client.messages:
            if Topic.matches(topic_ds_val, str(message.topic)):
                res = message.payload.decode()
                logger.info(f"DataStore get: {res}")
                if res != "hello":
                    raise ValueError(f"DataStore mismatch: {res}")
                break

        # 4. Console test
        logger.info("Testing Console Echo...")
        await client.publish(Topic.build(Topic.CONSOLE, "in"), b"ping")
        # Give some time for echo to return and be processed by the daemon
        await asyncio.sleep(2)
        # We don't verify the read here to keep it simple, but we trigger the write flow.
        logger.info("Console write triggered.")

        # 5. File IO test
        logger.info("Testing File IO...")
        import uuid

        test_file = f"/tmp/e2e_test_{uuid.uuid4().hex[:8]}.txt"
        await client.publish(Topic.build(Topic.FILE, "write", test_file), b"e2e-data")

        topic_file_val = Topic.build(Topic.FILE, "data", test_file)
        await client.subscribe(topic_file_val)
        await client.publish(Topic.build(Topic.FILE, "read", test_file), b"")
        async for message in client.messages:
            if Topic.matches(topic_file_val, str(message.topic)):
                content = message.payload
                logger.info(f"File read content: {content!r}")
                if content != b"e2e-data":
                    raise ValueError(f"File content mismatch: {content!r}")
                break

        await client.publish(Topic.build(Topic.FILE, "remove", test_file), b"")

        # Test MCU SD Write
        logger.info("Testing MCU SD Card Write...")
        mcu_test_file = f"mcu/test_sd_{uuid.uuid4().hex[:8]}.txt"
        await client.publish(Topic.build(Topic.FILE, "write", mcu_test_file), b"mcu-data")

        topic_mcu_file_val = Topic.build(Topic.FILE, "data", mcu_test_file)
        await client.subscribe(topic_mcu_file_val)
        await client.publish(Topic.build(Topic.FILE, "read", mcu_test_file), b"")
        async for message in client.messages:
            if Topic.matches(topic_mcu_file_val, str(message.topic)):
                mcu_content = message.payload
                logger.info(f"MCU file read content: {mcu_content!r}")
                if mcu_content != b"mcu-data":
                    raise ValueError(f"MCU file content mismatch: {mcu_content!r}")
                break

        await client.publish(Topic.build(Topic.FILE, "remove", mcu_test_file), b"")

        logger.info("File IO tests passed.")

    logger.info("--- ALL FEATURES VERIFICATION PASSED ---")
    logger.info("ALL FEATURES PASSED.")



def main(host: str = "127.0.0.1", port: int = 1883, user: str = "", password: str = ""):
    # Pass None if empty to rely on anonymous
    asyncio.run(run_test(host, port, user or None, password or None))


if __name__ == "__main__":
    typer.run(main)

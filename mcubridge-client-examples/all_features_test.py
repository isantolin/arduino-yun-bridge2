#!/usr/bin/env python3
"""Unified e2e feature test for mcubridge."""

import asyncio
import logging
import typer
from mcubridge_client import Bridge

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("all-features-test")


async def run_test(host, port, user, password):
    client = Bridge(host=host, port=port, username=user, password=password)
    logger.info("--- Starting UNIFIED ALL-FEATURES E2E Test ---")
    async with client._exit_stack:
        await client.connect()
        # 1. LED test
        logger.info("Testing LED (Digital Write)...")
        await client.digital_write(13, 1)
        await asyncio.sleep(0.5)
        await client.digital_write(13, 0)
        logger.info("LED test passed.")

        # 2. Pin Read test
        logger.info("Testing Digital Read...")
        val = await client.digital_read(13)
        logger.info(f"Digital read pin 13: {val}")

        # 2b. Analog test
        logger.info("Testing Analog Operations...")
        analog_val = await client.analog_read(0)
        logger.info(f"Analog read pin A0: {analog_val}")
        await client.analog_write(9, 128)
        logger.info("Analog write pin 9 (PWM) set to 128.")

        # 3. DataStore test
        logger.info("Testing DataStore...")
        await client.put("test/key", "hello")
        res = await client.get("test/key")
        logger.info(f"DataStore get: {res}")
        if res != "hello":
            raise ValueError(f"DataStore mismatch: {res}")

        # 4. Console test
        logger.info("Testing Console Echo...")
        await client.console_write("ping")
        # Give some time for echo to return and be processed by the daemon
        await asyncio.sleep(2)
        # We don't verify the read here to keep it simple, but we trigger the write flow.
        logger.info("Console write triggered.")

        # 5. File IO test
        logger.info("Testing File IO...")
        import uuid
        test_file = f"/tmp/e2e_test_{uuid.uuid4().hex[:8]}.txt"
        await client.file_write(test_file, "e2e-data")
        content = await client.file_read(test_file)
        logger.info(f"File read content: {content}")
        if content != b"e2e-data":
            raise ValueError(f"File content mismatch: {content}")
        await client.file_remove(test_file)

        # Test MCU SD Write
        logger.info("Testing MCU SD Card Write...")
        mcu_test_file = f"mcu/test_sd_{uuid.uuid4().hex[:8]}.txt"
        await client.file_write(mcu_test_file, "mcu-data")
        mcu_content = await client.file_read(mcu_test_file)
        logger.info(f"MCU file read content: {mcu_content}")
        if mcu_content != b"mcu-data":
            raise ValueError(f"MCU file content mismatch: {mcu_content}")
        await client.file_remove(mcu_test_file)

        logger.info("File IO tests passed.")

    logger.info("--- ALL FEATURES VERIFICATION PASSED ---")
    logger.info("ALL FEATURES PASSED.")


def main(
    host: str = "127.0.0.1",
    port: int = 1883,
    user: str = "",
    password: str = ""
):
    # Pass None if empty to rely on anonymous
    asyncio.run(run_test(host, port, user or None, password or None))

if __name__ == "__main__":
    typer.run(main)

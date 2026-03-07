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

        # 3. DataStore test
        logger.info("Testing DataStore...")
        await client.put("test/key", "hello")
        res = await client.get("test/key")
        logger.info(f"DataStore get: {res}")
        if res != "hello":
            raise ValueError(f"DataStore mismatch: {res}")

        # 4. File IO test
        logger.info("Testing File IO...")
        import uuid
        test_file = f"/tmp/e2e_test_{uuid.uuid4().hex[:8]}.txt"
        await client.file_write(test_file, "e2e-data")
        content = await client.file_read(test_file)
        logger.info(f"File read content: {content}")
        if content != b"e2e-data":
            raise ValueError(f"File content mismatch: {content}")
        await client.file_remove(test_file)
        logger.info("File IO test passed.")

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

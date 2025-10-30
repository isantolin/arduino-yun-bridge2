#!/usr/bin/env python
import asyncio
import logging
import os
import sys

# Add parent directory to Python path
from yunbridge_client import Bridge

# Configure basic logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    bridge = Bridge()
    await bridge.connect() # Explicitly connect

    try:
        logger.info('Testing digital port 13 (builtin led)')
        for i in range(2):
            await bridge.digital_write(13, 1)
            logger.info('LED 13 ON')
            await asyncio.sleep(1)
            await bridge.digital_write(13, 0)
            logger.info('LED 13 OFF')
            await asyncio.sleep(1)

        logger.info('Testing analog port 0')
        value: int = await bridge.analog_read(0)
        logger.info(f'Analog value {value}')

        logger.info('Testing digital port 2')
        value_digital: int = await bridge.digital_read(2)
        logger.info(f'Digital value {value_digital}')

        logger.info('Testing datastore')
        await bridge.put('mykey', 'myvalue')
        retrieved_value: str = await bridge.get('mykey', timeout=10)
        logger.info(f'Get value {retrieved_value}')

        logger.info('Testing RAM memory free (simulated)')
        free_memory: int = await bridge.get_free_memory() # This is simulated in the client
        logger.info(f'Free memory {free_memory}')

        logger.info('Testing run_sketch_command (mapped to sync shell command, returns full output)')
        command_output: bytes = await bridge.run_sketch_command(['/bin/ls', '-l', '/']) # This will return the full output
        logger.info(f'Process output: {command_output.decode("utf-8", errors="ignore")}')

        logger.info('Testing run_shell_command_async')
        async_pid: int = await bridge.run_shell_command_async(['sleep', '5', '&&', 'echo', 'Async command done']) # This will return the actual PID
        logger.info(f'Async process started with PID {async_pid}')
        # In a real scenario, you'd poll for status or wait for a notification
        await asyncio.sleep(1) # Give it a moment to start

        logger.info('Testing console')
        await bridge.console_write('Hello world from client')

        logger.info('Testing fileio')
        await bridge.file_write('/tmp/test_client.txt', 'Hello world from client file')
        file_content: bytes = await bridge.file_read('/tmp/test_client.txt')
        logger.info(f'File content: {file_content.decode("utf-8")}')

    finally:
        await bridge.disconnect() # Explicitly disconnect


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Exiting due to KeyboardInterrupt.")
    except Exception as e:
        logger.exception("An error occurred in main execution.")

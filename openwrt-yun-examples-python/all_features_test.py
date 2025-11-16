#!/usr/bin/env python
import asyncio
import logging

# Add parent directory to Python path
from yunbridge_client import Bridge, dump_client_env

# Configure basic logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    dump_client_env(logger)
    bridge = Bridge()
    await bridge.connect()  # Explicitly connect

    try:
        logger.info('Testing digital port 13 (builtin led)')
        for _ in range(2):
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

        logger.info('Testing RAM memory free (simulated)')  # This is simulated
        free_memory: int = await bridge.get_free_memory()  # in the client
        logger.info(f'Free memory {free_memory}')

        logger.info(
            "Testing run_sketch_command (mapped to sync shell command)"
        )
        command_output: bytes = await bridge.run_sketch_command(
            ["/bin/ls", "-l", "/"]
        )
        decoded_output = command_output.decode("utf-8", errors="ignore")
        logger.info("Process output: %s", decoded_output)

        logger.info('Testing run_shell_command_async')
        async_command = ["sleep", "5", "&&", "echo", "Async command done"]
        async_pid: int = await bridge.run_shell_command_async(async_command)
        logger.info(f'Async process started with PID {async_pid}')
        # In a real scenario, you'd poll for status or wait for a notification
        await asyncio.sleep(1)  # Give it a moment to start

        logger.info('Testing console')
        await bridge.console_write('Hello world from client')

        logger.info('Testing fileio')
        test_path = "/tmp/test_client.txt"
        payload = "Hello world from client file"
        await bridge.file_write(test_path, payload)
        file_content: bytes = await bridge.file_read(test_path)
        logger.info("File content: %s", file_content.decode("utf-8"))

    finally:
        await bridge.disconnect()  # Explicitly disconnect


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Exiting due to KeyboardInterrupt.")
    except Exception:
        logger.exception("An error occurred in main execution.")

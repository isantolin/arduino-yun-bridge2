#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import typer
import uvloop

# Add parent directory to Python path
from mcubridge_client import Bridge, build_bridge_args, dump_client_env

app = typer.Typer(help="Test all bridge features.")

# Configure basic logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    tls_insecure: bool,
) -> None:
    dump_client_env(logger)

    bridge_args = build_bridge_args(
        host, port, user, password, tls_insecure, disable_tls=not tls_insecure,
    )
    bridge = Bridge(**bridge_args)  # type: ignore[arg-type]
    await bridge.connect()  # Explicitly connect

    try:
        logger.info("Testing digital port 13 (builtin led)")
        for _ in range(2):
            await bridge.digital_write(13, 1)
            logger.info("LED 13 ON")
            await asyncio.sleep(1)
            await bridge.digital_write(13, 0)
            logger.info("LED 13 OFF")
            await asyncio.sleep(1)

        logger.info("Testing analog port 0")
        value: int = await bridge.analog_read(0)
        logger.info("Analog value %d", value)

        logger.info("Testing digital port 2")
        value_digital: int = await bridge.digital_read(2)
        logger.info("Digital value %d", value_digital)

        logger.info("Testing datastore")
        await bridge.put("mykey", "myvalue")
        retrieved_value: str = await bridge.get("mykey", timeout=10)
        logger.info("Get value %s", retrieved_value)

        logger.info("Testing RAM memory free (simulated)")  # This is simulated
        free_memory: int = await bridge.get_free_memory()  # in the client
        logger.info("Free memory %d", free_memory)

        logger.info("Testing run_sketch_command (mapped to sync shell command)")
        command_output: bytes = await bridge.run_sketch_command(["/bin/ls", "-l", "/"])
        decoded_output = command_output.decode("utf-8", errors="ignore")
        logger.info("Process output: %s", decoded_output)

        logger.info("Testing run_shell_command_async")
        async_command = ["sleep", "5", "&&", "echo", "Async command done"]
        async_pid: int = await bridge.run_shell_command_async(async_command)
        logger.info("Async process started with PID %d", async_pid)
        # In a real scenario, you'd poll for status or wait for a notification
        await asyncio.sleep(1)  # Give it a moment to start

        logger.info("Testing console")
        await bridge.console_write("Hello world from client")

        logger.info("Testing fileio")
        test_path = "/tmp/test_client.txt"
        payload = "Hello world from client file"
        await bridge.file_write(test_path, payload)
        file_content: bytes = await bridge.file_read(test_path)
        logger.info("File content: %s", file_content.decode("utf-8"))

    finally:
        await bridge.disconnect()  # Explicitly disconnect


@app.command()
def main(
    host: Annotated[str | None, typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[int | None, typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[str | None, typer.Option(help="MQTT Username")] = None,
    password: Annotated[str | None, typer.Option(help="MQTT Password")] = None,
    tls_insecure: Annotated[bool, typer.Option(help="Disable TLS certificate verification")] = False,
) -> None:
    try:
        asyncio.run(run_test(host, port, user, password, tls_insecure))
    except KeyboardInterrupt:
        logger.info("Exiting due to KeyboardInterrupt.")
    except (OSError, RuntimeError, ValueError) as exc:
        logger.critical("Fatal error in main execution: %s", exc)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    # [10/10 Efficiency] Use uvloop for maximum performance
    uvloop.install()
    app()

#!/usr/bin/env python3
import asyncio
import logging
import ssl
from typing import Annotated

import typer
import uvloop

# Add parent directory to Python path
from mcubridge_client import Bridge, dump_client_env

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

    # Concise argument mapping
    base_args = {
        "host": host,
        "port": port,
        "username": user,
        "password": password,
    }
    bridge_args = {k: v for k, v in base_args.items() if v is not None}

    if tls_insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        bridge_args["tls_context"] = ctx
    else:
        # [Local E2E Fix] Explicitly disable SSL for local development
        bridge_args["tls_context"] = None

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
        logger.info(f"Analog value {value}")

        logger.info("Testing digital port 2")
        value_digital: int = await bridge.digital_read(2)
        logger.info(f"Digital value {value_digital}")

        logger.info("Testing datastore")
        await bridge.put("mykey", "myvalue")
        retrieved_value: str = await bridge.get("mykey", timeout=10)
        logger.info(f"Get value {retrieved_value}")

        logger.info("Testing RAM memory free (simulated)")  # This is simulated
        free_memory: int = await bridge.get_free_memory()  # in the client
        logger.info(f"Free memory {free_memory}")

        logger.info("Testing run_sketch_command (mapped to sync shell command)")
        command_output: bytes = await bridge.run_sketch_command(["/bin/ls", "-l", "/"])
        decoded_output = command_output.decode("utf-8", errors="ignore")
        logger.info("Process output: %s", decoded_output)

        logger.info("Testing run_shell_command_async")
        async_command = ["sleep", "5", "&&", "echo", "Async command done"]
        async_pid: int = await bridge.run_shell_command_async(async_command)
        logger.info(f"Async process started with PID {async_pid}")
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
    tls_insecure: Annotated[
        bool, typer.Option(help="Disable TLS certificate verification")
    ] = False,
) -> None:
    try:
        asyncio.run(run_test(host, port, user, password, tls_insecure))
    except KeyboardInterrupt:
        logger.info("Exiting due to KeyboardInterrupt.")
    except (OSError, RuntimeError, ValueError) as exc:
        logger.critical("Fatal error in main execution: %s", exc)


if __name__ == "__main__":
    # [10/10 Efficiency] Use uvloop for maximum performance
    uvloop.install()
    app()

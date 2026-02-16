#!/usr/bin/env python3
"""Interactive console helper for the Arduino bridge."""

import asyncio
import logging
import argparse
import ssl

from mcubridge_client import Bridge, dump_client_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def main() -> None:
    """Run main test logic."""
    parser = argparse.ArgumentParser(description="Interactive console test.")
    parser.add_argument("--host", default=None, help="MQTT Broker Host")
    parser.add_argument("--port", type=int, default=None, help="MQTT Broker Port")
    parser.add_argument("--user", default=None, help="MQTT Username")
    parser.add_argument("--password", default=None, help="MQTT Password")
    parser.add_argument("--tls-insecure", action="store_true", help="Disable TLS certificate verification")
    args = parser.parse_args()

    # Validate essential arguments if not running on OpenWrt with UCI
    if not args.host or not args.user or not args.password:
        from mcubridge_client.env import read_uci_general
        if not read_uci_general():
            logging.info("Error: Missing required connection parameters.")
            parser.print_help()
            return

    dump_client_env(logging.getLogger(__name__))

    bridge_args: dict[str, object] = {}
    if args.host:
        bridge_args["host"] = args.host
    if args.port:
        bridge_args["port"] = args.port
    if args.user:
        bridge_args["username"] = args.user
    if args.password:
        bridge_args["password"] = args.password
    if args.tls_insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        bridge_args["tls_context"] = ctx

    bridge = Bridge(**bridge_args)  # type: ignore[arg-type]
    await bridge.connect()

    logging.info("Enter text to send to the Arduino console. Type 'exit' to quit.")

    try:
        # Start a task to listen for console messages
        async def console_listener() -> None:
            while True:
                message = await bridge.console_read_async()
                if message is not None:
                    logging.info("Received from Arduino: %s", message)
                else:
                    await asyncio.sleep(0.1)

        listener_task: asyncio.Task[None] = asyncio.create_task(console_listener())

        while True:
            try:
                # Run blocking input in a separate thread
                user_input = await asyncio.to_thread(input)
                if user_input.lower() == "exit":
                    break
                await bridge.console_write(user_input)
            except EOFError:
                break

        # Clean up the listener task
        listener_task.cancel()
        await asyncio.gather(listener_task, return_exceptions=True)

    except asyncio.CancelledError:
        logging.info("\nExiting...")
    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

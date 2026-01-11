#!/usr/bin/env python3
"""Example: Test generic pin control using the async McuBridge client."""

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
    parser = argparse.ArgumentParser(description="Test generic pin control.")
    parser.add_argument("pin", type=int, nargs="?", default=13, help="Pin number (default: 13)")
    parser.add_argument("--host", default=None, help="MQTT Broker Host")
    parser.add_argument("--port", type=int, default=None, help="MQTT Broker Port")
    parser.add_argument("--user", default=None, help="MQTT Username")
    parser.add_argument("--password", default=None, help="MQTT Password")
    parser.add_argument("--tls-insecure", action="store_true", help="Disable TLS certificate verification")
    args = parser.parse_args()

    dump_client_env(logging.getLogger(__name__))

    # Build arguments dict, only including provided values to allow Bridge defaults (env vars) to work
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

    # Option 2: Pass credentials to the constructor
    # If None, the library will try to use environment variables
    bridge = Bridge(**bridge_args)  # type: ignore[arg-type]

    await bridge.connect()

    pin: int = args.pin

    try:
        logging.info(f"Turning pin {pin} ON")
        await bridge.digital_write(pin, 1)
        await asyncio.sleep(2)

        logging.info(f"Turning pin {pin} OFF")
        await bridge.digital_write(pin, 0)
        await asyncio.sleep(2)

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()

    logging.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""Poll sensor values via the async bridge client."""

import asyncio
import logging
import ssl
import sys
from typing import Optional, Annotated

import typer
from mcubridge_client import Bridge, dump_client_env

app = typer.Typer(help="Poll sensor values via the async bridge client.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def run_test(
    host: Optional[str],
    port: Optional[int],
    user: Optional[str],
    password: Optional[str],
    pin: str,
    interval: float,
    tls_insecure: bool,
) -> None:
    # Validate essential arguments if not running on OpenWrt with UCI
    if not host or not user or not password:
        from mcubridge_client.env import read_uci_general

        if not read_uci_general():
            sys.stderr.write("Error: Missing required connection parameters.\n")
            raise typer.Exit(code=1)

    dump_client_env(logging.getLogger(__name__))

    bridge_args: dict[str, object] = {}
    if host:
        bridge_args["host"] = host
    if port:
        bridge_args["port"] = port
    if user:
        bridge_args["username"] = user
    if password:
        bridge_args["password"] = password
    if tls_insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        bridge_args["tls_context"] = ctx

    bridge = Bridge(**bridge_args)  # type: ignore[arg-type]
    await bridge.connect()

    logging.info(
        "Requesting a reading from pin %s every %.1f seconds.",
        pin,
        interval,
    )
    logging.info("Press Ctrl+C to exit.")

    is_analog = pin.lower().startswith("a")
    # Handle optional 'd' or 'a' prefix safely
    try:
        raw_pin_str = pin[1:] if pin[0].isalpha() else pin
        pin_number = int(raw_pin_str)
    except ValueError:
        logging.error(f"Invalid pin format: {pin}")
        raise typer.Exit(code=1)

    try:
        start_time = asyncio.get_running_loop().time()
        while True:
            if asyncio.get_running_loop().time() - start_time > 10.0:
                logging.info("Test duration of 10 seconds exceeded. Finishing.")
                break

            if is_analog:
                value: int = await bridge.analog_read(pin_number)
                logging.info(
                    "Received analog value for pin %s: %d",
                    pin,
                    value,
                )
            else:
                value = await bridge.digital_read(pin_number)
                logging.info(
                    "Received digital value for pin %s: %d",
                    pin,
                    value,
                )

            await asyncio.sleep(interval)

    except asyncio.CancelledError:
        logging.info("\nExiting...")
    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()


@app.command()
def main(
    host: Annotated[Optional[str], typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[Optional[int], typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[Optional[str], typer.Option(help="MQTT Username")] = None,
    password: Annotated[Optional[str], typer.Option(help="MQTT Password")] = None,
    pin: Annotated[str, typer.Option(help="Pin to read (e.g., 'd13' or 'a0').")] = "d13",
    interval: Annotated[float, typer.Option(help="Read interval in seconds.")] = 2.0,
    tls_insecure: Annotated[bool, typer.Option(help="Disable TLS certificate verification")] = False,
) -> None:
    try:
        asyncio.run(run_test(host, port, user, password, pin, interval, tls_insecure))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    app()

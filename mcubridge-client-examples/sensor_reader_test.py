#!/usr/bin/env python3
"""Poll sensor values via the async bridge client."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import typer
from mcubridge_client import Bridge, build_bridge_args, dump_client_env

app = typer.Typer(help="Poll sensor values via the async bridge client.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    pin: str,
    interval: float,
    tls_insecure: bool,
) -> None:

    dump_client_env(logging.getLogger(__name__))

    bridge_args = build_bridge_args(host, port, user, password, tls_insecure)
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
    host: Annotated[str | None, typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[int | None, typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[str | None, typer.Option(help="MQTT Username")] = None,
    password: Annotated[str | None, typer.Option(help="MQTT Password")] = None,
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

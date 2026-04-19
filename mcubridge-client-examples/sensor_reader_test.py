#!/usr/bin/env python3
"""Poll sensor values via the async bridge client."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import typer
from mcubridge_client import Topic, build_bridge_args, get_client
from mcubridge_client.cli import configure_logging

app = typer.Typer(help="Poll sensor values via the async bridge client.")
configure_logging()


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    pin: str,
    interval: float,
    tls_insecure: bool,
) -> None:

    async with get_client(**build_bridge_args(host, port, user, password, tls_insecure)) as client:
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

        topic_type = Topic.ANALOG if is_analog else Topic.DIGITAL
        topic_val = Topic.build(topic_type, pin_number, "value")
        topic_read = Topic.build(topic_type, pin_number, "read")

        await client.subscribe(topic_val)

        start_time = asyncio.get_running_loop().time()
        while True:
            if asyncio.get_running_loop().time() - start_time > 10.0:
                logging.info("Test duration of 10 seconds exceeded. Finishing.")
                break

            await client.publish(topic_read, b"")

            async for message in client.messages:
                if Topic.matches(topic_val, str(message.topic)):
                    value = int(message.payload.decode())
                    logging.info(
                        "Received %s value for pin %s: %d",
                        "analog" if is_analog else "digital",
                        pin,
                        value,
                    )
                    break

            await asyncio.sleep(interval)



@app.command()
def main(
    host: Annotated[str | None, typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[int | None, typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[str | None, typer.Option(help="MQTT Username")] = None,
    password: Annotated[str | None, typer.Option(help="MQTT Password")] = None,
    pin: Annotated[
        str, typer.Option(help="Pin to read (e.g., 'd13' or 'a0').")
    ] = "d13",
    interval: Annotated[float, typer.Option(help="Read interval in seconds.")] = 2.0,
    tls_insecure: Annotated[
        bool, typer.Option(help="Disable TLS certificate verification")
    ] = False,
) -> None:
    try:
        asyncio.run(run_test(host, port, user, password, pin, interval, tls_insecure))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    app()

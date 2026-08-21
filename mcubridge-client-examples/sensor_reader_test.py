#!/usr/bin/env python3
"""Poll sensor values via direct LocalBridgeStub Publish calls."""

from __future__ import annotations

import asyncio
import structlog
import typer
from typing import Annotated

from mcubridge_client import pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
    pin: str,
    interval: float,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        logger.info(
            "Requesting a reading from pin %s every %.1f seconds.",
            pin,
            interval,
        )
        logger.info("Press Ctrl+C to exit.")

        is_analog = pin.lower().startswith("a")
        try:
            raw_pin_str = pin[1:] if pin[0].isalpha() else pin
            pin_number = int(raw_pin_str)
        except ValueError:
            logger.error("Invalid pin format", pin=pin)
            raise SystemExit(1)

        start_time = asyncio.get_running_loop().time()
        while True:
            if asyncio.get_running_loop().time() - start_time > 20.0:
                logger.info("Test duration of 20 seconds exceeded. Finishing.")
                break

            if is_analog:
                resp_ar = await stub.AnalogRead(pb.PinRead(pin=pin_number))
                logger.info("Received analog value", pin=pin, value=resp_ar.value)
            else:
                resp_dr = await stub.DigitalRead(pb.PinRead(pin=pin_number))
                logger.info("Received digital value", pin=pin, value=resp_dr.value)

            await asyncio.sleep(interval)

    logger.info("Done.")


def main(
    socket_path: str | None = None,
    topic_prefix: str = "br",
    pin: str = "A0",
    interval: float = 1.0,
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix, pin, interval))


cli = typer.Typer(help="Poll sensor values via direct LocalBridgeStub.", add_completion=False)


@cli.command()
def cli_main(
    socket_path: Annotated[str | None, typer.Option("--socket-path", help="UNIX Domain Socket Path")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
    pin: Annotated[str, typer.Option("--pin", help="Pin to read (e.g. A0, A1, D13, 13)")] = "A0",
    interval: Annotated[float, typer.Option("--interval", help="Poll interval in seconds")] = 1.0,
) -> None:
    main(socket_path, topic_prefix, pin, interval)


if __name__ == "__main__":
    cli()

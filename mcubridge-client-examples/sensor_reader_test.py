#!/usr/bin/env python3
"""Poll sensor values via direct LocalBridgeStub Publish calls."""

from __future__ import annotations

import asyncio
import logging
import typer
from typing import Annotated

from mcubridge_client import Topic, parse_pin_spec, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
    pin: str,
    interval: float,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        logging.info(
            "Requesting a reading from pin %s every %.1f seconds.",
            pin,
            interval,
        )
        logging.info("Press Ctrl+C to exit.")

        topic_type, pin_number = parse_pin_spec(pin)
        if pin_number < 0:
            logging.error("Invalid pin format: %s", pin)
            raise SystemExit(1)

        start_time = asyncio.get_running_loop().time()
        while True:
            if asyncio.get_running_loop().time() - start_time > 20.0:
                logging.info("Test duration of 20 seconds exceeded. Finishing.")
                break

            topic_read = Topic.build(topic_type, str(pin_number), "read", prefix=topic_prefix)
            res = await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_read, payload=b"", qos=1))
            if not (res and res.payload):
                raise RuntimeError(f"Pin {pin} read returned empty response")
            value = int(res.payload.decode("utf-8"))
            pin_label = "analog" if topic_type == Topic.ANALOG else "digital"
            logging.info("Received %s value for pin %s: %d", pin_label, pin, value)

            await asyncio.sleep(interval)

    logging.info("Done.")


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

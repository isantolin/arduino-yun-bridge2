#!/usr/bin/env python3
"""Poll sensor values via direct LocalBridgeStub Publish calls."""

from __future__ import annotations

import argparse
import asyncio
import logging

from mcubridge_client import Topic, pb
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

        is_analog = pin.lower().startswith("a")
        try:
            raw_pin_str = pin[1:] if pin[0].isalpha() else pin
            pin_number = int(raw_pin_str)
        except ValueError:
            logging.error("Invalid pin format: %s", pin)
            raise SystemExit(1)

        start_time = asyncio.get_running_loop().time()
        while True:
            if asyncio.get_running_loop().time() - start_time > 20.0:
                logging.info("Test duration of 20 seconds exceeded. Finishing.")
                break

            if is_analog:
                topic_ar = Topic.build(Topic.ANALOG, str(pin_number), prefix=topic_prefix)
                res = await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_ar, payload=b"", qos=1))
                val_str = res.payload.decode("utf-8", errors="replace") if (res and res.payload) else "0"
                try:
                    value = int(val_str)
                except ValueError:
                    value = 0
                logging.info("Received analog value for pin %s: %d", pin, value)
            else:
                topic_dr = Topic.build(Topic.DIGITAL, str(pin_number), prefix=topic_prefix)
                res = await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_dr, payload=b"", qos=1))
                val_str = res.payload.decode("utf-8", errors="replace") if (res and res.payload) else "0"
                try:
                    value = int(val_str)
                except ValueError:
                    value = 0
                logging.info("Received digital value for pin %s: %d", pin, value)

            await asyncio.sleep(interval)

    logging.info("Done.")


def main(
    socket_path: str | None = None,
    topic_prefix: str = "br",
    pin: str = "A0",
    interval: float = 1.0,
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix, pin, interval))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poll sensor values via direct LocalBridgeStub.")
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    parser.add_argument("--pin", default="A0", help="Pin to read (e.g. A0, A1, D13, 13)")
    parser.add_argument("--interval", type=float, default=1.0, help="Poll interval in seconds")
    _args = parser.parse_args()
    main(
        _args.socket_path,
        _args.topic_prefix,
        _args.pin,
        _args.interval,
    )

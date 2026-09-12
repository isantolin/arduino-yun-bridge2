#!/usr/bin/env python3
"""Autonomous Pin Subscription Test for Arduino MCU Bridge."""

from __future__ import annotations

import asyncio
from typing import Annotated

import structlog
import typer

from mcubridge_client import pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger("pin-subscribe-test")


async def run_test(
    pin: int,
    mode: str,
    interval_ms: int,
    hysteresis: int,
    duration: float,
    socket_path: str | None,
    topic_prefix: str,
) -> None:
    """Execute autonomous pin subscription test lifecycle."""
    logger.info("--- Starting Pin Subscription Test ---")

    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        mode_upper = mode.upper()
        pb_mode = getattr(pb.PinModeType, f"PIN_{mode_upper}", pb.PinModeType.PIN_INPUT)

        logger.info(
            "Registering pin subscription",
            pin=pin,
            mode=mode_upper,
            interval_ms=interval_ms,
            hysteresis=hysteresis,
        )

        sub_req = pb.PinSubscribeRequest(
            pin=pin,
            mode=pb_mode,
            interval_ms=interval_ms,
            hysteresis=hysteresis,
            enabled=True,
        )
        resp = await stub.PinSubscribe(sub_req)
        if not resp.success:
            logger.error("MCU rejected pin subscription", pin=pin)
            raise RuntimeError(f"MCU rejected subscription for pin {pin}")

        logger.info("Subscription active on MCU", pin=resp.pin, success=resp.success)

        # Introspect bridge status snapshot
        status = await stub.GetStatus(pb.SubscribeRequest())
        logger.info(
            "Bridge status verified",
            is_synchronized=status.bridge.handshake.synchronised,
            failure_streak=status.bridge.handshake.failure_streak,
        )

        logger.info("Observing active subscription window", duration_seconds=duration)
        await asyncio.sleep(duration)

        # Deterministic teardown: Cancel subscription
        logger.info("Cancelling pin subscription", pin=pin)
        unsub_req = pb.PinSubscribeRequest(
            pin=pin,
            enabled=False,
        )
        unsub_resp = await stub.PinSubscribe(unsub_req)
        if not unsub_resp.success:
            logger.error("Failed to cancel pin subscription", pin=pin)
            raise RuntimeError(f"Failed to cancel subscription for pin {pin}")

        logger.info("Subscription cancelled successfully", pin=pin)

    logger.info("--- Pin Subscription Test Succeeded ---")


def main(
    pin: int = 13,
    mode: str = "INPUT",
    interval_ms: int = 100,
    hysteresis: int = 1,
    duration: float = 2.0,
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    """CLI runner entry point."""
    asyncio.run(
        run_test(
            pin=pin,
            mode=mode,
            interval_ms=interval_ms,
            hysteresis=hysteresis,
            duration=duration,
            socket_path=socket_path,
            topic_prefix=topic_prefix,
        )
    )


cli = typer.Typer(
    help="Autonomous hardware pin subscription test via LocalBridgeStub.",
    add_completion=False,
)


@cli.command()
def cli_main(
    pin: Annotated[int, typer.Option("--pin", help="Target pin number")] = 13,
    mode: Annotated[str, typer.Option("--mode", help="Pin mode (INPUT, INPUT_PULLUP, OUTPUT)")] = "INPUT",
    interval_ms: Annotated[int, typer.Option("--interval-ms", help="Sampling interval in ms")] = 100,
    hysteresis: Annotated[int, typer.Option("--hysteresis", help="Threshold hysteresis for reporting")] = 1,
    duration: Annotated[float, typer.Option("--duration", help="Observation duration in seconds")] = 2.0,
    socket_path: Annotated[str | None, typer.Option("--socket-path", help="UNIX Domain Socket Path")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    """Execute pin subscription test."""
    main(
        pin=pin,
        mode=mode,
        interval_ms=interval_ms,
        hysteresis=hysteresis,
        duration=duration,
        socket_path=socket_path,
        topic_prefix=topic_prefix,
    )


if __name__ == "__main__":
    cli()

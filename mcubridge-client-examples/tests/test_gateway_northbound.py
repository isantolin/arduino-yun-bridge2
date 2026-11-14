#!/usr/bin/env python3
"""[SIL-2] End-to-end test verifying northbound command orchestration through Pigweed Gateway.

Flow:
Northbound Client -> GatewayControl.Dispatch -> mcubridge-gateway-cpp
-> EdgeControl.Dispatch -> McuBridge Daemon -> Serial GpioService.DigitalWrite
-> MCU -> Serial ACK -> McuBridge Daemon -> EdgeDispatchResponse
-> mcubridge-gateway-cpp -> DispatchResponse -> Northbound Client.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any

import structlog
import typer
from mcubridge_client import GatewayClient, GatewayConnection, mcu_wire_pb2
from mcubridge_client.cli import build_gateway_ssl_context, configure_logging

logger = structlog.get_logger("test-gateway-northbound")


async def run_test(
    host: str,
    port: int,
    device_id: str,
    connection_factory: Callable[[str, int], Any] | None = None,
    client_factory: Callable[[Any, str], GatewayClient] | None = None,
) -> None:
    logger.info("Connecting to Cloud Gateway northbound endpoint", host=host, port=port, device_id=device_id)
    conn = (
        connection_factory(host, port)
        if connection_factory is not None
        # The standalone Gateway only accepts mTLS peers (ADR-0003).
        else GatewayConnection(host, port, ssl_context=build_gateway_ssl_context())
    )
    client = client_factory(conn, device_id) if client_factory is not None else GatewayClient(conn, device_id)
    try:
        req = mcu_wire_pb2.DigitalWrite(pin=13, value=1)
        logger.info("Dispatching DigitalWrite to gateway", target_device_id=device_id)
        resp = await client.DigitalWrite(req)
        assert resp is not None
        logger.info("Northbound roundtrip test PASSED")
    finally:
        conn.close()


executor_fn: Callable[..., Any] = run_test

cli = typer.Typer(help="Northbound command test script using Pigweed Gateway.", add_completion=False)


@cli.command()
def main(
    host: Annotated[str, typer.Option("--host", help="Cloud Gateway host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Cloud Gateway port")] = 8443,
    device_id: Annotated[
        str, typer.Option("--device-id", help="Explicit target device ID", envvar="MCUBRIDGE_DEVICE_ID")
    ] = "",
) -> None:
    if not device_id:
        raise typer.BadParameter("Explicit target device_id is required")
    configure_logging()
    asyncio.run(executor_fn(host, port, device_id))


if __name__ == "__main__":
    cli()

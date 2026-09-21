#!/usr/bin/env python3
"""[SIL-2] End-to-end test verifying northbound command orchestration through ProtobufGateway.

Flow:
Northbound Client -> CloudBridge.DispatchCommand -> ProtobufGateway.send_command
-> CloudEnvelope(command_request) -> McuBridge Daemon -> Serial CMD_DIGITAL_WRITE
-> MCU Emulator -> Serial ACK -> McuBridge Daemon -> CloudEnvelope(command_response)
-> ProtobufGateway -> Northbound Client.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from grpclib.client import Channel
import structlog
import typer

from mcubridge.protocol import mcubridge_grpc, mcubridge_pb2 as pb
from mcubridge_client.cli import configure_logging

configure_logging()
logger = structlog.get_logger("test-gateway-northbound")


async def run_test(host: str, port: int, device_id: str) -> None:
    logger.info("Connecting to Cloud Gateway northbound endpoint", host=host, port=port, device_id=device_id)
    channel = Channel(host, port)
    stub = mcubridge_grpc.CloudBridgeStub(channel)
    try:
        dispatch = pb.CommandDispatch(
            target_device_id=device_id,
            command_path="rpc/DigitalWrite",
            payload=pb.DigitalWrite(pin=13, value=1).SerializeToString(),
            timeout_seconds=5,
        )
        logger.info("Dispatching command to gateway", command=dispatch.command_path, target_device_id=device_id)
        response = await stub.DispatchCommand(dispatch)

        logger.info(
            "Received northbound command response",
            status_code=response.status_code,
            payload=response.payload,
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.payload!r}"
        if response.payload == b"OK":
            pass
        else:
            gen_resp = pb.GenericResponse()
            gen_resp.ParseFromString(response.payload)
            assert gen_resp.status == "ok", f"Expected ok status, got {gen_resp.status}"
        logger.info("Northbound roundtrip test PASSED")
    finally:
        channel.close()


cli = typer.Typer(
    help="Northbound Cloud Gateway E2E Command Orchestration Test",
    add_completion=False,
)


@cli.command()
def main(
    host: Annotated[str, typer.Option("--host", help="Gateway Host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Gateway Port")] = 8443,
    device_id: Annotated[
        str | None, typer.Option("--device-id", help="Explicit target device ID", envvar="MCUBRIDGE_DEVICE_ID")
    ] = None,
) -> None:
    if not device_id:
        raise ValueError("Explicit target device_id is required. Implicit fallback is prohibited.")
    asyncio.run(run_test(host, port, device_id))


if __name__ == "__main__":
    cli()

#!/usr/bin/env python3
"""Example: Test pw_transfer streaming and MPU file write/remove through the Gateway."""

from __future__ import annotations

import asyncio
from typing import Annotated

import structlog
import typer
from mcubridge_client import mcu_wire_pb2
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
) -> None:

    async with bridge_session(host=host, port=port, device_id=device_id) as (_channel, stub):
        # --- 1. Test pw_transfer streaming read/write ---
        transfer_resource_id: int = 1
        transfer_payload: bytes = b"hello from pw_transfer streaming fileio_test"
        logger.info("Writing resource via pw_transfer", resource_id=transfer_resource_id, size=len(transfer_payload))
        await stub.transfer_write(transfer_resource_id, transfer_payload)

        logger.info("Reading resource via pw_transfer", resource_id=transfer_resource_id)
        read_back = await stub.transfer_read(transfer_resource_id)
        logger.info("Resource read back successfully", size=len(read_back), matches=read_back == transfer_payload)
        if read_back != transfer_payload:
            raise RuntimeError(f"pw_transfer mismatch: expected {transfer_payload!r}, got {read_back!r}")

        # --- 2. Test MPU File Transfer via pw_transfer Staging & FileTransferService ---
        test_filename: str = "test_file.txt"
        test_content: bytes = b"hello from pw_transfer streaming fileio_test"

        try:
            logger.info("Pushing file to Linux device via pw_transfer staging", filename=test_filename)
            await stub.push_bytes_to_device(test_content, test_filename)
            logger.info("File pushed to MPU safe path")

            logger.info("Fetching file from Linux device via pw_transfer staging", filename=test_filename)
            fetched_content = await stub.fetch_bytes_from_device(test_filename)
            logger.info("File fetched back", matches=fetched_content == test_content)
            if fetched_content != test_content:
                raise RuntimeError(f"File content mismatch: expected {test_content!r}, got {fetched_content!r}")
        finally:
            logger.info("Removing file", filename=test_filename)
            await stub.FileRemove(
                mcu_wire_pb2.FileRemove(
                    path=test_filename,
                )
            )

    logger.info("Done.")


def main(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
) -> None:
    asyncio.run(run_test(host, port, device_id))


cli = typer.Typer(help="Test file I/O using direct GatewayClient through Gateway.", add_completion=False)


@cli.command()
def cli_main(
    host: Annotated[str | None, typer.Option("--host", help="Cloud Gateway host")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Cloud Gateway port")] = None,
    device_id: Annotated[
        str | None, typer.Option("--device-id", help="Explicit target device ID", envvar="MCUBRIDGE_DEVICE_ID")
    ] = None,
) -> None:
    main(host, port, device_id)


if __name__ == "__main__":
    cli()

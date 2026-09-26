#!/usr/bin/env python3
"""Example: Test file I/O using direct LocalBridgeStub Publish calls."""

from __future__ import annotations

import asyncio
from typing import Annotated

import structlog
import typer
from mcubridge_client import pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:

    async with bridge_session(host=host, port=port, device_id=device_id, topic_prefix=topic_prefix) as (_channel, stub):
        test_filename: str = "test_file.txt"
        test_content: str = "hello from async fileio_test"

        try:
            # --- Test File Write ---
            logger.info("Writing file", filename=test_filename, content=test_content)
            await stub.FileWrite(
                pb.FileWrite(
                    path=test_filename,
                    data=test_content.encode("utf-8"),
                )
            )

            # --- Test File Read ---
            logger.info("Reading file", filename=test_filename)
            res = await stub.FileRead(
                pb.FileRead(
                    path=test_filename,
                )
            )
            content = res.content if res else b""
            logger.info("Read file content", content=content.decode("utf-8"))
            if content != test_content.encode("utf-8"):
                raise AssertionError(
                    f"File content mismatch: expected {test_content!r}, got {content.decode('utf-8')!r}"
                )

        finally:
            # --- Test File Remove ---
            logger.info("Removing file", filename=test_filename)
            await stub.FileRemove(
                pb.FileRemove(
                    path=test_filename,
                )
            )

    logger.info("Done.")


def main(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(host, port, device_id, topic_prefix))


cli = typer.Typer(help="Test file I/O using direct LocalBridgeStub through Gateway.", add_completion=False)


@cli.command()
def cli_main(
    host: Annotated[str | None, typer.Option("--host", help="Cloud Gateway host")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Cloud Gateway port")] = None,
    device_id: Annotated[
        str | None, typer.Option("--device-id", help="Explicit target device ID", envvar="MCUBRIDGE_DEVICE_ID")
    ] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(host, port, device_id, topic_prefix)


if __name__ == "__main__":
    cli()

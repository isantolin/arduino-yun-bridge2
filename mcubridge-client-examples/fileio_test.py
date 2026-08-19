#!/usr/bin/env python3
"""Example: Test file I/O using direct LocalBridgeStub Publish calls."""

from __future__ import annotations

import asyncio
import structlog
import typer
from typing import Annotated

from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        test_filename: str = "test_file.txt"
        test_content: str = "hello from async fileio_test"

        topic_fw = Topic.build(Topic.FILE, "write", test_filename, prefix=topic_prefix)
        topic_fr = Topic.build(Topic.FILE, "read", test_filename, prefix=topic_prefix)
        topic_frm = Topic.build(Topic.FILE, "remove", test_filename, prefix=topic_prefix)

        try:
            # --- Test File Write ---
            logger.info("Writing file", filename=test_filename, content=test_content)
            await stub.Publish(
                pb.CloudQueuedPublish(
                    topic_name=topic_fw,
                    payload=test_content.encode("utf-8"),
                    qos=1,
                )
            )

            # --- Test File Read ---
            logger.info("Reading file", filename=test_filename)
            res = await stub.Publish(
                pb.CloudQueuedPublish(
                    topic_name=topic_fr,
                    payload=b"",
                    qos=1,
                )
            )
            content = res.payload if res else b""
            logger.info("Read file content", content=content.decode("utf-8"))
            if content != test_content.encode("utf-8"):
                raise AssertionError(
                    f"File content mismatch: expected {test_content!r}, got {content.decode('utf-8')!r}"
                )

        finally:
            # --- Test File Remove ---
            logger.info("Removing file", filename=test_filename)
            await stub.Publish(
                pb.CloudQueuedPublish(
                    topic_name=topic_frm,
                    payload=b"",
                    qos=1,
                )
            )

    logger.info("Done.")


def main(
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


cli = typer.Typer(help="Test file I/O using direct LocalBridgeStub.", add_completion=False)


@cli.command()
def cli_main(
    socket_path: Annotated[str | None, typer.Option("--socket-path", help="UNIX Domain Socket Path")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(socket_path, topic_prefix)


if __name__ == "__main__":
    cli()

#!/usr/bin/env python3
"""Minimal connectivity smoke test for LocalBridgeStub and Channel using bridge_session."""

from __future__ import annotations

import asyncio
import structlog
import typer
from typing import Annotated, Any, cast

from unittest.mock import MagicMock, patch
import pytest
from typer.testing import CliRunner

from mcubridge_client import dump_client_env
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:
    dump_client_env(logger)

    async with bridge_session(socket_path, topic_prefix) as (_channel, _stub):
        logger.info("Bridge channel initialized via bridge_session")


cli = typer.Typer(
    help="Minimal connectivity smoke test for LocalBridgeStub and Channel.",
    add_completion=False,
)


@cli.command()
def main(
    socket_path: Annotated[str | None, typer.Option("--socket-path", help="UNIX Domain Socket Path")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


@pytest.mark.asyncio
async def test_smoke_connection_run() -> None:
    with patch("test_smoke_connection.bridge_session") as mock_sess:
        mock_chan = MagicMock()
        mock_stub = MagicMock()
        mock_sess.return_value.__aenter__.return_value = (mock_chan, mock_stub)
        await run_test("/tmp/fake.sock", "br")
        mock_sess.assert_called_once_with("/tmp/fake.sock", "br")


def test_smoke_connection_main() -> None:
    with patch("mcubridge_client.cli.bridge_session") as mock_sess:
        mock_chan = MagicMock()
        mock_stub = MagicMock()
        mock_sess.return_value.__aenter__.return_value = (mock_chan, mock_stub)
        runner = CliRunner()
        res = runner.invoke(cast(Any, cli), ["--socket-path", "/tmp/fake.sock", "--topic-prefix", "test"])
        assert res.exit_code == 0


if __name__ == "__main__":
    cli()

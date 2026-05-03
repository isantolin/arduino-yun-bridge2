#!/usr/bin/env python3
"""Example: Run an async shell command and stream its output via MQTT polls."""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from mcubridge_client import Bridge
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()

POLL_INTERVAL = 0.5


async def _stream_poll_updates(
    bridge: Bridge,
    pid: int,
    *,
    poll_interval: float = POLL_INTERVAL,
) -> None:
    """Continuously poll the daemon for stdout/stderr chunks."""

    logger = logging.getLogger(__name__)
    while True:
        poll_payload: dict[str, Any] = await bridge.poll_shell_process(pid)

        # msgspec msgpack decoding yields bytes for stdout_chunk/stderr_chunk
        raw_stdout = poll_payload.get("stdout_chunk") or b""
        raw_stderr = poll_payload.get("stderr_chunk") or b""

        stdout_chunk = raw_stdout.decode("utf-8", errors="replace").rstrip()
        stderr_chunk = raw_stderr.decode("utf-8", errors="replace").rstrip()

        exit_code = poll_payload.get("exit_code")
        finished = bool(poll_payload.get("finished"))

        if stdout_chunk:
            logger.info("[PID %d] STDOUT: %s", pid, stdout_chunk)

        if stderr_chunk:
            logger.info("[PID %d] STDERR: %s", pid, stderr_chunk)

        if (
            finished
            and not poll_payload.get("stdout_truncated")
            and not poll_payload.get("stderr_truncated")
        ):
            if not stdout_chunk and not stderr_chunk:
                logger.info(
                    "Process %d completed with exit code %s",
                    pid,
                    exit_code,
                )
            else:
                logger.info(
                    (
                        "Process %d completed with exit code %s (final chunk logged above)"
                    ),
                    pid,
                    exit_code,
                )
            break

        if not stdout_chunk and not stderr_chunk:
            await asyncio.sleep(poll_interval)


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    tls_insecure: bool,
) -> None:

    async with bridge_session(host, port, user, password, tls_insecure) as bridge:
        command_to_run: list[str] = [
            "sh",
            "-c",
            (
                "for i in $(seq 1 4); do echo \"tick:$i\"; sleep 0.5; done; >&2 echo 'process complete'"
            ),
        ]

        logging.info("Launching async command: %s", " ".join(command_to_run))
        pid: int = await bridge.run_shell_command_async(command_to_run)
        logging.info("Async process PID %d started; polling for output", pid)
        await _stream_poll_updates(bridge, pid)

    logging.info("Done.")


def main(
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    tls_insecure: bool = False,
) -> None:
    asyncio.run(run_test(host, port, user, password, tls_insecure))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run an async shell command and stream its output via MQTT polls."
    )
    parser.add_argument("--host", default=None, help="MQTT Broker Host")
    parser.add_argument("--port", type=int, default=None, help="MQTT Broker Port")
    parser.add_argument("--user", default=None, help="MQTT Username")
    parser.add_argument("--password", default=None, help="MQTT Password")
    parser.add_argument(
        "--tls-insecure",
        action="store_true",
        default=False,
        help="Disable TLS certificate verification",
    )
    _args = parser.parse_args()
    main(_args.host, _args.port, _args.user, _args.password, _args.tls_insecure)

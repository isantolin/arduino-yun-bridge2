#!/usr/bin/env python3
"""Example: Run an async shell command and stream its output via periodic polls."""

from __future__ import annotations

import argparse
import asyncio
import logging

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
        poll_payload = await bridge.poll_shell_process(pid)

        # Protobuf poll payloads preserve stdout/stderr as raw bytes.
        raw_stdout = poll_payload.get("stdout_chunk", b"")
        raw_stderr = poll_payload.get("stderr_chunk", b"")

        def safe_decode(b: bytes) -> str:
            try:
                return b.decode("utf-8")
            except UnicodeDecodeError:
                return f"<hex:{b.hex()}>"

        stdout_chunk = safe_decode(raw_stdout).rstrip()
        stderr_chunk = safe_decode(raw_stderr).rstrip()

        exit_code = poll_payload.get("exit_code")
        finished = poll_payload.get("finished", False)

        if stdout_chunk:
            logger.info("[PID %d] STDOUT: %s", pid, stdout_chunk)

        if stderr_chunk:
            logger.info("[PID %d] STDERR: %s", pid, stderr_chunk)

        if finished and not poll_payload.get("stdout_truncated") and not poll_payload.get("stderr_truncated"):
            if not stdout_chunk and not stderr_chunk:
                logger.info(
                    "Process %d completed with exit code %s",
                    pid,
                    exit_code,
                )
            else:
                logger.info(
                    ("Process %d completed with exit code %s (final chunk logged above)"),
                    pid,
                    exit_code,
                )
            break

        if not stdout_chunk and not stderr_chunk:
            await asyncio.sleep(poll_interval)


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as bridge:
        command_to_run: list[str] = [
            "sh",
            "-c",
            ("for i in $(seq 1 4); do echo \"tick:$i\"; sleep 0.5; done; >&2 echo 'process complete'"),
        ]

        logging.info("Launching async command: %s", " ".join(command_to_run))
        pid: int = await bridge.run_shell_command_async(command_to_run)
        logging.info("Async process PID %d started; polling for output", pid)
        await _stream_poll_updates(bridge, pid)

    logging.info("Done.")


def main(
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run an async shell command and stream its output via periodic polls.")
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    _args = parser.parse_args()
    main(_args.socket_path, _args.topic_prefix)

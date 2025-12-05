#!/usr/bin/env python3
"""Example: Run an async shell command and stream its output via MQTT polls."""
import asyncio
import logging
from typing import Any, Dict

from yunbridge_client import Bridge, dump_client_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

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
        poll_payload: Dict[str, Any] = await bridge.poll_shell_process(pid)
        stdout_chunk = (poll_payload.get("stdout") or "").rstrip()
        stderr_chunk = (poll_payload.get("stderr") or "").rstrip()
        exit_code = poll_payload.get("exit_code")
        finished = bool(poll_payload.get("finished"))

        if stdout_chunk:
            logger.info("[PID %d] STDOUT: %s", pid, stdout_chunk)
        elif poll_payload.get("stdout_base64"):
            logger.info(
                "[PID %d] STDOUT (base64, %d bytes)",
                pid,
                len(poll_payload["stdout_base64"]),
            )

        if stderr_chunk:
            logger.info("[PID %d] STDERR: %s", pid, stderr_chunk)
        elif poll_payload.get("stderr_base64"):
            logger.info(
                "[PID %d] STDERR (base64, %d bytes)",
                pid,
                len(poll_payload["stderr_base64"]),
            )

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
                        "Process %d completed with exit code %s "
                        "(final chunk logged above)"
                    ),
                    pid,
                    exit_code,
                )
            break

        if not stdout_chunk and not stderr_chunk:
            await asyncio.sleep(poll_interval)


async def main() -> None:
    """Run main test logic."""
    dump_client_env(logging.getLogger(__name__))
    bridge = Bridge()
    await bridge.connect()

    command_to_run: list[str] = [
        "bash",
        "-c",
        (
            "for i in $(seq 1 4); do "
            "echo \"tick:$i\"; sleep 0.5; "
            "done; >&2 echo 'process complete'"
        ),
    ]

    try:
        logging.info("Launching async command: %s", " ".join(command_to_run))
        pid: int = await bridge.run_shell_command_async(command_to_run)
        logging.info("Async process PID %d started; polling for output", pid)
        await _stream_poll_updates(bridge, pid)
    except Exception as exc:  # pragma: no cover - runtime diagnostics
        logging.error("An error occurred: %s", exc)
    finally:
        await bridge.disconnect()

    logging.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())

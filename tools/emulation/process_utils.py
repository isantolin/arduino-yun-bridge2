"""Canonical process tree termination and supervision utilities (SIL-2)."""

from __future__ import annotations

import socket
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import tenacity

from mcubridge.state.context import terminate_pid_tree

__all__ = ["terminate_process_tree", "terminate_pid_tree", "wait_for_path_ready", "wait_for_tcp_ready"]


def wait_for_path_ready(path: Path | str, timeout: float = 10.0, interval: float = 0.1) -> bool:
    """Wait for a filesystem path (socket, PTY, file) to become available using tenacity."""
    target = Path(path)
    retryer = tenacity.Retrying(
        stop=tenacity.stop_after_delay(timeout),
        wait=tenacity.wait_fixed(interval),
        retry=tenacity.retry_if_result(lambda exists: not exists),
        reraise=False,
    )
    try:
        return retryer(target.exists)
    except tenacity.RetryError as exc:
        sys.stderr.write(f"[DEBUG] Path {path} did not become ready within {timeout}s: {exc}\n")
        return False


def wait_for_tcp_ready(host: str, port: int, timeout: float = 30.0, interval: float = 0.5) -> bool:
    """Wait for a TCP host:port endpoint to accept connections using tenacity."""

    def _probe() -> bool:
        with socket.create_connection((host, port), timeout=1.0):
            return True

    retryer = tenacity.Retrying(
        stop=tenacity.stop_after_delay(timeout),
        wait=tenacity.wait_fixed(interval),
        retry=tenacity.retry_if_exception_type((OSError, ConnectionRefusedError)),
        reraise=False,
    )
    try:
        return retryer(_probe)
    except (tenacity.RetryError, OSError) as exc:
        sys.stderr.write(f"[DEBUG] TCP {host}:{port} did not become ready within {timeout}s: {exc}\n")
        return False


def terminate_process_tree(
    procs: Sequence[subprocess.Popen[Any] | None],
    timeout: float = 3.0,
) -> None:
    """Recursively terminate and clean up process trees via psutil. [SIL-2 / Rule 19 / Rule 31]"""
    for p_handle in procs:
        pid = getattr(p_handle, "pid", None)
        if isinstance(pid, int):
            terminate_pid_tree(pid, timeout=timeout)

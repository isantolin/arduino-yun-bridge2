"""Canonical process tree termination and supervision utilities (SIL-2)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
import socket
from typing import Any

import psutil
import tenacity


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
    except tenacity.RetryError:
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
    except (tenacity.RetryError, OSError):
        return False


def terminate_process_tree(
    procs: Sequence[subprocess.Popen[Any] | None],
    timeout: float = 3.0,
) -> None:
    """Recursively terminate and clean up process trees via psutil. [SIL-2 / Rule 19 / Rule 31]

    Gracefully sends SIGTERM to all child processes and top-level processes, waits
    for bounded duration *timeout*, and escalates lingering processes to SIGKILL.
    """
    for p_handle in procs:
        pid = getattr(p_handle, "pid", None)
        if isinstance(pid, int) and psutil.pid_exists(pid):
            try:
                p = psutil.Process(pid)
                for child in p.children(recursive=True):
                    child.terminate()
                p.terminate()
            except (psutil.NoSuchProcess, ProcessLookupError, psutil.AccessDenied):
                continue

    active_procs: list[psutil.Process] = []
    for p in procs:
        pid = getattr(p, "pid", None)
        if isinstance(pid, int) and psutil.pid_exists(pid):
            try:
                active_procs.append(psutil.Process(pid))
            except (psutil.NoSuchProcess, ProcessLookupError, psutil.AccessDenied):
                continue

    if active_procs:
        _, alive = psutil.wait_procs(active_procs, timeout=timeout)
        for p in alive:
            try:
                p.kill()
            except (psutil.NoSuchProcess, ProcessLookupError, psutil.AccessDenied):
                continue


def terminate_pid_tree(pid: int, timeout: float = 3.0) -> None:
    """Recursively terminate an arbitrary process tree by root PID."""
    if pid <= 0 or not psutil.pid_exists(pid):
        return
    try:
        p = psutil.Process(pid)
        for child in p.children(recursive=True):
            child.terminate()
        p.terminate()
        _, alive = psutil.wait_procs([p], timeout=timeout)
        for lingering in alive:
            lingering.kill()
    except (psutil.NoSuchProcess, ProcessLookupError, psutil.AccessDenied):
        return

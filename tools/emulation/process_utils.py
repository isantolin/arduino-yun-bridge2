"""Canonical process tree termination and supervision utilities (SIL-2)."""

from __future__ import annotations

import io
import json
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import tenacity
from mcubridge.state.context import terminate_pid_tree

__all__ = [
    "start_daemon_thread",
    "stream_pump",
    "terminate_pid_tree",
    "terminate_process_tree",
    "wait_for_path_ready",
    "wait_for_tcp_ready",
    "write_fake_uci_module",
]


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


def start_daemon_thread(target: Callable[..., Any], name: str, *args: Any) -> threading.Thread:
    """Spawn and start a background daemon thread."""
    thread = threading.Thread(target=target, name=name, args=args, daemon=True)
    thread.start()
    return thread


def stream_pump(stream: Any, on_line: Callable[[str, str], None], source: str) -> None:
    """Read lines from a text or binary process stream and dispatch them to callback."""
    if stream is None:
        return
    is_text = isinstance(stream, io.TextIOBase)
    sentinel: str | bytes = "" if is_text else b""
    for raw in iter(stream.readline, sentinel):
        if not raw:
            break
        if isinstance(raw, bytes):
            try:
                line_str = raw.decode("utf-8")
            except UnicodeDecodeError:
                line_str = f"<hex:{raw.hex()}>"
        else:
            line_str = str(raw)
        on_line(line_str, source)


def write_fake_uci_module(base_dir: Path, config: dict[str, str], label: str = "runner") -> Path:
    """Generate fake in-memory OpenWrt uci.py module for isolated testing/emulation."""
    module_path = base_dir / "uci.py"
    module_source = (
        "from __future__ import annotations\n"
        "from typing import Any\n\n"
        f"_CONFIG = {json.dumps(config, sort_keys=True)!r}\n\n"
        "class Uci:\n"
        "    def __enter__(self) -> 'Uci':\n"
        "        return self\n\n"
        "    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:\n"
        "        return False\n\n"
        "    def get_all(self, package: str, section: str | None = None) -> dict[str, str]:\n"
        "        if package != 'mcubridge':\n"
        "            return {}\n"
        "        if section not in (None, 'general'):\n"
        "            return {}\n"
        "        return dict(__import__('json').loads(_CONFIG))\n\n"
        "    def get(self, package: str, section: str, option: str) -> str:\n"
        "        return self.get_all(package, section)[option]\n\n"
        "    def set(self, package: str, section: str, option: str, value: str) -> None:\n"
        f"        raise RuntimeError('fake UCI is read-only in {label}')\n\n"
        "    def commit(self, package: str) -> None:\n"
        "        return None\n\n"
        "class UCI(Uci):\n"
        "    pass\n"
    )
    module_path.write_text(module_source, encoding="utf-8")
    return module_path

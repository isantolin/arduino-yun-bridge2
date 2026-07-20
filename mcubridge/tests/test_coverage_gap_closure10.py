"""Tenth targeted coverage gap closure for runtime.py process IO polling, termination, and SIGKILL paths
to reach 95%+ Python coverage. [SIL-2]"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import ProcessContext, RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport

# ==============================================================================
# Fixtures
# ==============================================================================


def _make_config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/ttyATH0",
        topic_prefix="br",
        allowed_commands=("*",),
        serial_shared_secret=b"secret123456abcd",
        file_system_root=str(tmp_path / "fs"),
        cloud_spool_dir=str(tmp_path / "spool"),
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def cfg(tmp_path: Path) -> RuntimeConfig:
    return _make_config(tmp_path)


@pytest.fixture
def state(cfg: RuntimeConfig) -> Iterator[RuntimeState]:
    s = create_runtime_state(cfg)
    yield s
    s.cleanup()


# ==============================================================================
# runtime.py — Process IO polling, stop & terminate SIGKILL (lines 1226-1287)
# ==============================================================================


@pytest.mark.asyncio
async def test_poll_process_io_read_and_finalize(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_poll_process covers IO reading and process finalization (lines 1226-1256)."""
    service = BridgeService(cfg, state, MagicMock(spec=SerialTransport))

    ctx = ProcessContext.__new__(ProcessContext)
    ctx.io_lock = asyncio.Lock()
    ctx.exit_code = 0

    async def _rd_stdout(*_a: Any, **_k: Any) -> bytes:
        return b"stdout output"

    async def _rd_stderr(*_a: Any, **_k: Any) -> bytes:
        raise TimeoutError()

    mock_stdout = MagicMock()
    mock_stdout.at_eof = MagicMock(return_value=False)
    mock_stdout.read = _rd_stdout

    mock_stderr = MagicMock()
    mock_stderr.at_eof = MagicMock(return_value=False)
    mock_stderr.read = _rd_stderr

    mock_handle = MagicMock()
    mock_handle.stdout = mock_stdout
    mock_handle.stderr = mock_stderr
    mock_handle.returncode = 0

    ctx.handle = mock_handle

    state.running_processes[200] = ctx

    fn_poll = getattr(service, "_poll_process")
    resp = await fn_poll(200)

    assert resp.stdout_data == b"stdout output"
    assert resp.stderr_truncated is True


@pytest.mark.asyncio
async def test_terminate_process_sigkill_escalation(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_terminate_process covers SIGTERM timeout and SIGKILL escalation (lines 1269-1287)."""
    service = BridgeService(cfg, state, MagicMock(spec=SerialTransport))

    async def _wait_timeout() -> int:
        raise TimeoutError()

    mock_handle = MagicMock()
    mock_handle.returncode = None
    mock_handle.pid = 9999
    mock_handle.wait = _wait_timeout

    ctx = ProcessContext.__new__(ProcessContext)
    ctx.handle = mock_handle

    fn_term = getattr(service, "_terminate_process")
    with patch("os.killpg") as mock_killpg:
        code = await fn_term(9999, ctx, grace_period=0.01)
        assert code == -1
        assert mock_killpg.call_count == 2

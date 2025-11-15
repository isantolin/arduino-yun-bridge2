"""Unit tests for async process buffering semantics."""
from __future__ import annotations

import asyncio

import pytest

from yunbridge.config.settings import RuntimeConfig
from yunbridge.services.runtime import BridgeService
from yunbridge.state.context import create_runtime_state
from yunrpc.protocol import MAX_PAYLOAD_SIZE, Status


@pytest.fixture()
def runtime_service() -> BridgeService:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=115200,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="br",
        allowed_commands=[],
        file_system_root="/tmp",
        process_timeout=10,
        reconnect_delay=5,
        status_interval=5,
        debug_logging=False,
        console_queue_limit_bytes=16384,
        mailbox_queue_limit=64,
        mailbox_queue_bytes_limit=65536,
    )
    state = create_runtime_state(config)
    return BridgeService(config, state)


def test_trim_process_buffers_mutates_in_place(runtime_service: BridgeService) -> None:
    stdout = bytearray(b"a" * MAX_PAYLOAD_SIZE)
    stderr = bytearray(b"b" * 10)

    (
        trimmed_stdout,
        trimmed_stderr,
        stdout_truncated,
        stderr_truncated,
    ) = runtime_service._trim_process_buffers(stdout, stderr)  # pyright: ignore[reportPrivateUsage]

    # MAX payload reserves 6 bytes for status/length metadata
    assert len(trimmed_stdout) == MAX_PAYLOAD_SIZE - 6
    assert trimmed_stdout == b"a" * (MAX_PAYLOAD_SIZE - 6)
    # All of stdout but 6 bytes should have been emitted
    assert len(stdout) == 6

    # Stderr cannot emit any data until stdout drained
    assert trimmed_stderr == b""
    assert len(stderr) == 10
    assert stdout_truncated is True
    assert stderr_truncated is True


def test_collect_process_output_flushes_stored_buffers(
    runtime_service: BridgeService,
) -> None:
    async def _run() -> None:
        pid = 42
        state = runtime_service.state
        state.process_exit_codes[pid] = 3
        state.process_stdout_buffer[pid] = bytearray(b"hello")
        state.process_stderr_buffer[pid] = bytearray(b"world")

        (
            status,
            exit_code,
            stdout_chunk,
            stderr_chunk,
            finished,
            stdout_truncated,
            stderr_truncated,
        ) = await runtime_service._collect_process_output(  # pyright: ignore[reportPrivateUsage]
            pid
        )

        assert status == Status.OK.value
        assert exit_code == 3
        assert stdout_chunk == b"hello"
        assert stderr_chunk == b"world"
        assert finished is True
        assert stdout_truncated is False
        assert stderr_truncated is False

        # Buffers and exit codes should be cleaned up after final chunk
        assert pid not in state.process_stdout_buffer
        assert pid not in state.process_stderr_buffer
        assert pid not in state.process_exit_codes
        # Ensure lock remains usable for subsequent consumers
        async with state.process_lock:
            pass

    asyncio.run(_run())

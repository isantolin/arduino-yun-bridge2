"""Unit tests for async process buffering semantics."""

from __future__ import annotations

import asyncio
from types import MethodType
from typing import Any, cast
from collections.abc import Awaitable, Callable

import pytest
from asyncio.subprocess import Process

from mcubridge.config.settings import RuntimeConfig
from mcubridge.const import (
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.services.runtime import BridgeService
from mcubridge.services.components.process import (
    ProcessComponent,
    ProcessOutputBatch,
)
from mcubridge.state.context import ManagedProcess, create_runtime_state
from mcubridge.rpc import protocol
from mcubridge.rpc.protocol import MAX_PAYLOAD_SIZE, Status
from mcubridge.policy import AllowedCommandPolicy


@pytest.fixture()
def runtime_service() -> BridgeService:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=True,
        mqtt_cafile="/tmp/test-ca.pem",
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug_logging=False,
        mailbox_queue_limit=DEFAULT_MAILBOX_QUEUE_LIMIT,
        mailbox_queue_bytes_limit=DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
        serial_shared_secret=b"testshared",
    )
    state = create_runtime_state(config)
    return BridgeService(config, state)


def testtrim_process_buffers_mutates_in_place(
    runtime_service: BridgeService,
) -> None:
    stdout = bytearray(b"a" * MAX_PAYLOAD_SIZE)
    stderr = bytearray(b"b" * 10)

    trim = cast(
        Callable[[bytearray, bytearray], tuple[bytes, bytes, bool, bool]],
        getattr(runtime_service, "trim_process_buffers"),
    )

    (
        trimmed_stdout,
        trimmed_stderr,
        stdout_truncated,
        stderr_truncated,
    ) = trim(
        stdout,
        stderr,
    )

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
        slot = ManagedProcess(pid, "noop", None)
        slot.exit_code = 3
        slot.stdout_buffer.extend(b"hello")
        slot.stderr_buffer.extend(b"world")
        async with state.process_lock:
            state.running_processes[pid] = slot

        collect = cast(
            Callable[[int], Awaitable[ProcessOutputBatch]],
            runtime_service._process.collect_output,
        )

        batch = await collect(pid)

        assert batch.status_byte == Status.OK.value
        assert batch.exit_code == 3
        assert batch.stdout_chunk == b"hello"
        assert batch.stderr_chunk == b"world"
        assert batch.finished is True
        assert batch.stdout_truncated is False
        assert batch.stderr_truncated is False

        # Slot should be removed after final chunk
        assert pid not in state.running_processes
        # Ensure lock remains usable for subsequent consumers
        async with state.process_lock:
            pass

    asyncio.run(_run())


def test_start_async_respects_concurrency_limit(
    runtime_service: BridgeService,
) -> None:
    async def _run() -> None:
        process_component = cast(ProcessComponent, runtime_service._process)
        state = runtime_service.state
        state.allowed_policy = AllowedCommandPolicy.from_iterable(["*"])
        state.process_max_concurrent = 1
        async with state.process_lock:
            state.running_processes[123] = ManagedProcess(
                123,
                "",
                cast(Process, object()),
            )
        result = await process_component.start_async("/bin/true")
        assert result == protocol.INVALID_ID_SENTINEL

    asyncio.run(_run())


def test_handle_run_respects_concurrency_limit(
    runtime_service: BridgeService,
) -> None:
    async def _run() -> None:
        process_component = cast(ProcessComponent, runtime_service._process)
        allowed_policy = AllowedCommandPolicy.from_iterable(["*"])
        runtime_service.state.allowed_policy = allowed_policy
        guard = asyncio.BoundedSemaphore(1)
        await guard.acquire()
        process_component._process_slots = guard

        captured: list[tuple[int, bytes]] = []

        async def _fake_send_frame(self: BridgeService, command_id: int, payload: bytes = b"") -> bool:
            captured.append((command_id, payload))
            return True

        runtime_service_any = cast(Any, runtime_service)
        runtime_service_any.send_frame = MethodType(
            _fake_send_frame,
            runtime_service,
        )

        await process_component.handle_run(b"/bin/true")

        assert captured, "Expected an error frame when slots are exhausted"
        status_id, payload = captured[0]
        assert status_id == Status.ERROR.value
        assert payload == b"process_limit_reached"

        guard.release()

    asyncio.run(_run())


def test_async_process_monitor_releases_slot(
    runtime_service: BridgeService,
) -> None:
    async def _run() -> None:
        process_component = cast(ProcessComponent, runtime_service._process)
        state = runtime_service.state
        process_component._process_slots = asyncio.BoundedSemaphore(1)
        guard = process_component._process_slots
        assert guard is not None
        await guard.acquire()

        class _FakeStream:
            def __init__(self, payload: bytes) -> None:
                self._buffer = bytearray(payload)

            async def read(self, max_bytes: int | None = None) -> bytes:
                if not self._buffer:
                    return b""
                size = len(self._buffer)
                if max_bytes is not None:
                    size = min(size, max_bytes)
                chunk = bytes(self._buffer[:size])
                del self._buffer[:size]
                return chunk

        class _FakeProcess:
            def __init__(self) -> None:
                self.stdout = _FakeStream(b"out")
                self.stderr = _FakeStream(b"err")
                self.returncode: int | None = 5
                self.pid = 9999

            async def wait(self) -> None:
                return None

        fake_proc = _FakeProcess()
        slot = ManagedProcess(
            77,
            "/bin/true",
            cast(Process, fake_proc),
        )
        async with state.process_lock:
            state.running_processes[slot.pid] = slot

        await process_component._monitor_async_process(
            slot.pid,
            cast(Process, fake_proc),
        )

        assert slot.handle is None
        assert slot.exit_code == 5
        assert bytes(slot.stdout_buffer) == b"out"
        assert bytes(slot.stderr_buffer) == b"err"
        await asyncio.wait_for(guard.acquire(), timeout=0.1)
        process_component._release_process_slot()

    asyncio.run(_run())

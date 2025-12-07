"""Unit tests for async process buffering semantics."""
from __future__ import annotations

import asyncio
from types import MethodType
from typing import Any, Awaitable, Callable, Optional, cast

import pytest
from anyio import EndOfStream
from anyio.abc import ByteReceiveStream, Process as AnyioProcess

from yunbridge.config.settings import RuntimeConfig
from yunbridge.const import (
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SERIAL_BAUD,
    DEFAULT_STATUS_INTERVAL,
)
from yunbridge.services.runtime import BridgeService
from yunbridge.services.components.process import ProcessComponent
from yunbridge.state.context import ManagedProcess, create_runtime_state
from yunbridge.rpc.protocol import MAX_PAYLOAD_SIZE, Status
from yunbridge.policy import AllowedCommandPolicy


@pytest.fixture()
def runtime_service() -> BridgeService:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=DEFAULT_SERIAL_BAUD,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=True,
        mqtt_cafile="/tmp/test-ca.pem",
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=DEFAULT_MQTT_TOPIC,
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug_logging=False,
        console_queue_limit_bytes=DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
        mailbox_queue_limit=DEFAULT_MAILBOX_QUEUE_LIMIT,
        mailbox_queue_bytes_limit=DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
        serial_shared_secret=b"testshared",
    )
    state = create_runtime_state(config)
    return BridgeService(config, state)


def test_trim_process_buffers_mutates_in_place(
    runtime_service: BridgeService,
) -> None:
    stdout = bytearray(b"a" * MAX_PAYLOAD_SIZE)
    stderr = bytearray(b"b" * 10)

    trim = cast(
        Callable[[bytearray, bytearray], tuple[bytes, bytes, bool, bool]],
        getattr(runtime_service, "_trim_process_buffers"),
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
            Callable[[int], Awaitable[
                tuple[int, int, bytes, bytes, bool, bool, bool]
            ]],
            getattr(runtime_service, "_collect_process_output"),
        )

        (
            status,
            exit_code,
            stdout_chunk,
            stderr_chunk,
            finished,
            stdout_truncated,
            stderr_truncated,
        ) = await collect(pid)

        assert status == Status.OK.value
        assert exit_code == 3
        assert stdout_chunk == b"hello"
        assert stderr_chunk == b"world"
        assert finished is True
        assert stdout_truncated is False
        assert stderr_truncated is False

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
                cast(AnyioProcess, object()),
            )
        result = await process_component.start_async("/bin/true")
        assert result == 0xFFFF

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

        async def _fake_send_frame(
            self: BridgeService, command_id: int, payload: bytes = b""
        ) -> bool:
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

        class _FakeStream(ByteReceiveStream):
            def __init__(self, payload: bytes) -> None:
                self._buffer = bytearray(payload)
                self._closed = False

            async def receive(self, max_bytes: Optional[int] = None) -> bytes:
                if self._closed:
                    raise EndOfStream
                if not self._buffer:
                    self._closed = True
                    raise EndOfStream
                size = len(self._buffer)
                if max_bytes is not None:
                    size = min(size, max_bytes)
                chunk = bytes(self._buffer[:size])
                del self._buffer[:size]
                if not self._buffer:
                    self._closed = True
                return chunk

            async def aclose(self) -> None:
                self._closed = True
                self._buffer.clear()

        class _FakeProcess:
            def __init__(self) -> None:
                self.stdout: ByteReceiveStream = _FakeStream(b"out")
                self.stderr: ByteReceiveStream = _FakeStream(b"err")
                self.returncode: Optional[int] = 5
                self.pid = 9999

            async def wait(self) -> None:
                return None

        fake_proc = _FakeProcess()
        slot = ManagedProcess(
            77,
            "/bin/true",
            cast(AnyioProcess, fake_proc),
        )
        async with state.process_lock:
            state.running_processes[slot.pid] = slot

        await process_component._monitor_async_process(
            slot.pid,
            cast(AnyioProcess, fake_proc),
        )

        assert slot.handle is None
        assert slot.exit_code == 5
        assert bytes(slot.stdout_buffer) == b"out"
        assert bytes(slot.stderr_buffer) == b"err"
        await asyncio.wait_for(guard.acquire(), timeout=0.1)
        process_component._release_process_slot()

    asyncio.run(_run())

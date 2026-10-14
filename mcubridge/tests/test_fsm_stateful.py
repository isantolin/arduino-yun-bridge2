"""SIL-2 stateful property testing for deterministic finite state machines.

Formally tests state invariants across:
1. LinkConnectionMachine (serial physical connection & synchronization)
2. ProcessMachine (asynchronous subprocess lifecycle)
3. CloudLinkMachine (cloud gateway transport & degradation)
4. FileTransferMachine (chunk streaming protocol transfer)
5. HandshakeMachine (serial link handshake & synchronization state)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
import time
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from google.protobuf.message import Message as ProtobufMessage
import hypothesis.stateful as h_stateful
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule
import pytest
import tenacity

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command, Status
from mcubridge.services.handshake import (
    HandshakeEvent,
    HandshakeMachine,
    HandshakeState,
    RateLimiter,
    SerialHandshakeManager,
    derive_serial_timing,
)
from mcubridge.state.context import (
    CloudLinkMachine,
    CloudLinkState,
    FileTransferMachine,
    FileTransferState,
    LinkConnectionState,
    ProcessContext,
    ProcessState,
    RuntimeState,
    create_runtime_state,
)

run_state_machine_as_test: Callable[[type[RuleBasedStateMachine]], None] = cast(
    Any, h_stateful
).run_state_machine_as_test


class LinkConnectionStateMachine(RuleBasedStateMachine):
    """Formal model verification for serial LinkConnectionMachine invariants."""

    def __init__(self) -> None:
        super().__init__()
        self.config = RuntimeConfig(
            serial_shared_secret=b"test_secret_1234",
            allow_non_tmp_paths=True,
        )
        self.state = create_runtime_state(self.config)

    def teardown(self) -> None:
        self.state.cleanup()
        super().teardown()

    @rule()
    def connect(self) -> None:
        self.state.connection_fsm.connect()
        assert self.state.connection_fsm.connected.is_active
        assert not self.state.is_synchronized

    @rule()
    def synchronize(self) -> None:
        self.state.connection_fsm.synchronize()
        assert self.state.connection_fsm.synchronized.is_active
        assert self.state.is_synchronized

    @rule()
    def disconnect(self) -> None:
        self.state.connection_fsm.disconnect()
        assert self.state.connection_fsm.disconnected.is_active
        assert not self.state.is_synchronized

    @invariant()
    def verify_link_invariants(self) -> None:
        current: str = self.state.state
        assert current in (
            LinkConnectionState.DISCONNECTED.value,
            LinkConnectionState.CONNECTED.value,
            LinkConnectionState.SYNCHRONIZED.value,
        )
        # SIL-2 Critical Invariant: is_synchronized MUST strictly track synchronized state
        assert (current == LinkConnectionState.SYNCHRONIZED.value) == self.state.is_synchronized
        # Metric state must match FSM state synchronously
        assert self.state.metrics.link_state.value == current


class ProcessLifecycleStateMachine(RuleBasedStateMachine):
    """Formal model verification for asynchronous subprocess lifecycle machine."""

    processes = Bundle("processes")

    def __init__(self) -> None:
        super().__init__()
        self.active_contexts: dict[int, ProcessContext] = {}
        self.pid_seq = 1000

    @rule(target=processes)
    def spawn_process(self) -> int:
        self.pid_seq += 1
        pid = self.pid_seq
        mock_proc: Any = MagicMock(spec=asyncio.subprocess.Process)
        mock_proc.pid = pid
        ctx = ProcessContext(mock_proc)
        self.active_contexts[pid] = ctx
        return pid

    @rule(pid=processes)
    def start(self, pid: int) -> None:
        ctx = self.active_contexts[pid]
        if ctx.fsm.spawning.is_active:
            ctx.fsm.start()
            assert ctx.fsm.running.is_active
            assert ctx.status == ProcessState.RUNNING.value

    @rule(pid=processes)
    def terminate(self, pid: int) -> None:
        ctx = self.active_contexts[pid]
        if ctx.fsm.running.is_active:
            ctx.fsm.terminate()
            assert ctx.fsm.terminating.is_active
            assert ctx.status == ProcessState.TERMINATING.value

    @rule(pid=processes, exit_code=st.integers(min_value=0, max_value=255))
    def finish(self, pid: int, exit_code: int) -> None:
        ctx = self.active_contexts[pid]
        if not ctx.fsm.exited.is_active:
            ctx.exit_code = exit_code
            ctx.fsm.finish()
            assert ctx.fsm.exited.is_active
            assert ctx.status == ProcessState.EXITED.value

    @invariant()
    def verify_process_invariants(self) -> None:
        for pid, ctx in self.active_contexts.items():
            assert pid > 0
            assert ctx.status in (
                ProcessState.SPAWNING.value,
                ProcessState.RUNNING.value,
                ProcessState.TERMINATING.value,
                ProcessState.EXITED.value,
            )


class CloudLinkStateMachine(RuleBasedStateMachine):
    """Formal model verification for Cloud Gateway connection & degradation."""

    def __init__(self) -> None:
        super().__init__()
        self.fsm = CloudLinkMachine()

    @rule()
    def start_connecting(self) -> None:
        prev = self.fsm.current_state_value
        can_trans = (
            self.fsm.disabled.is_active
            or self.fsm.reconnecting.is_active
            or self.fsm.spooling_degraded.is_active
            or self.fsm.connecting.is_active
        )
        self.fsm.start_connecting()
        if can_trans:
            assert self.fsm.connecting.is_active
        else:
            assert self.fsm.current_state_value == prev

    @rule()
    def connect_http3(self) -> None:
        prev = self.fsm.current_state_value
        can_trans = (
            self.fsm.connecting.is_active
            or self.fsm.reconnecting.is_active
            or self.fsm.spooling_degraded.is_active
            or self.fsm.connected_http3.is_active
        )
        self.fsm.connect_http3()
        if can_trans:
            assert self.fsm.connected_http3.is_active
        else:
            assert self.fsm.current_state_value == prev

    @rule()
    def connect_http2(self) -> None:
        prev = self.fsm.current_state_value
        can_trans = (
            self.fsm.connecting.is_active
            or self.fsm.reconnecting.is_active
            or self.fsm.spooling_degraded.is_active
            or self.fsm.connected_http2.is_active
        )
        self.fsm.connect_http2()
        if can_trans:
            assert self.fsm.connected_http2.is_active
        else:
            assert self.fsm.current_state_value == prev

    @rule()
    def degrade(self) -> None:
        prev = self.fsm.current_state_value
        can_trans = not self.fsm.disabled.is_active
        self.fsm.degrade()
        if can_trans:
            assert self.fsm.spooling_degraded.is_active
        else:
            assert self.fsm.current_state_value == prev

    @rule()
    def start_reconnect(self) -> None:
        prev = self.fsm.current_state_value
        can_trans = (
            self.fsm.spooling_degraded.is_active or self.fsm.connecting.is_active or self.fsm.reconnecting.is_active
        )
        self.fsm.start_reconnect()
        if can_trans:
            assert self.fsm.reconnecting.is_active
        else:
            assert self.fsm.current_state_value == prev

    @rule()
    def disable(self) -> None:
        self.fsm.disable()
        assert self.fsm.disabled.is_active

    @invariant()
    def verify_cloud_invariants(self) -> None:
        current: str = str(self.fsm.current_state_value)
        assert current in (
            CloudLinkState.DISABLED.value,
            CloudLinkState.CONNECTING.value,
            CloudLinkState.CONNECTED_HTTP3.value,
            CloudLinkState.CONNECTED_HTTP2.value,
            CloudLinkState.SPOOLING_DEGRADED.value,
            CloudLinkState.RECONNECTING.value,
        )


class FileTransferStateMachine(RuleBasedStateMachine):
    """Formal model verification for chunk streaming file transfer machine."""

    def __init__(self) -> None:
        super().__init__()
        self.fsm = FileTransferMachine()

    @rule()
    def start_transfer(self) -> None:
        self.fsm.start_transfer()
        assert self.fsm.transferring.is_active

    @rule()
    def receive_chunk(self) -> None:
        prev = self.fsm.current_state_value
        can_trans = self.fsm.transferring.is_active
        self.fsm.receive_chunk()
        if can_trans:
            assert self.fsm.transferring.is_active
        else:
            assert self.fsm.current_state_value == prev

    @rule()
    def complete(self) -> None:
        prev = self.fsm.current_state_value
        can_trans = self.fsm.transferring.is_active or self.fsm.completed.is_active
        self.fsm.complete()
        if can_trans:
            assert self.fsm.completed.is_active
        else:
            assert self.fsm.current_state_value == prev

    @rule()
    def timeout(self) -> None:
        prev = self.fsm.current_state_value
        can_trans = self.fsm.transferring.is_active or self.fsm.timed_out.is_active
        self.fsm.timeout()
        if can_trans:
            assert self.fsm.timed_out.is_active
        else:
            assert self.fsm.current_state_value == prev

    @rule()
    def abort(self) -> None:
        prev = self.fsm.current_state_value
        can_trans = self.fsm.transferring.is_active or self.fsm.idle.is_active or self.fsm.aborted.is_active
        self.fsm.abort()
        if can_trans:
            assert self.fsm.aborted.is_active
        else:
            assert self.fsm.current_state_value == prev

    @rule()
    def reset(self) -> None:
        prev = self.fsm.current_state_value
        can_trans = not self.fsm.transferring.is_active
        self.fsm.reset()
        if can_trans:
            assert self.fsm.idle.is_active
        else:
            assert self.fsm.current_state_value == prev

    @invariant()
    def verify_transfer_invariants(self) -> None:
        current: str = str(self.fsm.current_state_value)
        assert current in (
            FileTransferState.IDLE.value,
            FileTransferState.TRANSFERRING.value,
            FileTransferState.COMPLETED.value,
            FileTransferState.TIMED_OUT.value,
            FileTransferState.ABORTED.value,
        )


class HandshakeStateMachine(RuleBasedStateMachine):
    """Formal model verification for serial HandshakeMachine invariants."""

    def __init__(self) -> None:
        super().__init__()
        self.fsm = HandshakeMachine()

    @rule()
    def start_sync(self) -> None:
        if (
            self.fsm.unsynchronized.is_active
            or self.fsm.synchronized.is_active
            or self.fsm.fault.is_active
        ):
            self.fsm.start_sync()
            assert self.fsm.resetting.is_active

    @rule()
    def reset_sent(self) -> None:
        if self.fsm.resetting.is_active:
            self.fsm.reset_sent()
            assert self.fsm.syncing.is_active

    @rule()
    def sync_sent(self) -> None:
        if self.fsm.syncing.is_active:
            self.fsm.sync_sent()
            assert self.fsm.confirming.is_active

    @rule()
    def sync_confirmed(self) -> None:
        if self.fsm.confirming.is_active or self.fsm.syncing.is_active:
            self.fsm.sync_confirmed()
            assert self.fsm.synchronized.is_active

    @rule()
    def failure(self) -> None:
        self.fsm.failure()
        assert self.fsm.fault.is_active

    @rule()
    def reset(self) -> None:
        self.fsm.reset()
        assert self.fsm.unsynchronized.is_active

    @invariant()
    def verify_handshake_invariants(self) -> None:
        current: str = str(self.fsm.current_state_value)
        assert current in (
            HandshakeState.UNSYNCHRONIZED.value,
            HandshakeState.RESETTING.value,
            HandshakeState.SYNCING.value,
            HandshakeState.CONFIRMING.value,
            HandshakeState.SYNCHRONIZED.value,
            HandshakeState.FAULT.value,
        )


def test_link_connection_state_machine() -> None:
    """Execute Hypothesis RuleBasedStateMachine on LinkConnectionMachine."""
    run_state_machine_as_test(LinkConnectionStateMachine)


def test_process_lifecycle_state_machine() -> None:
    """Execute Hypothesis RuleBasedStateMachine on ProcessMachine."""
    run_state_machine_as_test(ProcessLifecycleStateMachine)


def test_cloud_link_state_machine() -> None:
    """Execute Hypothesis RuleBasedStateMachine on CloudLinkMachine."""
    run_state_machine_as_test(CloudLinkStateMachine)


def test_file_transfer_state_machine() -> None:
    """Execute Hypothesis RuleBasedStateMachine on FileTransferMachine."""
    run_state_machine_as_test(FileTransferStateMachine)


def test_handshake_state_machine() -> None:
    """Execute Hypothesis RuleBasedStateMachine on HandshakeMachine."""
    run_state_machine_as_test(HandshakeStateMachine)


@pytest.fixture
def handshake_setup(
    tmp_path: Path,
) -> Iterator[tuple[SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock]]:
    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/test",
        serial_shared_secret=b"secure_secret_123456789012345678",
        serial_handshake_fatal_failures=3,
        file_system_root=str(tmp_path),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)

    send_frame = AsyncMock(return_value=True)
    enqueue_cloud = AsyncMock()
    acknowledge_frame = AsyncMock()
    timing = derive_serial_timing(config)

    manager = SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=timing,
        send_frame=send_frame,
        enqueue_cloud=enqueue_cloud,
        acknowledge_frame=acknowledge_frame,
    )
    yield manager, state, send_frame, config, timing, acknowledge_frame
    state.cleanup()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_handshake_auth_mismatch(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    """Verify rejection of invalid HMAC tags during sync."""
    manager, state, _, _config, _timing, _ack = handshake_setup

    # Start sync to set expectations — MUST be cancelled to avoid dangling
    # tenacity retries (up to 3 × 5s timeout) that hang xdist workers.
    sync_task = asyncio.create_task(manager.synchronize())
    await asyncio.sleep(0.2)  # Let it send RESET and SYNC

    nonce = state.link_handshake_nonce
    assert nonce is not None

    # Simulate MCU response with WRONG tag
    bad_tag = b"F" * 16
    payload = pb.LinkSync(nonce=nonce, tag=bad_tag).SerializeToString()

    result = await manager.handle_link_sync_resp(1, payload)
    assert not result
    assert state.handshake_failure_streak == 1
    assert state.last_handshake_error == "sync_auth_mismatch"

    sync_task.cancel()
    with pytest.raises((asyncio.CancelledError, tenacity.RetryError)):
        await sync_task


@pytest.mark.asyncio
async def test_handshake_rate_limiting(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    """Verify handshake rate limiting protects MCU from thrashing."""
    manager, state, _, config, _timing, _ack = handshake_setup
    config.serial_handshake_min_interval = 1.0

    state.connection_fsm.synchronize()
    state.link_handshake_nonce = b"pending"
    state.handshake_rate_until = time.monotonic() + 0.5

    # Try to process response while rate limited
    result = await manager.handle_link_sync_resp(1, b"")
    assert not result
    assert state.last_handshake_error == "sync_rate_limited"


@pytest.mark.asyncio
async def test_handshake_fatal_threshold(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    """Verify transition to permanent failure after threshold is reached."""
    manager, state, _, _config, _timing, _ack = handshake_setup

    # sync_auth_mismatch is an immediate fatal reason, so it increments every time
    for _ in range(3):
        await manager.handle_handshake_failure("sync_auth_mismatch")

    assert state.handshake_fatal_count == 3
    assert state.handshake_fatal_reason is not None


@pytest.mark.asyncio
async def test_handshake_streak_fatal_threshold(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    """Verify transition to permanent failure after streak threshold is reached for non-immediate errors."""
    manager, state, _, _config, _timing, _ack = handshake_setup

    # non-immediate reason
    reason = "link_reset_send_failed"
    for _ in range(2):
        await manager.handle_handshake_failure(reason)
        assert state.handshake_fatal_count == 0

    # 3rd failure hits the threshold (3)
    await manager.handle_handshake_failure(reason)
    assert state.handshake_fatal_count == 1


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_handshake_capabilities_retry(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    """Verify capabilities discovery retries on timeout then succeeds with valid payload."""
    manager, _state, send_frame, _config, timing, _ack = handshake_setup

    manager.retry_backoff_base = 0.001
    manager.retry_backoff_max = 0.005
    timing.response_timeout_ms = 10

    attempts = 0

    async def mock_send_frame(cmd: int, *args: Any, **kwargs: Any) -> bool:
        nonlocal attempts
        attempts += 1
        fut: Any = manager.capabilities_future
        if attempts < 3:
            if fut is not None and not fut.done():
                fut.set_exception(TimeoutError("mock timeout"))
        elif attempts == 3:
            # [SIL-2] Use valid empty Capabilities payload (serialized empty protobuf)
            asyncio.create_task(manager.handle_capabilities_resp(1, pb.Capabilities().SerializeToString()))
        return True

    send_frame.side_effect = mock_send_frame

    fetch_caps: Callable[[], Awaitable[bool]] = manager.fetch_capabilities
    result = await fetch_caps()
    assert result
    assert send_frame.call_count == 3


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_handshake_capabilities_corrupt_payload(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    """Verify capabilities discovery returns False immediately on corrupt payload. [SIL-2]"""
    manager, _state, send_frame, _config, timing, _ack = handshake_setup

    timing.response_timeout_ms = 200

    async def mock_send_frame_corrupt(cmd: int, *args: Any, **kwargs: Any) -> bool:
        fut: Any = manager.capabilities_future
        if fut is not None and not fut.done():
            asyncio.create_task(manager.handle_capabilities_resp(1, b"\x80"))
        return True

    send_frame.side_effect = mock_send_frame_corrupt

    fetch_caps_corrupt: Callable[[], Awaitable[bool]] = manager.fetch_capabilities
    result = await fetch_caps_corrupt()
    assert not result


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_handshake_malformed_sync_resp(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    """Verify handling of corrupt protobuf in sync response."""
    manager, state, _, _config, _timing, _ack = handshake_setup
    state.link_handshake_nonce = b"pending"

    result = await manager.handle_link_sync_resp(1, b"\xff\xff\xff")  # Invalid protobuf
    assert not result
    assert state.last_handshake_error == "sync_decode_failed"
    handshake_setup[-1].assert_called_with(Command.CMD_LINK_SYNC_RESP.value, 1, status=Status.MALFORMED)


@pytest.mark.asyncio
async def test_wait_for_link_sync_confirmation_timeout(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    manager, state, _, _config, _timing, _ack = handshake_setup
    _timing.response_timeout_ms = 1
    state.connection_fsm.disconnect()
    wait_sync: Callable[[bytes], Awaitable[bool]] = manager.wait_for_link_sync_confirmation
    res = await wait_sync(b"test_nonce")
    assert res is False


@pytest.mark.asyncio
async def test_handshake_attempt_link_sync_timeout(
    handshake_setup: tuple[
        SerialHandshakeManager, RuntimeState, AsyncMock, RuntimeConfig, pb.HandshakeConfig, AsyncMock
    ],
) -> None:
    manager, _state, _, _config, _timing, _ack = handshake_setup
    manager.sync_waiter = AsyncMock(return_value=False)
    sync_attempt: Callable[[], Awaitable[bool]] = manager.synchronize_attempt
    res = await sync_attempt()
    assert res is False
    assert _state.last_handshake_error == "link_sync_timeout"


def test_handshake_calculate_tag_empty_secret() -> None:
    assert SerialHandshakeManager.calculate_handshake_tag(None, b"test_nonce") == b""
    assert SerialHandshakeManager.calculate_handshake_tag(b"", b"test_nonce") == b""


@given(secret=st.binary(min_size=16, max_size=32), nonce=st.binary(min_size=12, max_size=12))
def test_handshake_calculate_tag_deterministic_property(secret: bytes, nonce: bytes) -> None:
    tag1 = SerialHandshakeManager.calculate_handshake_tag(secret, nonce)
    tag2 = SerialHandshakeManager.calculate_handshake_tag(secret, nonce)
    assert tag1 == tag2
    assert len(tag1) == 16


@pytest.mark.asyncio
async def test_handshake_wait_confirmation_already_synchronized(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    hs = SerialHandshakeManager(
        config=runtime_config,
        state=runtime_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )
    runtime_state.connection_fsm.synchronize()

    wait_sync: Callable[[bytes], Awaitable[bool]] = hs.wait_for_link_sync_confirmation
    confirmed = await wait_sync(b"nonce")
    assert confirmed is True


@pytest.mark.asyncio
async def test_handshake_sync_state_permutations(runtime_config: RuntimeConfig, runtime_state: RuntimeState) -> None:
    runtime_state.connection_fsm.disconnect()
    mock_send = AsyncMock(return_value=True)
    hs = SerialHandshakeManager(
        config=runtime_config,
        state=runtime_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=mock_send,
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )
    sync_attempt: Callable[[], Awaitable[bool]] = hs.synchronize_attempt

    async def _send_and_fault(command_id: int, payload: bytes | ProtobufMessage, seq_id: int | None = None) -> bool:
        if command_id == Command.CMD_LINK_SYNC.value:
            hs.fsm_state = HandshakeState.FAULT
        return True

    hs.send_frame = _send_and_fault
    assert await sync_attempt() is False

    hs.fsm_state = HandshakeState.SYNCING

    async def _mock_wait_fault(nonce: bytes) -> bool:
        hs.fsm_state = HandshakeState.FAULT
        return False

    hs.sync_waiter = _mock_wait_fault
    assert await sync_attempt() is False

    hs.sync_waiter = AsyncMock(return_value=False)
    hs.fsm_state = HandshakeState.SYNCING
    runtime_state.link_handshake_nonce = b"different_nonce"
    assert await sync_attempt() is False

    hs.transition(HandshakeEvent.RESET)
    hs.send_frame = AsyncMock(return_value=True)
    hs.sync_waiter = AsyncMock(return_value=True)
    assert await sync_attempt() is True


@pytest.mark.asyncio
async def test_handshake_resp_rate_limit_and_secret_none(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    runtime_config.serial_handshake_min_interval = 5.0
    hs = SerialHandshakeManager(
        config=runtime_config,
        state=runtime_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    runtime_state.link_handshake_nonce = b"expected_nonce_12b"
    runtime_state.handshake_rate_until = time.monotonic() + 10.0
    pkt = pb.LinkSync(nonce=b"expected_nonce_12b", tag=b"tag")
    assert await hs.handle_link_sync_resp(1, pkt) is False

    runtime_config.serial_handshake_min_interval = 0.0
    runtime_config.serial_shared_secret = b""
    runtime_state.handshake_rate_until = 0.0
    nonce = b"expected_12b_nonce"
    runtime_state.link_handshake_nonce = nonce
    tag = hs.calculate_handshake_tag(b"", nonce)
    runtime_state.link_expected_tag = tag
    pkt_matching = pb.LinkSync(nonce=nonce, tag=tag)
    hs.capabilities_delay = 100.0
    assert await hs.handle_link_sync_resp(2, pkt_matching) is True
    assert runtime_state.is_synchronized is True
    assert runtime_state.link_session_key is None


@pytest.mark.asyncio
async def test_handshake_capabilities_resp_future_none(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    hs = SerialHandshakeManager(
        config=runtime_config,
        state=runtime_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )
    hs.capabilities_future = None
    assert await hs.handle_capabilities_resp(1, b"") is True


@pytest.mark.asyncio
async def test_handshake_fsm_state_override_and_unexpected_resp(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    mock_cloud = AsyncMock()
    hs = SerialHandshakeManager(
        config=runtime_config,
        state=runtime_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=mock_cloud,
        acknowledge_frame=AsyncMock(),
    )

    set_fsm_state: Callable[[HandshakeState], None] = hs.set_fsm_state
    set_fsm_state(HandshakeState.SYNCHRONIZED)
    assert hs.fsm_state == HandshakeState.SYNCHRONIZED
    set_fsm_state(HandshakeState.SYNCHRONIZED)  # no-op same state
    set_fsm_state(HandshakeState.UNSYNCHRONIZED)
    assert hs.fsm_state == HandshakeState.UNSYNCHRONIZED

    runtime_state.link_handshake_nonce = None
    res = await hs.handle_link_sync_resp(1, pb.LinkSync(nonce=b"none", tag=b"tag"))
    assert res is False
    assert runtime_state.last_handshake_error == "unexpected_sync_resp"

    def _empty_resolver(_prefix: str, _msg: object) -> str:
        return ""

    mock_cloud.reset_mock()
    publish_event: Callable[..., Awaitable[None]] = hs.publish_handshake_event
    hs.topic_resolver = _empty_resolver
    await publish_event("test_event", reason="test_err")
    assert mock_cloud.await_count == 0


@pytest.mark.asyncio
async def test_handshake_rate_limit_and_sync_fault_branches(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    hs = SerialHandshakeManager(
        config=runtime_config,
        state=runtime_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    # 1. check_rate_limit when now >= limit_until (line 121)
    allowed, remaining = RateLimiter.check_rate_limit(10.0, 5.0)
    assert allowed is True
    assert remaining == 0.0

    # 2. _synchronize_attempt() when confirmed is False and current_state == HandshakeState.FAULT (line 328)
    set_fsm_state: Callable[[HandshakeState], None] = hs.set_fsm_state
    sync_fn: Callable[[], Awaitable[bool]] = hs.synchronize_attempt

    async def _mock_wait_fault(_nonce: bytes) -> bool:
        hs.transition(HandshakeEvent.FAILURE)
        return False

    hs.sync_waiter = _mock_wait_fault
    set_fsm_state(HandshakeState.UNSYNCHRONIZED)
    res_fault = await sync_fn()
    assert res_fault is False

    # 3. _synchronize_attempt() when pending_nonce != nonce (line 332->334)
    async def _mock_wait_mismatch(_nonce: bytes) -> bool:
        runtime_state.link_handshake_nonce = None
        return False

    hs.sync_waiter = _mock_wait_mismatch
    set_fsm_state(HandshakeState.UNSYNCHRONIZED)
    res_mismatch = await sync_fn()
    assert res_mismatch is False


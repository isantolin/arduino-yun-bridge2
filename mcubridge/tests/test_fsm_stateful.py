"""SIL-2 stateful property testing for deterministic finite state machines.

Formally tests state invariants across:
1. LinkConnectionMachine (serial physical connection & synchronization)
2. ProcessMachine (asynchronous subprocess lifecycle)
3. CloudLinkMachine (cloud gateway transport & degradation)
4. FileTransferMachine (chunk streaming protocol transfer)
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import MagicMock

import hypothesis.stateful as h_stateful
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule

from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import (
    CloudLinkMachine,
    CloudLinkState,
    FileTransferMachine,
    FileTransferState,
    LinkConnectionState,
    ProcessContext,
    ProcessState,
    create_runtime_state,
)

_RUN_STATE_MACHINE: Callable[[type[RuleBasedStateMachine]], None] = cast(
    Callable[[type[RuleBasedStateMachine]], None],
    getattr(h_stateful, "run_state_machine_as_test"),
)


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
            assert ctx.is_running
            assert ctx.status == ProcessState.RUNNING.value

    @rule(pid=processes)
    def terminate(self, pid: int) -> None:
        ctx = self.active_contexts[pid]
        if ctx.fsm.running.is_active:
            ctx.fsm.terminate()
            assert ctx.is_terminating
            assert ctx.status == ProcessState.TERMINATING.value

    @rule(pid=processes, exit_code=st.integers(min_value=0, max_value=255))
    def finish(self, pid: int, exit_code: int) -> None:
        ctx = self.active_contexts[pid]
        if not ctx.is_exited:
            ctx.exit_code = exit_code
            ctx.fsm.finish()
            assert ctx.is_exited
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
            assert ctx.is_running == ctx.fsm.running.is_active
            assert ctx.is_terminating == ctx.fsm.terminating.is_active
            assert ctx.is_exited == ctx.fsm.exited.is_active


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


def test_link_connection_state_machine() -> None:
    """Execute Hypothesis RuleBasedStateMachine on LinkConnectionMachine."""
    _RUN_STATE_MACHINE(LinkConnectionStateMachine)


def test_process_lifecycle_state_machine() -> None:
    """Execute Hypothesis RuleBasedStateMachine on ProcessMachine."""
    _RUN_STATE_MACHINE(ProcessLifecycleStateMachine)


def test_cloud_link_state_machine() -> None:
    """Execute Hypothesis RuleBasedStateMachine on CloudLinkMachine."""
    _RUN_STATE_MACHINE(CloudLinkStateMachine)


def test_file_transfer_state_machine() -> None:
    """Execute Hypothesis RuleBasedStateMachine on FileTransferMachine."""
    _RUN_STATE_MACHINE(FileTransferStateMachine)

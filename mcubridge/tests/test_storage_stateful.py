"""SIL-2 stateful property testing for LMDB persistent storage and queues."""

from __future__ import annotations

import asyncio
import collections
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from hypothesis import strategies as st
import hypothesis.stateful as h_stateful
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule
import pytest

from mcubridge.state.storage import LmdbDeque

_RUN_STATE_MACHINE: Callable[[type[RuleBasedStateMachine]], None] = cast(
    Callable[[type[RuleBasedStateMachine]], None],
    getattr(h_stateful, "run_state_machine_as_test"),
)


class LmdbDequeDiskStateMachine(RuleBasedStateMachine):
    """Stateful model verification for persistent on-disk LmdbDeque."""

    items = Bundle("items")

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.temp_dir = TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "stateful_deque")
        self.deque = LmdbDeque(self.db_path, maxlen=15)
        self.model: collections.deque[bytes] = collections.deque(maxlen=15)

    def teardown(self) -> None:
        self.loop.run_until_complete(self.deque.close())
        self.loop.close()
        self.temp_dir.cleanup()
        super().teardown()

    @rule(target=items, data=st.binary(min_size=1, max_size=256))
    def push(self, data: bytes) -> bytes:
        self.loop.run_until_complete(self.deque.append(data))
        self.model.append(data)
        assert len(self.deque) == len(self.model)
        return data

    @rule()
    def pop(self) -> None:
        if not self.model:
            with pytest.raises(IndexError):
                self.loop.run_until_complete(self.deque.popleft())
        else:
            expected = self.model.popleft()
            actual = self.loop.run_until_complete(self.deque.popleft())
            assert actual == expected

    @rule()
    def peek(self) -> None:
        if not self.model:
            with pytest.raises(IndexError):
                self.loop.run_until_complete(self.deque.peek())
        else:
            expected = self.model[0]
            actual = self.loop.run_until_complete(self.deque.peek())
            assert actual == expected

    @rule()
    def clear(self) -> None:
        self.loop.run_until_complete(self.deque.clear())
        self.model.clear()
        assert len(self.deque) == 0

    @rule()
    def vacuum(self) -> None:
        self.loop.run_until_complete(self.deque.vacuum())
        assert len(self.deque) == len(self.model)

    @invariant()
    def invariants(self) -> None:
        assert len(self.deque) == len(self.model)
        assert len(self.deque) <= 15


class LmdbDequeMemoryStateMachine(RuleBasedStateMachine):
    """Stateful model verification for in-memory LmdbDeque."""

    items = Bundle("items")

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.deque = LmdbDeque(":memory:", maxlen=15)
        self.model: collections.deque[bytes] = collections.deque(maxlen=15)

    def teardown(self) -> None:
        self.loop.run_until_complete(self.deque.close())
        self.loop.close()
        super().teardown()

    @rule(target=items, data=st.binary(min_size=1, max_size=256))
    def push(self, data: bytes) -> bytes:
        self.loop.run_until_complete(self.deque.append(data))
        self.model.append(data)
        assert len(self.deque) == len(self.model)
        return data

    @rule()
    def pop(self) -> None:
        if not self.model:
            with pytest.raises(IndexError):
                self.loop.run_until_complete(self.deque.popleft())
        else:
            expected = self.model.popleft()
            actual = self.loop.run_until_complete(self.deque.popleft())
            assert actual == expected

    @rule()
    def peek(self) -> None:
        if not self.model:
            with pytest.raises(IndexError):
                self.loop.run_until_complete(self.deque.peek())
        else:
            expected = self.model[0]
            actual = self.loop.run_until_complete(self.deque.peek())
            assert actual == expected

    @rule()
    def clear(self) -> None:
        self.loop.run_until_complete(self.deque.clear())
        self.model.clear()
        assert len(self.deque) == 0

    @invariant()
    def invariants(self) -> None:
        assert len(self.deque) == len(self.model)
        assert len(self.deque) <= 15


def test_lmdb_deque_disk_state_machine() -> None:
    """Run stateful property tests for disk-backed LmdbDeque."""
    _RUN_STATE_MACHINE(LmdbDequeDiskStateMachine)


def test_lmdb_deque_memory_state_machine() -> None:
    """Run stateful property tests for in-memory LmdbDeque."""
    _RUN_STATE_MACHINE(LmdbDequeMemoryStateMachine)

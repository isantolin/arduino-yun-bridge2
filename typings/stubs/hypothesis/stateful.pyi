"""Type stub for hypothesis.stateful [SIL-2]."""

from __future__ import annotations

from typing import Any, Callable
from hypothesis.stateful import RuleBasedStateMachine
from hypothesis import settings

def run_state_machine_as_test(
    state_machine_factory: Callable[[], RuleBasedStateMachine] | type[RuleBasedStateMachine],
    settings: settings | None = None,
) -> None: ...

__all__ = ["run_state_machine_as_test"]

"""Extra coverage for mcubridge.state components (SIL-2)."""

from __future__ import annotations


from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import (
    create_runtime_state,
)


def test_state_record_supervisor_failure_logic():
    config = RuntimeConfig(serial_shared_secret=b"secret1234", allow_non_tmp_paths=True)
    state = create_runtime_state(config)

    state.record_supervisor_failure("test-task", backoff=5.0, exc=RuntimeError("fail"))

    assert "test-task" in state.supervisor_stats
    assert state.supervisor_stats["test-task"].restarts == 1
    assert state.supervisor_stats["test-task"].last_exception == "RuntimeError: fail"
    assert state.supervisor_failures == 1

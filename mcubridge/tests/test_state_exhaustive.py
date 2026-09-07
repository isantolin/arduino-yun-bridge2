import time

from mcubridge.state.context import create_runtime_state
from mcubridge.config.settings import RuntimeConfig


def test_state_metrics_exhaustive() -> None:
    config = RuntimeConfig(topic_prefix="br", serial_port="/dev/test")
    state = create_runtime_state(config)

    state.connection_fsm.connect()
    state.connection_fsm.synchronize()

    state.handshake_attempts = 5
    state.handshake_successes = 2
    assert state.handshake_attempts == 5

    state.handshake_last_started = time.monotonic() - 10
    assert state.handshake_duration_since_start() >= 10

    _ = state.is_synchronized
    _ = state.state
    _ = state.mcu_status_counts

    state.cleanup()

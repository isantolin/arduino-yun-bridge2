# pyright: reportPrivateUsage=false
import time

from mcubridge.state.context import create_runtime_state
from mcubridge.config.settings import RuntimeConfig


def test_state_metrics_exhaustive() -> None:
    config = RuntimeConfig(mqtt_topic="br", serial_port="/dev/test")
    state = create_runtime_state(config)

    state.mark_transport_connected()
    state.mark_synchronized()
    state.mark_supervisor_healthy("task")
    state.record_supervisor_failure("task", 1.0, RuntimeError("fail"))

    state.apply_handshake_stats(
        {"attempts": 5, "successes": 2, "last_unix": time.time()}
    )
    assert state.handshake_attempts == 5

    state._apply_spool_observation({"corrupt_dropped": 1, "dropped_due_to_limit": 1})

    state.handshake_last_started = time.monotonic() - 10
    assert state.handshake_duration_since_start() >= 10

    _ = state.is_synchronized
    _ = state.state
    _ = state.mcu_status_counts

    state.cleanup()

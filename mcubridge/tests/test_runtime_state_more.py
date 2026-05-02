"""More unit tests for RuntimeState edge cases (SIL-2)."""

from __future__ import annotations

from typing import Any
import pytest
from unittest.mock import AsyncMock, MagicMock

from mcubridge.mqtt.spool import MQTTPublishSpool
from mcubridge.state.context import RuntimeState


@pytest.mark.asyncio
async def test_disable_mqtt_spool_handles_close_errors(
    runtime_config: Any,
) -> None:
    from mcubridge.config.settings import RuntimeConfig

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234", allow_non_tmp_paths=True
    )
    state = RuntimeState(config)
    AsyncMock()

    mock_spool = MagicMock(spec=MQTTPublishSpool)
    mock_spool.close.side_effect = OSError("close-failed")
    state.mqtt_spool = mock_spool

    # Should not raise
    state.cleanup()
    assert state.mqtt_spool is None

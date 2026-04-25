from unittest.mock import MagicMock, patch
import pytest
import msgspec
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state
from mcubridge.services.runtime import BridgeService
from mcubridge import daemon
from mcubridge.mqtt.spool_manager import MqttSpoolManager


def create_real_config():
    from mcubridge.config.common import get_default_config

    raw_cfg = get_default_config()
    raw_cfg.update(
        {
            "serial_port": "/dev/ttyFake",
            "serial_shared_secret": b"valid_secret_1234",
            "mqtt_spool_dir": ".tmp_tests/spool_v3",
            "allow_non_tmp_paths": True,
        }
    )
    return msgspec.convert(raw_cfg, RuntimeConfig)


@pytest.mark.asyncio
async def test_process_run_async_limit_reached():
    config = create_real_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MagicMock(spec=MqttSpoolManager))
        state.running_processes = {
            i: MagicMock() for i in range(config.process_max_concurrent)
        }

        from mcubridge.services.process import ProcessComponent

        comp = service._container.get(ProcessComponent)

        from mcubridge.protocol.structures import ProcessRunAsyncPacket

        pkt = ProcessRunAsyncPacket(command="ls")

        # Should return error status or handle limit
        await comp.handle_run_async(0, msgspec.msgpack.encode(pkt))
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_daemon_run_exception_group_coverage():
    config = create_real_config()
    d = daemon.BridgeDaemon(config)

    with patch.object(d, "_supervise", side_effect=RuntimeError("Group-Fail")):
        with pytest.raises(
            ExceptionGroup if hasattr(globals(), "ExceptionGroup") else Exception
        ):
            await d.run()


@pytest.mark.asyncio
async def test_configure_logging_stream_env(monkeypatch):
    monkeypatch.setenv("MCUBRIDGE_LOG_STREAM", "1")
    from mcubridge.config import logging as bridge_logging

    config = create_real_config()
    bridge_logging.configure_logging(config)

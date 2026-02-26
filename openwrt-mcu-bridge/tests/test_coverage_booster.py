from transitions.core import MachineError

import asyncio
import errno
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import pytest
from mcubridge import metrics
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Status
from mcubridge.services.process import ProcessComponent
from mcubridge.state.context import PROCESS_STATE_FINISHED, ManagedProcess
from mcubridge.transport.serial import BridgeSerialProtocol, SerialTransport


def create_real_config():
    from mcubridge.config.common import get_default_config

    raw_cfg = get_default_config()
    raw_cfg.update(
        {
            "serial_port": "/dev/ttyFake",
            "serial_shared_secret": b"valid_secret_1234",
            "mqtt_spool_dir": "/tmp/spool_booster",
        }
    )
    return msgspec.convert(raw_cfg, RuntimeConfig)


# --- ProcessComponent Booster ---


@pytest.mark.asyncio
async def test_process_run_sync_exception_group_no_match():
    config = create_real_config()
    state = MagicMock()
    comp = ProcessComponent(config, state, MagicMock())

    mock_proc = MagicMock()
    mock_proc.wait = AsyncMock()

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        patch(
            "asyncio.TaskGroup.__aenter__",
            side_effect=BaseExceptionGroup("Group", [BaseException("Literal")]),
        ),
    ):
        with pytest.raises(BaseExceptionGroup):
            await comp.run_sync("cmd", ["cmd"])


@pytest.mark.asyncio
async def test_process_collect_output_slot_changed():
    config = create_real_config()
    state = MagicMock()
    state.process_lock = asyncio.Lock()
    ctx = MagicMock()
    comp = ProcessComponent(config, state, ctx)

    slot = ManagedProcess(pid=123)
    state.running_processes = {123: slot}

    with patch.object(slot, "pop_payload", return_value=(b"", b"", False, False)):
        state.running_processes = MagicMock()
        state.running_processes.get.side_effect = [slot, None]

        batch = await comp.collect_output(123)
        assert batch.status_byte == Status.ERROR.value


@pytest.mark.asyncio
async def test_process_collect_output_finished_finalize_fail():
    config = create_real_config()
    state = MagicMock()
    state.process_lock = asyncio.Lock()
    comp = ProcessComponent(config, state, MagicMock())

    slot = ManagedProcess(pid=123)
    slot.fsm_state = PROCESS_STATE_FINISHED
    slot.trigger = MagicMock(side_effect=MachineError("FSM Fail"))

    state.running_processes = {123: slot}
    await comp.collect_output(123)


@pytest.mark.asyncio
async def test_process_finalize_async_process_fsm_fail():
    config = create_real_config()
    state = MagicMock()
    state.process_lock = asyncio.Lock()
    comp = ProcessComponent(config, state, MagicMock())

    slot = ManagedProcess(pid=123)
    slot.trigger = MagicMock(side_effect=MachineError("FSM Fail"))
    state.running_processes = {123: slot}

    with patch.object(
        comp, "_drain_process_pipes", new_callable=AsyncMock, return_value=(b"", b"")
    ):
        await comp._finalize_async_process(123, MagicMock())


# --- Metrics Booster ---


@pytest.mark.asyncio
async def test_metrics_publish_metrics_error_path():
    state = MagicMock()
    state.build_metrics_snapshot.side_effect = ValueError("Boom")
    enqueue = AsyncMock()

    # Trigger line 217: logger.error("Failed to publish initial metrics payload: %s", e)
    with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await metrics.publish_metrics(state, enqueue, interval=10)


@pytest.mark.asyncio
async def test_metrics_collector_flatten_edge_cases():
    state = MagicMock()
    coll = metrics._RuntimeStateCollector(state)
    assert list(coll._flatten("t", True)) == [("gauge", "t", 1.0)]
    assert list(coll._flatten("t", False)) == [("gauge", "t", 0.0)]
    assert list(coll._flatten("t", 1.5)) == [("gauge", "t", 1.5)]


# --- Serial Protocol Booster ---


@pytest.mark.asyncio
async def test_serial_protocol_async_process_parse_error_crc():
    proto = BridgeSerialProtocol(MagicMock(), MagicMock(), asyncio.get_event_loop())
    with (
        patch("cobs.cobs.decode", return_value=b"raw"),
        patch(
            "mcubridge.protocol.frame.Frame.from_bytes",
            side_effect=ValueError("CRC mismatch"),
        ),
    ):
        await proto._async_process_packet(b"encoded")
        proto.state.record_serial_crc_error.assert_called_once()


@pytest.mark.asyncio
async def test_serial_protocol_async_process_os_error():
    proto = BridgeSerialProtocol(MagicMock(), MagicMock(), asyncio.get_event_loop())
    with (patch("cobs.cobs.decode", side_effect=OSError("Disk full")),):
        await proto._async_process_packet(b"encoded")
        proto.state.record_serial_decode_error.assert_called_once()


@pytest.mark.asyncio
async def test_serial_protocol_async_process_runtime_error():
    proto = BridgeSerialProtocol(MagicMock(), MagicMock(), asyncio.get_event_loop())
    with (patch("cobs.cobs.decode", side_effect=RuntimeError("Bug")),):
        await proto._async_process_packet(b"encoded")
        proto.state.record_serial_decode_error.assert_called_once()


@pytest.mark.asyncio
async def test_serial_protocol_log_frame_unknown():
    proto = BridgeSerialProtocol(MagicMock(), MagicMock(), asyncio.get_event_loop())
    from mcubridge.protocol.frame import Frame

    frame = Frame(command_id=0xFFFF, payload=b"")
    with patch("mcubridge.transport.serial.logger.log") as mock_log:
        with patch("mcubridge.transport.serial.logger.isEnabledFor", return_value=True):
            proto._log_frame(frame, "DIR")
            # logger.log(level, "%s %s: %s", direction, label, hex)
            assert mock_log.call_args[0][0] == logging.DEBUG
            assert mock_log.call_args[0][1] == "%s %s: %s"
            assert mock_log.call_args[0][2] == "DIR"
            assert mock_log.call_args[0][3] == "0xFFFF"
            assert mock_log.call_args[0][4] == "[]"


@pytest.mark.asyncio
async def test_serial_transport_toggle_dtr_oserror_other():
    transport = SerialTransport(create_real_config(), MagicMock(), MagicMock())
    with patch("serial.Serial", side_effect=OSError(errno.EIO, "IO Error")):
        await transport._toggle_dtr(asyncio.get_event_loop())

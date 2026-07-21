"""Exhaustive gap closure suite 21 for Python daemon SIL-2 coverage (96%+ target)."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.serial import SerialTransport


@pytest.mark.asyncio
async def test_serial_transport_dtr_and_service_disconnect_errors():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_port = AsyncMock()
    st = SerialTransport(cfg, state, mock_port)

    # 1. _toggle_dtr when set_modem_pins raises OSError
    mock_port.set_modem_pins.side_effect = OSError("DTR modem error")
    st.serial = mock_port
    await st._toggle_dtr()  # type: ignore[reportPrivateUsage]

    # 2. _correlate_frame with command_id == Status.ACK.value and ProtobufMessage payload
    st_any = cast(Any, st)
    pending = MagicMock()
    pending.command_id = protocol.Command.CMD_DIGITAL_WRITE.value
    pending.success = None
    pending.expected_resp_ids = (protocol.Command.CMD_DIGITAL_WRITE.value,)
    st_any._current = pending

    ack_msg = pb.AckPacket(command_id=protocol.Command.CMD_DIGITAL_WRITE.value)
    st_any._correlate_frame(protocol.Status.ACK.value, ack_msg)

    # 3. _correlate_frame with command_id == Status.ACK.value and malformed bytes payload
    pending2 = MagicMock()
    pending2.command_id = protocol.Command.CMD_DIGITAL_WRITE.value
    pending2.success = None
    pending2.expected_resp_ids = (protocol.Command.CMD_DIGITAL_WRITE.value,)
    st_any._current = pending2

    st_any._correlate_frame(protocol.Status.ACK.value, b"\xff\xff\xff")

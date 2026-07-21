"""Exhaustive gap closure suite 23 for Python daemon SIL-2 coverage (97.5%+ target)."""

from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_runtime_service_del_and_no_serial_branches():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    service = BridgeService(cfg, state, mock_serial)

    # 1. __del__ cleanup
    service.__del__()

    # 2. handlers when self.serial is None
    service.serial = None
    s_any = cast(Any, service)

    assert await s_any._on_mcu_mailbox_available(1, None) is False
    assert await s_any._on_mcu_mailbox_read(1, None) is False

    write_pb = pb.FileWrite(path="a.txt", data=b"hi")
    assert await s_any._on_mcu_file_write(1, write_pb) is False

    read_pb = pb.FileRead(path="a.txt")
    await s_any._on_mcu_file_read(1, read_pb)

    remove_pb = pb.FileRemove(path="a.txt")
    assert await s_any._on_mcu_file_remove(1, remove_pb) is False

    run_pb = pb.ProcessRunAsync(command="invalid_cmd_xyz")
    assert await s_any._on_mcu_process_run(1, run_pb) is False


@pytest.mark.asyncio
async def test_runtime_service_mailbox_empty_read_and_process_denied():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    service = BridgeService(cfg, state, mock_serial)
    s_any = cast(Any, service)

    # 1. _on_mcu_mailbox_read when queue is empty (popleft raises IndexError)
    with patch.object(state.mailbox_queue, "popleft", side_effect=IndexError):
        res = await s_any._on_mcu_mailbox_read(1, None)
        assert res is True

    # 2. _on_mcu_process_run when command is denied by policy
    run_pb = pb.ProcessRunAsync(command="forbidden_cmd")
    with patch("mcubridge.services.runtime.is_command_allowed", return_value=False):
        res2 = await s_any._on_mcu_process_run(1, run_pb)
        assert res2 is False

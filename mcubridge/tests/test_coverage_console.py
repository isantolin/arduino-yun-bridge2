"""Extra coverage for ConsoleComponent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import msgspec
from mcubridge.services.console import ConsoleComponent
from mcubridge.protocol.structures import ConsoleWritePacket


@pytest.fixture
def console_comp(runtime_config, runtime_state):
    serial_flow = MagicMock()
    return ConsoleComponent(runtime_config, runtime_state, serial_flow)


@pytest.mark.asyncio
async def test_console_handle_write_malformed(console_comp: ConsoleComponent):
    with patch(
        "mcubridge.services.console.atomic_publish", new_callable=AsyncMock
    ) as mock_publish:
        await console_comp.handle_write(0, b"bad-msgpack")
        assert not mock_publish.called


@pytest.mark.asyncio
async def test_console_handle_write_empty_data(console_comp: ConsoleComponent):
    empty_pkt = msgspec.msgpack.encode(ConsoleWritePacket(data=b""))
    with patch(
        "mcubridge.services.console.atomic_publish", new_callable=AsyncMock
    ) as mock_publish:
        await console_comp.handle_write(0, empty_pkt)
        assert not mock_publish.called

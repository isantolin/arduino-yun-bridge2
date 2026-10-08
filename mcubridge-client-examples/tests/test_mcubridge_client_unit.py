"""Comprehensive unit tests for mcubridge_client (cli, env, spi, definitions). [SIL-2]"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from hypothesis import given, settings
from hypothesis import strategies as st
from mcubridge_client import (
    CLOUD_DEFAULT_TOPIC_PREFIX,
    LocalBridgeStub,
    SpiBitOrder,
    SpiDataMode,
    SpiDevice,
    build_bridge_args,
    dump_client_env,
    pb,
)
from mcubridge_client.cli import bridge_session, configure_logging
from mcubridge_client.env import is_openwrt, read_uci_general
from typer.testing import CliRunner

# ==============================================================================
# cli.py & env.py tests
# ==============================================================================


def test_cli_configure_logging() -> None:
    """configure_logging sets up basic logging without raising exceptions."""
    configure_logging()
    structlog.get_logger("test").info("logging configured")
    assert structlog.is_configured()


@pytest.mark.asyncio
async def test_cli_bridge_session() -> None:
    """bridge_session context manager yields Channel and LocalBridgeStub."""
    mock_chan = MagicMock()
    mock_chan.__dispatch__ = MagicMock()

    async with bridge_session(
        host="127.0.0.1",
        port=8443,
        device_id="yun-01",
        channel_factory=lambda _h, _p: mock_chan,
    ) as (chan, stub):
        assert chan is mock_chan
        assert isinstance(stub, LocalBridgeStub)
        mock_chan.__dispatch__.add_listener.assert_called_once()
        callback = mock_chan.__dispatch__.add_listener.call_args[0][1]
        event = MagicMock()
        event.metadata = {}
        await callback(event)
        assert event.metadata["x-device-id"] == "yun-01"

    mock_chan.close.assert_called_once()


def test_env_is_openwrt_force_uci(monkeypatch: pytest.MonkeyPatch) -> None:
    """is_openwrt checks environment variable and file presence."""
    monkeypatch.setenv("MCUBRIDGE_FORCE_UCI", "1")
    assert is_openwrt() is True


def test_env_is_openwrt_file_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MCUBRIDGE_FORCE_UCI", raising=False)
    fake_release = tmp_path / "openwrt_release"
    fake_release.touch()
    assert is_openwrt(release_file=fake_release) is True


def test_env_read_uci_general(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_uci_general returns UCI config dict or empty dict."""
    monkeypatch.delenv("MCUBRIDGE_FORCE_UCI", raising=False)
    assert read_uci_general() == {}

    res = read_uci_general(config_getter=lambda: {"cloud_host": "127.0.0.1", "_private": "x"})
    assert res == {"cloud_host": "127.0.0.1"}

    # Exception path in get_uci_config
    def _raise_error() -> dict[str, Any]:
        raise RuntimeError("UCI error")

    assert read_uci_general(config_getter=_raise_error) == {}


def test_env_dump_client_env(capsys: pytest.CaptureFixture[str]) -> None:
    """dump_client_env outputs snapshot to logger or stdout."""
    # 1. Custom logger
    mock_log = MagicMock()
    dump_client_env(mock_log)
    assert mock_log.info.call_count >= 2

    # 2. Stdout fallback
    dump_client_env(None)
    captured = capsys.readouterr()
    assert "gateway_host=" in captured.out
    assert "target_device_id=" in captured.out


# ==============================================================================
# spi.py & definitions.py tests
# ==============================================================================


def test_definitions_build_bridge_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_bridge_args builds dictionary targeting Gateway with explicit device_id."""
    monkeypatch.delenv("MCUBRIDGE_DEVICE_ID", raising=False)
    args = build_bridge_args(
        host="127.0.0.1",
        port=8443,
        device_id="yun-01",
        topic_prefix=CLOUD_DEFAULT_TOPIC_PREFIX,
    )
    assert args == {
        "host": "127.0.0.1",
        "port": 8443,
        "device_id": "yun-01",
        "topic_prefix": CLOUD_DEFAULT_TOPIC_PREFIX,
    }
    # Explicit device_id is required: missing device_id raises ValueError
    with pytest.raises(ValueError, match="Explicit target device_id is required"):
        build_bridge_args(host="127.0.0.1", port=8443)


@pytest.mark.asyncio
async def test_spi_device_lifecycle_and_transfer() -> None:
    """SpiDevice context manager, properties, begin/end, and transfer."""
    mock_stub = MagicMock(spec=LocalBridgeStub)
    mock_stub.SpiConfigure = AsyncMock(return_value=pb.GenericResponse(status="ok"))

    def _mock_spi_transfer(req: pb.SpiTransfer) -> pb.SpiTransferResponse:
        return pb.SpiTransferResponse(data=req.data)

    mock_stub.SpiTransfer = AsyncMock(side_effect=_mock_spi_transfer)

    dev = SpiDevice(
        mock_stub,
        frequency=2000000,
        bit_order=SpiBitOrder.SPI_BIT_ORDER_LSB_FIRST,
        mode=SpiDataMode.SPI_DATA_MODE_1,
    )

    assert dev.frequency == 2000000
    assert dev.bit_order == SpiBitOrder.SPI_BIT_ORDER_LSB_FIRST
    assert dev.mode == SpiDataMode.SPI_DATA_MODE_1

    async with dev as active_dev:
        assert active_dev is dev
        mock_stub.SpiConfigure.assert_called_once()

        # Verify protobuf SpiConfig payload was sent accurately
        cfg_pb = mock_stub.SpiConfigure.call_args[0][0]
        assert cfg_pb.frequency == 2000000
        assert cfg_pb.bit_order == SpiBitOrder.SPI_BIT_ORDER_LSB_FIRST.value
        assert cfg_pb.data_mode == SpiDataMode.SPI_DATA_MODE_1.value

        # Idempotent begin
        await dev.begin()

        # Transfer with bytes and Sequence[int]
        res1 = await dev.transfer(b"\x01\x02")
        assert res1 == b"\x01\x02"

        res2 = await dev.transfer([1, 2, 3])
        assert res2 == b"\x01\x02\x03"

    await dev.end()


_SEGMENT_STRATEGY = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=16)


@settings(max_examples=30, derandomize=True, deadline=None)
@given(seg1=_SEGMENT_STRATEGY, seg2=_SEGMENT_STRATEGY)
def test_topic_matches_exact_reflexive_property(seg1: str, seg2: str) -> None:
    """Validate that exact matching is reflexive for any arbitrary topic path."""
    from mcubridge_client.protocol import Topic

    topic_str = f"br/{seg1}/{seg2}"
    assert Topic.matches(topic_str, topic_str)


@settings(max_examples=30, derandomize=True, deadline=None)
@given(seg1=_SEGMENT_STRATEGY, seg2=_SEGMENT_STRATEGY)
def test_topic_matches_wildcards_property(seg1: str, seg2: str) -> None:
    """Validate MQTT single (+) and multi-level (#) wildcard matching invariants."""
    from mcubridge_client.protocol import Topic

    topic_str = f"br/{seg1}/{seg2}"
    assert Topic.matches("br/#", topic_str)
    assert Topic.matches(f"br/+/{seg2}", topic_str)
    assert Topic.matches(f"br/{seg1}/+", topic_str)


@settings(max_examples=30, derandomize=True, deadline=None)
@given(
    prefix=_SEGMENT_STRATEGY,
    seg1=_SEGMENT_STRATEGY,
    seg2=_SEGMENT_STRATEGY,
)
def test_topic_build_and_match_invariants(prefix: str, seg1: str, seg2: str) -> None:
    """Validate Topic.build output structure and matching against generated paths."""
    from mcubridge_client.protocol import Topic

    built = Topic.build(seg1, seg2, prefix=prefix)
    assert built == f"{prefix}/{seg1}/{seg2}"
    assert Topic.matches(f"{prefix}/#", built)
    assert Topic.matches(built, built)


@pytest.mark.asyncio
async def test_smoke_connection_run_test() -> None:
    """Verify test_smoke_connection.run_test calls bridge_session correctly."""
    import test_smoke_connection

    mock_sess = MagicMock()
    mock_chan = MagicMock()
    mock_stub = MagicMock()
    mock_sess.return_value.__aenter__.return_value = (mock_chan, mock_stub)
    await test_smoke_connection.run_test(
        host="127.0.0.1", port=8443, device_id="yun-01", session_factory=mock_sess
    )
    mock_sess.assert_called_once_with(host="127.0.0.1", port=8443, device_id="yun-01")


def test_smoke_connection_cli_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify test_smoke_connection CLI entry point invokes run_test via typer runner."""
    import test_smoke_connection

    mock_run = AsyncMock()
    monkeypatch.setattr(test_smoke_connection, "executor_fn", mock_run)
    runner = CliRunner()
    res = runner.invoke(
        cast(Any, test_smoke_connection.cli),
        ["--host", "127.0.0.1", "--port", "8443", "--device-id", "yun-01", "--topic-prefix", "test"],
    )
    assert res.exit_code == 0
    mock_run.assert_awaited_once_with("127.0.0.1", 8443, "yun-01", "test")


@pytest.mark.asyncio
async def test_gateway_northbound_run_test() -> None:
    """Verify test_gateway_northbound.run_test calls DispatchCommand correctly."""
    import test_gateway_northbound

    mock_chan = MagicMock()
    mock_stub = MagicMock()
    mock_stub.DispatchCommand = AsyncMock(return_value=MagicMock(status_code=200, payload=b"OK"))

    await test_gateway_northbound.run_test(
        host="127.0.0.1",
        port=8443,
        device_id="yun-01",
        channel_factory=lambda _h, _p: mock_chan,
        stub_factory=lambda _c: mock_stub,
    )
    mock_stub.DispatchCommand.assert_awaited_once()


def test_gateway_northbound_cli_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify test_gateway_northbound CLI entry point invokes run_test via typer runner."""
    import test_gateway_northbound

    mock_run = AsyncMock()
    monkeypatch.setattr(test_gateway_northbound, "executor_fn", mock_run)
    runner = CliRunner()
    res = runner.invoke(
        cast(Any, test_gateway_northbound.cli),
        ["--host", "127.0.0.1", "--port", "8443", "--device-id", "yun-01"],
    )
    assert res.exit_code == 0
    mock_run.assert_awaited_once_with("127.0.0.1", 8443, "yun-01")


def test_gateway_northbound_cli_missing_device() -> None:
    """Verify test_gateway_northbound CLI raises error when device_id is omitted."""
    import test_gateway_northbound

    runner = CliRunner()
    res = runner.invoke(
        cast(Any, test_gateway_northbound.cli),
        ["--host", "127.0.0.1", "--port", "8443"],
        env={"MCUBRIDGE_DEVICE_ID": ""},
    )
    assert res.exit_code != 0

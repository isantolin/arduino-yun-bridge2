# pyright: reportPrivateUsage=false
import contextlib
import io
from types import SimpleNamespace

from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.transport.mqtt import MqttTransport
import asyncio
from mcubridge.daemon import app
from unittest.mock import patch, MagicMock, AsyncMock


from typing import Any, Callable


class CliRunner:
    def invoke(
        self, func: Callable[[list[str]], Any], args: list[str]
    ) -> SimpleNamespace:
        buf = io.StringIO()
        exit_code = 0
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                func(args)
        except SystemExit as e:
            exit_code = int(e.code) if isinstance(e.code, int) else 1
        except Exception:
            exit_code = 1
        return SimpleNamespace(exit_code=exit_code, output=buf.getvalue())


runner = CliRunner()


def test_daemon_cli_help():
    """Verify CLI help works and covers entry paths."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Main entry point" in result.output


def test_daemon_cli_invalid_config():
    """Verify CLI error handling for invalid config."""
    with patch(
        "mcubridge.daemon.load_runtime_config", side_effect=ValueError("Invalid config")
    ):
        result = runner.invoke(app, ["--serial-port", "/dev/null"])
        assert result.exit_code == 1


@patch("mcubridge.daemon.verify_crypto_integrity", return_value=False)
def test_daemon_cli_crypto_fail(mock_verify: MagicMock):
    """Verify CLI aborts on crypto integrity failure."""
    result = runner.invoke(app, ["--serial-port", "/dev/null"])
    assert result.exit_code == 1
    assert "CRYPTOGRAPHIC INTEGRITY CHECK FAILED" in result.output


def test_daemon_cli_default_secret_warning():
    """Verify CLI warning when using default secret."""
    with (
        patch("mcubridge.daemon.verify_crypto_integrity", return_value=True),
        patch("mcubridge.daemon.load_runtime_config") as mock_load,
        patch("mcubridge.daemon.BridgeDaemon"),
        patch("asyncio.Runner"),
    ):
        from mcubridge.config.const import DEFAULT_SERIAL_SHARED_SECRET
        from mcubridge.config.settings import RuntimeConfig

        mock_config = MagicMock(spec=RuntimeConfig)
        mock_config.serial_shared_secret = DEFAULT_SERIAL_SHARED_SECRET
        mock_config.serial_port = "/dev/ttyFake"
        mock_config.serial_baud = 115200
        mock_config.mqtt_host = "localhost"
        mock_config.mqtt_port = 1883

        mock_load.return_value = mock_config

        result = runner.invoke(app, ["--non-interactive"])
        assert "SECURITY CRITICAL" in result.output


def test_spi_service_coverage():
    """Boost coverage for SPI service which is at 29%."""
    from mcubridge.services.spi import SpiComponent
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.protocol.structures import TopicRoute
    from mcubridge.protocol.topics import Topic
    from aiomqtt.message import Message

    mock_config = MagicMock(spec=RuntimeConfig)
    mock_state = MagicMock()
    mock_state.mqtt_topic_prefix = "br"

    serial_flow = AsyncMock(spec=SerialFlowController)
    mqtt_flow = AsyncMock(spec=MqttTransport)

    service = SpiComponent(
        config=mock_config,
        state=mock_state,
        serial_flow=serial_flow,
        mqtt_flow=mqtt_flow,
    )

    # Test handle_mqtt for 'begin'
    route = TopicRoute(
        raw="br/spi/begin", prefix="br", topic=Topic.SPI, segments=("begin",)
    )
    msg = Message(Topic.SPI.value, b"", 0, False, False, None)
    asyncio.run(service.handle_mqtt(route, msg))
    serial_flow.send.assert_called()

    # Test handle_mqtt for 'config'
    route_cfg = TopicRoute(
        raw="br/spi/config", prefix="br", topic=Topic.SPI, segments=("config",)
    )
    import msgspec

    payload = msgspec.json.encode({"frequency": 1000000})
    msg_cfg = Message(Topic.SPI.value, payload, 0, False, False, None)
    asyncio.run(service.handle_mqtt(route_cfg, msg_cfg))

    # Test handle_transfer_resp
    asyncio.run(service.handle_transfer_resp(1, b"\x91\xc4\x04data"))
    mqtt_flow.enqueue_mqtt.assert_called()

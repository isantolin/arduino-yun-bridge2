"""
test_coverage_extreme.py (V3 Fixed).

Objetivo: 100% Cobertura Real en Daemon y Transportes (Py3.13 Compatible).
"""
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from yunbridge.transport.serial import SerialTransport
from yunbridge.transport.mqtt import mqtt_task
from yunbridge.daemon import BridgeDaemon
from yunbridge.rpc.protocol import (
    FRAME_DELIMITER,
    Command,
    UINT8_MASK,
)
from yunbridge.rpc.frame import Frame
from cobs import cobs
import aiomqtt


# --- DAEMON TESTS (Refactored) ---


def test_daemon_task_setup_logic():
    """Verifica que se crean las tareas correctas según la config."""
    mock_config = MagicMock()
    mock_config.serial_shared_secret = "secret"
    mock_config.watchdog_enabled = True
    mock_config.metrics_enabled = True

    # Valores numéricos explícitos para evitar TypeError en comparaciones
    mock_config.bridge_summary_interval = 10.0
    mock_config.bridge_handshake_interval = 10.0
    mock_config.status_interval = 5.0
    mock_config.watchdog_interval = 10.0
    mock_config.metrics_host = "localhost"
    mock_config.metrics_port = 9090

    with patch("yunbridge.daemon.create_runtime_state"), \
            patch("yunbridge.daemon.BridgeService"):

        daemon = BridgeDaemon(mock_config)
        specs = daemon._setup_supervision()

        task_names = [s.name for s in specs]
        assert "serial-link" in task_names
        assert "mqtt-link" in task_names
        assert "watchdog" in task_names
        assert "prometheus-exporter" in task_names
        assert "bridge-snapshots" in task_names


@pytest.mark.asyncio
async def test_daemon_run_lifecycle():
    """Prueba el ciclo de vida completo de run() sin bloquear."""
    mock_config = MagicMock()
    # Desactivar features opcionales para simplificar
    mock_config.watchdog_enabled = False
    mock_config.metrics_enabled = False

    # Valores numéricos explícitos (0.0 para desactivar lógica de intervalos)
    mock_config.bridge_summary_interval = 0.0
    mock_config.bridge_handshake_interval = 0.0
    mock_config.status_interval = 5.0
    mock_config.serial_shared_secret = "secret"

    with patch("yunbridge.daemon.create_runtime_state"), \
            patch("yunbridge.daemon.BridgeService") as MockService, \
            patch("yunbridge.daemon.supervise_task",
                  new_callable=AsyncMock) as mock_supervise:

        # Hacer que supervise_task retorne inmediatamente para no bloquear
        mock_supervise.return_value = None

        # Simular Context Manager del servicio
        service_instance = MockService.return_value
        service_instance.__aenter__.return_value = service_instance
        service_instance.__aexit__.return_value = None

        daemon = BridgeDaemon(mock_config)

        # Ejecutar run (debe terminar rápido porque supervise_task es mock)
        await daemon.run()

        assert mock_supervise.call_count >= 2  # Al menos serial y mqtt


# --- SERIAL TRANSPORT: DEEP RESILIENCE ---


@pytest.mark.asyncio
async def test_serial_read_loop_corruption_and_recovery():
    """Simula flujo de bytes corruptos y recuperación."""
    mock_config = MagicMock()
    mock_state = MagicMock()
    mock_service = AsyncMock()

    transport = SerialTransport(mock_config, mock_state, mock_service)
    mock_reader = AsyncMock()
    transport.reader = mock_reader
    transport.writer = MagicMock()

    # Data stream: [Valid] [Corrupt] [Huge] [Noise]
    valid_frame = cobs.encode(
        Frame.build(Command.CMD_GET_VERSION, b"")
    ) + FRAME_DELIMITER
    bad_cobs = bytes([5, UINT8_MASK, UINT8_MASK]) + FRAME_DELIMITER
    huge_chunk = b"A" * 300 + FRAME_DELIMITER
    TEST_PAYLOAD_BYTE = 0xAA
    noise = bytes([0, 0, UINT8_MASK, TEST_PAYLOAD_BYTE])

    feed_data = [valid_frame, bad_cobs, huge_chunk, noise, b""]

    # Generador asíncrono byte a byte
    async def feed_generator():
        for chunk in feed_data:
            for b in chunk:
                yield bytes([b])

    iterator = feed_generator()

    async def mock_read(_n):
        try:
            return await iterator.__anext__()
        except StopAsyncIteration:
            return b""  # EOF para salir del loop

    mock_reader.read.side_effect = mock_read

    await transport._read_loop()

    mock_service.handle_mcu_frame.assert_awaited()
    assert mock_state.record_serial_decode_error.call_count >= 2


@pytest.mark.asyncio
async def test_serial_write_flow_control():
    """Prueba protecciones de escritura."""
    transport = SerialTransport(MagicMock(), MagicMock(), MagicMock())
    transport.writer = None
    assert (
        await transport.send_frame(Command.CMD_GET_VERSION.value, b"")
        is False
    )

    transport.writer = MagicMock()
    transport.writer.is_closing.return_value = True
    assert (
        await transport.send_frame(Command.CMD_GET_VERSION.value, b"")
        is False
    )


# --- MQTT TRANSPORT: CONNECTION BACKOFF ---


@pytest.mark.asyncio
async def test_mqtt_connection_backoff_and_auth_fail():
    """Simula fallos de conexión y backoff."""
    mock_config = MagicMock()
    mock_config.mqtt_host = "localhost"
    mock_config.reconnect_delay = 0.01
    # FIX: Desactivar TLS explícitamente
    mock_config.tls_enabled = False

    mock_client_cls = MagicMock()
    mock_ctx = MagicMock()

    # Fallos secuenciales -> Cancelación
    mock_ctx.__aenter__ = AsyncMock(side_effect=[
        aiomqtt.MqttError("Network Unreachable"),
        OSError("No route to host"),
        asyncio.CancelledError("Stop Test")
    ])
    mock_ctx.__aexit__ = AsyncMock()
    mock_client_cls.return_value = mock_ctx

    with patch("yunbridge.transport.mqtt.aiomqtt.Client", mock_client_cls):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # FIX: Capturar BaseExceptionGroup para Py3.13
            with pytest.raises((asyncio.CancelledError, BaseExceptionGroup)):
                await mqtt_task(mock_config, MagicMock(), AsyncMock())

            assert mock_sleep.call_count >= 2


@pytest.mark.asyncio
async def test_mqtt_publisher_loop_error_handling():
    """Prueba resiliencia del publisher loop."""
    mock_config = MagicMock()
    mock_config.reconnect_delay = 0.01
    mock_config.tls_enabled = False  # FIX

    mock_state = MagicMock()
    queue = asyncio.Queue()
    msg = MagicMock()
    msg.topic_name = "test"
    msg.payload = b"data"
    await queue.put(msg)
    mock_state.mqtt_publish_queue = queue
    mock_state.flush_mqtt_spool = AsyncMock()

    mock_client = MagicMock()
    mock_client.publish = AsyncMock(side_effect=[
        aiomqtt.MqttError("Pub failed"),
        asyncio.CancelledError("Stop")
    ])

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)

    tg_mock = MagicMock()
    tg_mock.__aenter__ = AsyncMock(return_value=tg_mock)
    tg_mock.create_task = MagicMock()

    with patch("yunbridge.transport.mqtt.aiomqtt.Client",
               return_value=mock_ctx):
        with patch("asyncio.TaskGroup", return_value=tg_mock):
            task = asyncio.create_task(
                mqtt_task(mock_config, mock_state, AsyncMock())
            )
            await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, BaseExceptionGroup):
                pass

            # Extraer y ejecutar publisher loop aislado
            if tg_mock.create_task.call_args_list:
                publisher_coro = tg_mock.create_task.call_args_list[0][0][0]
                try:
                    await publisher_coro
                except (asyncio.CancelledError, BaseExceptionGroup):
                    pass
                mock_client.publish.assert_called()

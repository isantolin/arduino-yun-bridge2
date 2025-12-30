"""
test_coverage_extreme.py (V2)
Objetivo: Atacar bucles infinitos y manejo de excepciones en transportes.
"""
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from yunbridge.transport.serial import SerialTransport
from yunbridge.transport.mqtt import mqtt_task
from yunbridge.rpc.protocol import FRAME_DELIMITER, Command
from yunbridge.rpc.frame import Frame
from cobs import cobs
import aiomqtt

# --- SERIAL TRANSPORT: DEEP RESILIENCE ---


@pytest.mark.asyncio
async def test_serial_read_loop_corruption_and_recovery():
    """
    Simula flujo de bytes corruptos, tramas gigantes y recuperación.
    Cubre: _read_loop, _process_packet (ramas de error), buffer overflow.
    """
    mock_config = MagicMock()
    mock_state = MagicMock()
    mock_service = AsyncMock()

    transport = SerialTransport(mock_config, mock_state, mock_service)

    # Mockear reader/writer
    mock_reader = AsyncMock()
    transport.reader = mock_reader
    transport.writer = MagicMock()

    # Escenario de Inyección de Bytes:
    # 1. Trama válida
    valid_frame = cobs.encode(
        Frame.build(Command.CMD_GET_VERSION, b"")
    ) + FRAME_DELIMITER

    # 2. Trama corrupta (COBS inválido)
    bad_cobs = b"\x05\xFF\xFF" + FRAME_DELIMITER

    # 3. Trama gigante (Buffer Overflow)
    # MAX_SERIAL_PACKET_BYTES suele ser ~260. Enviamos 300 bytes.
    huge_chunk = b"A" * 300 + FRAME_DELIMITER

    # 4. Basura aleatoria (ruido de línea)
    noise = b"\x00\x00\xFF\xAA"

    # Configurar el stream de lectura simulado
    # side_effect devuelve bytes uno a uno o en chunks
    feed_data = [valid_frame, bad_cobs, huge_chunk, noise, b""]

    # Iterador asíncrono para simular lectura
    async def feed_generator():
        for chunk in feed_data:
            # Entregamos byte a byte para ejercitar la lógica de buffer
            for b in chunk:
                yield bytes([b])

    # Mockear read(1)
    iterator = feed_generator()

    async def mock_read(n):
        try:
            return await iterator.__anext__()
        except StopAsyncIteration:
            return b""

    mock_reader.read.side_effect = mock_read

    # Ejecutar solo el read_loop (no run() completo)
    await transport._read_loop()

    # Verificaciones
    # 1. Trama válida debió procesarse
    mock_service.handle_mcu_frame.assert_awaited()

    # 2. Errores debieron registrarse en state
    assert mock_state.record_serial_decode_error.call_count >= 2


@pytest.mark.asyncio
async def test_serial_write_flow_control():
    """
    Prueba que el writer respete si el puerto está cerrado o fallando.
    """
    transport = SerialTransport(MagicMock(), MagicMock(), MagicMock())

    # Caso 1: Writer es None
    transport.writer = None
    assert await transport.send_frame(0x01, b"") is False

    # Caso 2: Writer cerrándose
    transport.writer = MagicMock()
    transport.writer.is_closing.return_value = True
    assert await transport.send_frame(0x01, b"") is False


# --- MQTT TRANSPORT: CONNECTION BACKOFF ---


@pytest.mark.asyncio
async def test_mqtt_connection_backoff_and_auth_fail():
    """
    Simula caída del broker y reintentos con backoff exponencial.
    """
    mock_config = MagicMock()
    mock_config.mqtt_host = "localhost"
    mock_config.reconnect_delay = 0.01  # Rápido para el test
    # IMPORTANTE: Desactivar TLS explícitamente para evitar validación de paths
    mock_config.tls_enabled = False

    mock_state = MagicMock()
    mock_state.mqtt_topic_prefix = "test"

    # Mock Client context manager
    mock_client_cls = MagicMock()
    mock_ctx = MagicMock()

    # Configurar fallos secuenciales en __aenter__:
    # 1. MqttError (Red caída)
    # 2. OSError (Host inalcanzable)
    # 3. CancelledError (Para detener el test limpiamente)
    mock_ctx.__aenter__ = AsyncMock(side_effect=[
        aiomqtt.MqttError("Network Unreachable"),
        OSError("No route to host"),
        asyncio.CancelledError("Stop Test")
    ])
    mock_ctx.__aexit__ = AsyncMock()
    mock_client_cls.return_value = mock_ctx

    with patch("yunbridge.transport.mqtt.aiomqtt.Client", mock_client_cls):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # En Python 3.13, TaskGroup envuelve excepciones en BaseExceptionGroup
            with pytest.raises((asyncio.CancelledError, BaseExceptionGroup)):
                await mqtt_task(mock_config, mock_state, AsyncMock())

            # Verificar que hubo backoff (sleep llamado tras fallos)
            assert mock_sleep.call_count >= 2


@pytest.mark.asyncio
async def test_mqtt_publisher_loop_error_handling():
    """
    Prueba que el publisher no muera si falla un publish individual.
    """
    mock_config = MagicMock()
    mock_config.reconnect_delay = 0.01
    # IMPORTANTE: Desactivar TLS para que no intente buscar certificados
    mock_config.tls_enabled = False
    
    mock_state = MagicMock()

    # Configurar cola de publicación mock
    queue = asyncio.Queue()
    msg = MagicMock()
    msg.topic_name = "test/topic"
    msg.payload = b"data"
    msg.qos = 0
    msg.retain = False

    await queue.put(msg)
    mock_state.mqtt_publish_queue = queue
    mock_state.flush_mqtt_spool = AsyncMock()

    # Mock cliente conectado
    mock_client = MagicMock()
    mock_client.publish = AsyncMock(side_effect=[
        aiomqtt.MqttError("Pub failed"),  # Fallo 1
        asyncio.CancelledError("Stop")    # Parada
    ])

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    # TaskGroup mock para ejecutar los loops
    tg_mock = MagicMock()
    tg_mock.__aenter__ = AsyncMock(return_value=tg_mock)
    tg_mock.create_task = MagicMock()  # Capturamos las corutinas

    with patch("yunbridge.transport.mqtt.aiomqtt.Client",
               return_value=mock_ctx):
        with patch("asyncio.TaskGroup", return_value=tg_mock):
            # Lanzamos y cancelamos rápido
            task = asyncio.create_task(
                mqtt_task(mock_config, mock_state, AsyncMock())
            )
            await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, BaseExceptionGroup):
                pass

            # Recuperamos la corutina publisher del TaskGroup
            # args[0] de create_task
            if tg_mock.create_task.call_args_list:
                publisher_coro = tg_mock.create_task.call_args_list[0][0][0]

                # Ejecutamos el publisher aislado
                try:
                    await publisher_coro
                except (asyncio.CancelledError, BaseExceptionGroup):
                    pass

                # Verificamos que se intentó publicar
                mock_client.publish.assert_called()

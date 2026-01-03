"""
test_internals.py.

Objetivo: Ejecución quirúrgica de bucles internos y manejo de errores.
"""
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from yunbridge.transport.serial import SerialTransport
from yunbridge.daemon import main
from yunbridge.rpc.protocol import Command
from yunbridge.rpc.frame import Frame
from cobs import cobs


# --- SERIAL TRANSPORT INTERNALS ---

@pytest.mark.asyncio
async def test_serial_read_loop_handles_partial_reads_and_errors():
    """Prueba _read_loop byte a byte y con excepciones."""
    mock_config = MagicMock()
    mock_state = MagicMock()
    mock_service = AsyncMock()

    transport = SerialTransport(mock_config, mock_state, mock_service)

    # 1. Crear una trama REAL y válida
    # Frame válido: CMD_GET_VERSION con payload vacío
    raw_frame_bytes = Frame.build(Command.CMD_GET_VERSION, b"")
    encoded_frame = cobs.encode(raw_frame_bytes) + b'\x00'  # COBS + Delimiter

    # 2. Simular lector que devuelve bytes fragmentados
    mock_reader = AsyncMock()

    # Generar secuencia de lectura byte a byte + EOF
    read_side_effect = [bytes([b]) for b in encoded_frame]
    read_side_effect.append(b"")  # EOF para terminar el loop

    mock_reader.read.side_effect = read_side_effect

    # Hack: Inyectamos el reader directamente (la función usa self.reader)
    transport.reader = mock_reader

    # Ejecutar loop
    await transport._read_loop()

    # Verificaciones
    # read(1) se llama una vez por cada byte + 1 por el EOF
    assert mock_reader.read.call_count == len(encoded_frame) + 1

    # AHORA SÍ: Verificar que se procesó el frame correctamente
    # Como la trama es válida (COBS decoding OK -> CRC OK), debe llamar
    assert mock_service.handle_mcu_frame.call_count >= 1


# --- MQTT INTERNALS ---

@pytest.mark.asyncio
async def test_mqtt_internal_tls_setup_branches():
    """Prueba todas las ramas de configuración TLS (sin conectar)."""
    # Caso 1: TLS deshabilitado (Mock Config)
    mock_cfg = MagicMock()
    mock_cfg.tls_enabled = False

    # Accedemos a la función privada via import directo
    from yunbridge.transport.mqtt import _configure_tls
    assert _configure_tls(mock_cfg) is None

    # Caso 2: TLS habilitado sin cafile (usa trust store)
    mock_cfg.tls_enabled = True
    mock_cfg.mqtt_cafile = None
    mock_cfg.mqtt_tls_insecure = False
    mock_cfg.mqtt_certfile = None
    mock_cfg.mqtt_keyfile = None

    fake_ctx = MagicMock()
    fake_ctx.check_hostname = True

    with patch("yunbridge.transport.mqtt.ssl.create_default_context") as mk_ctx:
        mk_ctx.return_value = fake_ctx
        ctx = _configure_tls(mock_cfg)
        assert ctx is fake_ctx
        assert fake_ctx.check_hostname is True

    # Caso 3: cafile explícito pero inexistente (debe fallar)
    mock_cfg.mqtt_cafile = "/non/existent/ca.crt"

    with patch("yunbridge.transport.mqtt.Path") as MockPath:
        MockPath.return_value.exists.return_value = False
        with pytest.raises(RuntimeError, match="MQTT TLS CA file missing"):
            _configure_tls(mock_cfg)

    # Caso 4: mqtt_tls_insecure desactiva check_hostname
    mock_cfg.mqtt_cafile = None
    mock_cfg.mqtt_tls_insecure = True

    fake_ctx2 = MagicMock()
    fake_ctx2.check_hostname = True

    with patch("yunbridge.transport.mqtt.ssl.create_default_context") as mk_ctx2:
        mk_ctx2.return_value = fake_ctx2
        ctx2 = _configure_tls(mock_cfg)
        assert ctx2 is fake_ctx2
        assert fake_ctx2.check_hostname is False


# --- DAEMON ENTRY POINT ---

def test_main_entry_point_success():
    """Prueba la función main() real simulando todo el entorno."""
    with patch("yunbridge.daemon.load_runtime_config") as mock_load, \
            patch("yunbridge.daemon.configure_logging"), \
            patch("yunbridge.daemon.BridgeDaemon") as MockDaemon, \
            patch("yunbridge.daemon.asyncio.run") as mock_run, \
            patch("sys.exit") as mock_exit:

        # Configurar mocks
        mock_cfg = MagicMock()
        mock_cfg.serial_shared_secret = "safe"  # Evita warning critico
        mock_load.return_value = mock_cfg

        # Ejecutar main
        main()

        # Verificar flujo
        MockDaemon.assert_called_once()
        mock_run.assert_called_once()
        mock_exit.assert_called_with(0)


def test_main_entry_point_errors():
    """Prueba main() ante excepciones fatales."""
    with patch("yunbridge.daemon.load_runtime_config") as mock_load, \
            patch("yunbridge.daemon.configure_logging"), \
            patch("yunbridge.daemon.BridgeDaemon"), \
            patch("yunbridge.daemon.asyncio.run",
                  side_effect=RuntimeError("Boot fail")), \
            patch("sys.exit") as mock_exit:

        mock_load.return_value = MagicMock()

        main()

        # Debe salir con código 1
        mock_exit.assert_called_with(1)

"""
test_internals.py.

Objetivo: Ejecución quirúrgica de bucles internos y manejo de errores.
"""
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from mcubridge.transport import SerialTransport
from mcubridge.daemon import main
from mcubridge.rpc.protocol import Command
from mcubridge.rpc.frame import Frame
from cobs import cobs


# --- SERIAL TRANSPORT INTERNALS ---

# Obsolete test removed (read_loop replaced by Protocol)



# --- MQTT INTERNALS ---

@pytest.mark.asyncio
async def test_mqtt_internal_tls_setup_branches():
    """Prueba todas las ramas de configuración TLS (sin conectar)."""
    # Caso 1: TLS deshabilitado (Mock Config)
    mock_cfg = MagicMock()
    mock_cfg.tls_enabled = False

    # Accedemos a la función privada via import directo
    from mcubridge.transport.mqtt import _configure_tls
    assert _configure_tls(mock_cfg) is None

    # Caso 2: TLS habilitado sin cafile (usa trust store)
    mock_cfg.tls_enabled = True
    mock_cfg.mqtt_cafile = None
    mock_cfg.mqtt_tls_insecure = False
    mock_cfg.mqtt_certfile = None
    mock_cfg.mqtt_keyfile = None

    fake_ctx = MagicMock()
    fake_ctx.check_hostname = True

    with patch("mcubridge.transport.mqtt.ssl.create_default_context") as mk_ctx:
        mk_ctx.return_value = fake_ctx
        ctx = _configure_tls(mock_cfg)
        assert ctx is fake_ctx
        assert fake_ctx.check_hostname is True

    # Caso 3: cafile explícito pero inexistente (debe fallar)
    mock_cfg.mqtt_cafile = "/non/existent/ca.crt"

    with patch("mcubridge.transport.mqtt.Path") as MockPath:
        MockPath.return_value.exists.return_value = False
        with pytest.raises(RuntimeError, match="MQTT TLS CA file missing"):
            _configure_tls(mock_cfg)

    # Caso 4: mqtt_tls_insecure desactiva check_hostname
    mock_cfg.mqtt_cafile = None
    mock_cfg.mqtt_tls_insecure = True

    fake_ctx2 = MagicMock()
    fake_ctx2.check_hostname = True

    with patch("mcubridge.transport.mqtt.ssl.create_default_context") as mk_ctx2:
        mk_ctx2.return_value = fake_ctx2
        ctx2 = _configure_tls(mock_cfg)
        assert ctx2 is fake_ctx2
        assert fake_ctx2.check_hostname is False


# --- DAEMON ENTRY POINT ---

def test_main_entry_point_success():
    """Prueba la función main() real simulando todo el entorno."""
    with patch("mcubridge.daemon.load_runtime_config") as mock_load, \
            patch("mcubridge.daemon.configure_logging"), \
            patch("mcubridge.daemon.BridgeDaemon") as MockDaemon, \
            patch("mcubridge.daemon.asyncio.run") as mock_run, \
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
    with patch("mcubridge.daemon.load_runtime_config") as mock_load, \
            patch("mcubridge.daemon.configure_logging"), \
            patch("mcubridge.daemon.BridgeDaemon"), \
            patch("mcubridge.daemon.asyncio.run",
                  side_effect=RuntimeError("Boot fail")), \
            patch("sys.exit") as mock_exit:

        mock_load.return_value = MagicMock()

        main()

        # Debe salir con código 1
        mock_exit.assert_called_with(1)

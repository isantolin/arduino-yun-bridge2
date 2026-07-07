import pytest
from unittest.mock import MagicMock, patch
from typing import Any
from collections.abc import Coroutine
import importlib.util
from pathlib import Path
import sys
import io


def load_script(name: str) -> Any:
    # Use underscore version for filename lookup
    filename = name.replace("-", "_")
    script_path = Path(__file__).parent.parent / "scripts" / f"{filename}.py"
    spec = importlib.util.spec_from_file_location(filename, str(script_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {filename}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[filename] = module

    from unittest.mock import MagicMock

    sys.modules["uci"] = MagicMock()

    spec.loader.exec_module(module)
    return module


def mock_asyncio_run(coro: Coroutine[Any, Any, Any]) -> None:
    coro.close()


@pytest.mark.asyncio
async def test_file_push_script(runtime_config: Any) -> None:
    script = load_script("mcubridge-file-push")
    with (
        patch("mcubridge_file_push.load_runtime_config", return_value=runtime_config),
        patch("socket.socket") as mock_sock_cls,
        patch("sys.argv", ["mcubridge-file-push", "local.txt", "mcu/remote.txt"]),
        patch("pathlib.Path.read_bytes", return_value=b"data"),
        patch("pathlib.Path.exists", return_value=True),
    ):
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock
        script.main()
        mock_sock.connect.assert_called_once_with("/var/run/mcubridge.sock")


@pytest.mark.asyncio
async def test_led_control_script(runtime_config: Any) -> None:
    script = load_script("mcubridge-led-control")
    with (
        patch("mcubridge_led_control.load_runtime_config", return_value=runtime_config),
        patch("socket.socket") as mock_sock_cls,
        patch("sys.argv", ["mcubridge-led-control", "on"]),
    ):
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock
        script.main()
        mock_sock.connect.assert_called_once_with("/var/run/mcubridge.sock")


@pytest.mark.asyncio
async def test_rotate_credentials_script(runtime_config: Any) -> None:
    script = load_script("mcubridge-rotate-credentials")
    with (
        patch("sys.argv", ["mcubridge-rotate-credentials", "--force", "--no-restart"]),
        patch("subprocess.run"),
        patch("uci.Uci"),
        patch("mcubridge_rotate_credentials.update_uci_credentials") as mock_update,
        patch("sys.stdout", new_callable=io.StringIO) as stdout,
    ):
        script.main()
        assert mock_update.called
        output = stdout.getvalue()
        assert "SERIAL_SECRET=" in output
        assert "CLOUD_PASSWORD=" in output


@pytest.mark.asyncio
async def test_file_push_error_cases(runtime_config: Any) -> None:
    script = load_script("mcubridge-file-push")
    with (
        patch("mcubridge_file_push.load_runtime_config", return_value=runtime_config),
        patch("sys.argv", ["mcubridge-file-push", "nonexistent.txt", "mcu/remote.txt"]),
        patch("pathlib.Path.exists", return_value=False),
        pytest.raises(SystemExit),
    ):
        script.main()


@pytest.mark.asyncio
async def test_led_control_invalid_state(runtime_config: Any) -> None:
    script = load_script("mcubridge-led-control")
    with (
        patch("mcubridge_led_control.load_runtime_config", return_value=runtime_config),
        patch("sys.argv", ["mcubridge-led-control", "invalid"]),
        pytest.raises(SystemExit),
    ):
        script.main()


@pytest.mark.asyncio
async def test_rotate_credentials_abort(runtime_config: Any) -> None:
    script = load_script("mcubridge-rotate-credentials")
    with (
        patch("sys.argv", ["mcubridge-rotate-credentials"]),
        patch("sys.stdin.readline", return_value="n\n"),
        pytest.raises(SystemExit) as exc,
    ):
        script.main()
    assert exc.value.code == 0


def test_rotate_credentials_updates_expected_uci_keys() -> None:
    script = load_script("mcubridge-rotate-credentials")
    mock_cursor = MagicMock()
    with patch("uci.Uci", return_value=mock_cursor):
        script.update_uci_credentials("serial-secret", "mqtt-password")
    assert mock_cursor.set.call_args_list[0].args == ("mcubridge", "general", "serial_shared_secret", "serial-secret")
    assert mock_cursor.set.call_args_list[1].args == ("mcubridge", "general", "cloud_pass", "mqtt-password")

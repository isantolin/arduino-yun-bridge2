import pytest
from unittest.mock import AsyncMock, patch
from typing import Any, Coroutine
import importlib.util
from pathlib import Path
import sys


def load_script(name: str) -> Any:
    script_path = Path(__file__).parent.parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), str(script_path)
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name.replace("-", "_")] = module
    spec.loader.exec_module(module)
    return module


def mock_asyncio_run(coro: Coroutine[Any, Any, Any]) -> None:
    coro.close()


@pytest.mark.asyncio
async def test_file_push_script(runtime_config: Any) -> None:
    script = load_script("mcubridge-file-push")
    with (
        patch("mcubridge_file_push.load_runtime_config", return_value=runtime_config),
        patch("aiomqtt.Client") as mock_client_cls,
        patch("sys.argv", ["mcubridge-file-push", "local.txt", "mcu/remote.txt"]),
        patch("pathlib.Path.read_bytes", return_value=b"data"),
        patch("pathlib.Path.exists", return_value=True),
        patch("asyncio.run", side_effect=mock_asyncio_run),
    ):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        script.main()


@pytest.mark.asyncio
async def test_led_control_script(runtime_config: Any) -> None:
    script = load_script("mcubridge-led-control")
    with (
        patch("mcubridge_led_control.load_runtime_config", return_value=runtime_config),
        patch("aiomqtt.Client") as mock_client_cls,
        patch("sys.argv", ["mcubridge-led-control", "on"]),
        patch("asyncio.run", side_effect=mock_asyncio_run),
    ):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        script.main()


@pytest.mark.asyncio
async def test_rotate_credentials_script(runtime_config: Any) -> None:
    script = load_script("mcubridge-rotate-credentials")
    with (
        patch("aiomqtt.Client") as mock_client_cls,
        patch("sys.argv", ["mcubridge-rotate-credentials", "--force", "--no-restart"]),
        patch("subprocess.run"),
        patch("uci.Uci"),
        patch("mcubridge_rotate_credentials.update_uci_secret") as mock_update,
        patch("asyncio.run", side_effect=mock_asyncio_run),
    ):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        script.main()
        assert mock_update.called


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

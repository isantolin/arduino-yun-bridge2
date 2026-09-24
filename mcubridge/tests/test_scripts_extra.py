"""Unit tests for auxiliary scripts (SIL-2)."""

from __future__ import annotations

import importlib.util
from collections.abc import Coroutine
from pathlib import Path
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture


def load_script(name: str) -> Any:
    # Use underscore version for filename lookup
    norm_name = name.replace("-", "_")
    script_path = Path(__file__).parent.parent / "scripts" / f"{norm_name}.py"
    spec = importlib.util.spec_from_file_location(norm_name, str(script_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name.replace("-", "_")] = module

    from unittest.mock import MagicMock

    sys.modules["uci"] = MagicMock()

    spec.loader.exec_module(module)
    return module


def mock_asyncio_run(coro: Coroutine[Any, Any, Any]) -> None:
    coro.close()


def test_file_push_script(runtime_config: Any, mocker: MockerFixture) -> None:
    script = load_script("mcubridge-file-push")
    mock_push_ubus = mocker.patch("mcubridge_file_push.push_file_ubus", return_value=True)
    mocker.patch("sys.argv", ["mcubridge-file-push", "local.txt", "mcu/remote.txt"])
    mocker.patch("pathlib.Path.read_bytes", return_value=b"data")
    mocker.patch("pathlib.Path.exists", return_value=True)
    script.app(standalone_mode=False)
    mock_push_ubus.assert_called_once_with("mcu/remote.txt", b"data")


def test_rotate_credentials_script(
    runtime_config: Any, capsys: pytest.CaptureFixture[str], mocker: MockerFixture
) -> None:
    script = load_script("mcubridge-rotate-credentials")
    mocker.patch("sys.argv", ["mcubridge-rotate-credentials", "--force", "--no-restart"])
    mocker.patch("subprocess.run")
    mocker.patch("uci.Uci")
    mock_update = mocker.patch("mcubridge_rotate_credentials.update_uci_credentials")
    script.app(standalone_mode=False)
    assert mock_update.called
    captured = capsys.readouterr()
    assert "SERIAL_SECRET=" in captured.out
    assert "CLOUD_PASSWORD=" in captured.out


def test_file_push_error_cases(runtime_config: Any, mocker: MockerFixture) -> None:
    script = load_script("mcubridge-file-push")
    mocker.patch("sys.argv", ["mcubridge-file-push", "nonexistent.txt", "mcu/remote.txt"])
    mocker.patch("pathlib.Path.exists", return_value=False)
    with pytest.raises(SystemExit):
        script.app(standalone_mode=False)


def test_rotate_credentials_abort(runtime_config: Any, mocker: MockerFixture) -> None:
    script = load_script("mcubridge-rotate-credentials")
    mocker.patch("sys.argv", ["mcubridge-rotate-credentials"])
    mocker.patch("sys.stdin.readline", return_value="n\n")
    with pytest.raises(SystemExit) as exc:
        script.app(standalone_mode=False)
    assert exc.value.code == 0


def test_rotate_credentials_updates_expected_uci_keys(mocker: MockerFixture) -> None:
    script = load_script("mcubridge-rotate-credentials")
    mock_cursor = MagicMock()
    mocker.patch("uci.Uci", return_value=mock_cursor)
    script.update_uci_credentials("serial-secret", "cloud-password")
    assert mock_cursor.set.call_args_list[0].args == ("mcubridge", "general", "serial_shared_secret", "serial-secret")
    assert mock_cursor.set.call_args_list[1].args == ("mcubridge", "general", "cloud_pass", "cloud-password")

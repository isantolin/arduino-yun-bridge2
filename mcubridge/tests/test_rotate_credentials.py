"""Unit tests for mcubridge-rotate-credentials script (SIL-2)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture


def load_script(name: str) -> Any:
    norm_name = name.replace("-", "_")
    script_path = Path(__file__).parent.parent / "scripts" / f"{norm_name}.py"
    spec = importlib.util.spec_from_file_location(norm_name, str(script_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rotate_credentials_script(
    runtime_config: Any, capsys: pytest.CaptureFixture[str], mocker: MockerFixture
) -> None:
    script = load_script("mcubridge-rotate-credentials")
    mocker.patch("sys.argv", ["mcubridge-rotate-credentials", "--force", "--no-restart"])
    mock_cursor = MagicMock()
    mock_uci = MagicMock()
    mock_uci.Uci.return_value = mock_cursor
    script.uci = mock_uci
    script.app(standalone_mode=False)
    assert mock_cursor.commit.called
    captured = capsys.readouterr()
    assert "SERIAL_SECRET=" in captured.out
    assert "CLOUD_PASSWORD=" in captured.out


def test_rotate_credentials_abort(runtime_config: Any, mocker: MockerFixture) -> None:
    script = load_script("mcubridge-rotate-credentials")
    mocker.patch("sys.argv", ["mcubridge-rotate-credentials"])
    mocker.patch("sys.stdin.readline", return_value="n\n")
    with pytest.raises(SystemExit) as exc:
        script.app(standalone_mode=False)
    assert exc.value.code == 0


def test_rotate_credentials_updates_expected_uci_keys() -> None:
    script = load_script("mcubridge-rotate-credentials")
    mock_cursor = MagicMock()
    mock_uci = MagicMock()
    mock_uci.Uci.return_value = mock_cursor
    script.uci = mock_uci
    script.update_uci_credentials("serial-secret", "cloud-password")
    assert mock_cursor.set.call_args_list[0].args == ("mcubridge", "general", "serial_shared_secret", "serial-secret")
    assert mock_cursor.set.call_args_list[1].args == ("mcubridge", "general", "cloud_pass", "cloud-password")


def test_restart_service_ubus_success() -> None:
    script = load_script("mcubridge-rotate-credentials")
    mock_ubus = MagicMock()
    mock_conn = MagicMock()
    mock_ubus.connect.return_value = mock_conn
    script.ubus = mock_ubus

    script.restart_service()

    mock_ubus.connect.assert_called_once()
    mock_conn.call.assert_called_once_with("service", "restart", {"name": "mcubridge"})


def test_restart_service_ubus_error_raises() -> None:
    script = load_script("mcubridge-rotate-credentials")
    mock_ubus = MagicMock()
    mock_conn = MagicMock()
    mock_conn.call.side_effect = RuntimeError("ubus service call failed")
    mock_ubus.connect.return_value = mock_conn
    script.ubus = mock_ubus

    with pytest.raises(RuntimeError, match="ubus service call failed"):
        script.restart_service()

    mock_conn.call.assert_called_once_with("service", "restart", {"name": "mcubridge"})


def test_restart_service_ubus_none_raises() -> None:
    script = load_script("mcubridge-rotate-credentials")
    script.ubus = None

    with pytest.raises(RuntimeError, match="Native OpenWrt UBUS module unavailable"):
        script.restart_service()

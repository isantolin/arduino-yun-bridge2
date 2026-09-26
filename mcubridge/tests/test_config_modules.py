"""Tests for RuntimeConfig loader and utility functions."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcubridge.config import common, settings
from mcubridge.protocol import protocol
from pytest_mock import MockerFixture


def test_load_runtime_config_applies_env_and_defaults(
    mocker: MockerFixture,
):
    raw_config = {
        "serial_port": "/dev/custom",
        "serial_baud": 57600,
        "serial_safe_baud": 9600,
        "cloud_host": "broker",
        "cloud_port": 321,
        "cloud_user": " user ",
        "cloud_pass": " pass ",
        "cloud_tls": True,
        "cloud_cafile": " /etc/cafile ",
        "cloud_certfile": " ",
        "cloud_keyfile": "",
        "topic_prefix": " custom/topic ",
        "allowed_commands": "  ls  ECHO ls  ",
        "file_system_root": "/data",
        "allow_non_tmp_paths": True,
        "process_timeout": 60,
        "cloud_queue_limit": 1,
        "reconnect_delay": 7,
        "status_interval": 5,
        "console_queue_limit_bytes": 4096,
        "mailbox_queue_limit": 3,
        "mailbox_queue_bytes_limit": 512,
        "serial_retry_timeout": 0.5,
        "serial_response_timeout": 1.5,
        "serial_retry_attempts": 1,
        "serial_shared_secret": " envsecret ",
        "watchdog_enabled": True,
        "watchdog_interval": 0.5,
    }

    mocker.patch.object(settings, "_load_raw_config", return_value=(raw_config, "test"))

    config = settings.load_runtime_config()

    assert config.serial_port == "/dev/custom"
    assert config.serial_baud == 57600
    assert config.cloud_host == "broker"
    assert config.cloud_port == 321
    assert config.cloud_user == "user"
    assert config.cloud_pass == "pass"
    assert config.cloud_tls
    assert config.cloud_cafile == "/etc/cafile"
    assert config.cloud_certfile == ""
    assert config.cloud_keyfile == ""
    assert config.topic_prefix == "custom/topic"
    assert config.allowed_commands == ["echo", "ls"]
    assert config.file_system_root == "/data"
    assert config.process_timeout == 60
    assert config.cloud_queue_limit == 1
    assert config.reconnect_delay == 7
    assert config.status_interval == 5
    assert config.console_queue_limit_bytes == 4096
    assert config.mailbox_queue_limit == 3
    assert config.mailbox_queue_bytes_limit == 512
    assert config.serial_retry_timeout == 0.5
    assert config.serial_response_timeout == 1.5
    assert config.serial_retry_attempts == 1
    assert config.serial_shared_secret == b"envsecret"
    assert config.watchdog_enabled
    assert config.watchdog_interval == 0.5


def test_load_runtime_config_intervals(mocker: MockerFixture):
    raw_config = {
        "bridge_summary_interval": 10.5,
        "bridge_handshake_interval": 20.0,
    }
    mocker.patch.object(settings, "_load_raw_config", return_value=(raw_config, "test"))

    config = settings.load_runtime_config()
    assert config.bridge_summary_interval == 10.5
    assert config.bridge_handshake_interval == 20.0


def test_load_runtime_config_rejects_non_tmp_paths_when_disabled(
    mocker: MockerFixture,
):
    raw_config = {
        "cloud_spool_dir": "/var/spool/mcu",
        "file_system_root": "/var/lib/mcu",
        "allow_non_tmp_paths": False,
    }
    mocker.patch.object(settings, "_load_raw_config", return_value=(raw_config, "test"))

    # Strict validation should now raise ValueError during load_runtime_config in test mode

    with pytest.raises(ValueError):
        settings.load_runtime_config()


def test_load_runtime_config_allows_empty_cloud_user_value(
    mocker: MockerFixture,
):
    raw_config = {
        "cloud_user": "",
        "cloud_pass": " ",
    }
    mocker.patch.object(settings, "_load_raw_config", return_value=(raw_config, "test"))

    config = settings.load_runtime_config()
    assert config.cloud_user == ""
    assert config.cloud_pass == ""


def test_load_runtime_config_prefers_uci_config(mocker: MockerFixture):
    raw_config = {"serial_port": "/dev/uci"}
    mocker.patch.object(settings, "_load_raw_config", return_value=(raw_config, "uci"))

    config = settings.load_runtime_config()
    assert config.serial_port == "/dev/uci"


def test_load_runtime_config_falls_back_to_defaults(
    mocker: MockerFixture,
):
    def _uci_failure() -> dict[str, Any]:
        raise OSError("uci unavailable")

    mocker.patch.object(settings, "get_uci_config", side_effect=_uci_failure)

    # We must ensure get_default_config returns a valid config or convert will fail
    # Default is valid by definition.
    config = settings.load_runtime_config()
    from mcubridge.protocol import protocol

    assert config.serial_port == protocol.DEFAULT_SERIAL_PORT


def test_get_uci_config_flattens_nested_structures(mocker: MockerFixture):
    mocker.patch.object(
        settings,
        "get_uci_config",
        return_value={
            "allowed_commands": ["ls", "uptime"],
            "topic_prefix": "br",
        },
    )
    raw, _ = getattr(settings, "_load_raw_config")()
    assert raw["allowed_commands"] == ["ls", "uptime"]


def test_get_uci_config_handles_value_wrappers(mocker: MockerFixture):
    # Mocking UCI internal list handling
    mocker.patch.object(settings, "get_uci_config", return_value={"debug": True})
    config = settings.load_runtime_config()
    assert config.debug


def test_load_runtime_config_parses_watchdog(mocker: MockerFixture):
    raw_config = common.get_default_config()
    raw_config.update(
        {
            "serial_port": "/dev/ttyS1",
            "serial_baud": protocol.DEFAULT_BAUDRATE,
            "serial_safe_baud": protocol.DEFAULT_SAFE_BAUDRATE,
            "cloud_host": "broker",
            "cloud_port": 8883,
            "cloud_tls": True,
            "cloud_cafile": "/etc/ca.pem",
            "topic_prefix": "br",
            "allowed_commands": "uptime",
            "file_system_root": "/tmp/tests",
            "process_timeout": 10,
            "serial_shared_secret": " s_e_c_r_e_t_mock ",
            "watchdog_enabled": True,
            "watchdog_interval": 0.5,
        }
    )

    mocker.patch.object(settings, "_load_raw_config", return_value=(raw_config, "test"))

    config = settings.load_runtime_config()
    assert config.watchdog_enabled
    assert config.watchdog_interval == 0.5


def test_load_runtime_config_http3(mocker: MockerFixture):
    raw_config = {
        "cloud_http3_enabled": True,
        "cloud_http3_port": 8843,
        "cloud_http3_congestion_control": "cubic",
    }
    mocker.patch.object(settings, "_load_raw_config", return_value=(raw_config, "test"))

    config = settings.load_runtime_config()
    assert config.cloud_http3_enabled is True
    assert config.cloud_http3_port == 8843
    assert config.cloud_http3_congestion_control == "cubic"


def test_settings_factory_bypass_defaults(mocker: MockerFixture) -> None:
    mocker.patch("mcubridge.config.settings.validate_config")
    factory_fn = getattr(settings, "_runtime_config_factory")
    cfg = factory_fn(
        bypass_defaults=True,
        serial_shared_secret="secretstring",
        serial_port="/dev/ttyS0",
        serial_baud=115200,
        serial_safe_baud=115200,
    )
    assert cfg.serial_port == "/dev/ttyS0"
    assert isinstance(cfg.serial_shared_secret, bytes)


def test_settings_load_raw_config_empty_uci(mocker: MockerFixture) -> None:
    mocker.patch("mcubridge.config.settings.get_uci_config", return_value={})
    load_raw_fn = getattr(settings, "_load_raw_config")
    cfg_dict, source = load_raw_fn()
    assert source == "defaults"
    assert "serial_port" in cfg_dict


def test_settings_load_runtime_config_from_json_unknown_override() -> None:
    data = {"serial_port": "/dev/ttyACM0"}
    cfg = settings.load_runtime_config_from_json(
        data,
        overrides={"nonexistent_override_key": "ignored", "serial_baud": 230400},
    )
    assert cfg.serial_baud == 230400

    cfg_str = settings.load_runtime_config_from_json('{"serial_port": "/dev/ttyS1"}')
    assert cfg_str.serial_port == "/dev/ttyS1"


@given(
    cloud_en=st.sampled_from(["1", "true", "yes", "on", True]),
    wd_en=st.sampled_from(["0", "false", "no", "off", False]),
    baud=st.sampled_from(["9600", "115200", 9600, 115200]),
    interval=st.floats(min_value=0.5, max_value=60.0),
    secret_str=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=32),
)
def test_settings_normalize_config_property(
    cloud_en: Any, wd_en: Any, baud: Any, interval: float, secret_str: str
) -> None:
    norm_fn = getattr(settings, "_normalize_config_dict")
    norm, secret = norm_fn(
        {
            "cloud_enabled": cloud_en,
            "cloud_tls": cloud_en,
            "watchdog_enabled": wd_en,
            "serial_baud": baud,
            "bridge_summary_interval": interval,
            "topic_prefix": "br",
            "serial_shared_secret": secret_str,
            "allowed_commands": "cat ls",
            "cloud_allow_datastore": cloud_en,
            "unknown_extra_key": "val",
        }
    )
    assert norm["cloud_enabled"] is True
    assert norm["cloud_tls"] is True
    assert norm["watchdog_enabled"] is False
    assert norm["serial_baud"] == baud
    assert norm["bridge_summary_interval"] == interval
    assert norm["topic_prefix"] == "br"
    assert secret == secret_str.encode()
    assert norm["allowed_commands"] == ["cat", "ls"]
    assert norm["topic_authorization"]["datastore_get"] is True
    assert norm["topic_authorization"]["datastore_put"] is True
    assert norm["unknown_extra_key"] == "val"

    _, secret_none = norm_fn({"serial_shared_secret": None})
    assert secret_none is None


def test_logging_discover_syslog_and_none(runtime_config: settings.RuntimeConfig, mocker: MockerFixture) -> None:
    from pathlib import Path

    from mcubridge.config.logging import configure_logging

    def _mock_exists(path_obj: Path) -> bool:
        return str(path_obj) == "/var/run/log"

    mocker.patch.dict("os.environ", {}, clear=True)
    mocker.patch.object(Path, "exists", _mock_exists)
    mock_syslog = mocker.patch("mcubridge.config.logging.SysLogHandler")
    configure_logging(runtime_config)
    assert mock_syslog.called

    mocker.patch.object(Path, "exists", return_value=False)
    configure_logging(runtime_config)


def test_uci_edge_branches(mocker: MockerFixture) -> None:
    import builtins

    orig_import = builtins.__import__

    def mock_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "uci":
            raise ImportError("Mocked missing uci")
        return orig_import(name, *args, **kwargs)

    mocker.patch("builtins.__import__", side_effect=mock_import)
    result = common.get_uci_config()
    assert result == common.get_default_config()


def test_config_settings_and_logging_branches(runtime_config: settings.RuntimeConfig, mocker: MockerFixture) -> None:
    from pathlib import Path
    from mcubridge.config.logging import configure_logging
    from mcubridge.protocol import mcubridge_pb2 as pb

    # 1. _runtime_config_factory with pb_msg
    existing_msg = pb.RuntimeConfig(serial_port="/dev/test_factory")
    factory_fn = getattr(settings, "_runtime_config_factory")
    res_factory = factory_fn(pb_msg=existing_msg)
    assert res_factory.serial_port == "/dev/test_factory"

    # 2. load_runtime_config_from_json with dict and serial_shared_secret
    cfg_json = settings.load_runtime_config_from_json(
        {"serial_port": "/dev/ttyS0", "serial_shared_secret": b"secret_bytes_123"}
    )
    assert cfg_json.serial_shared_secret == b"secret_bytes_123"

    # 3. load_runtime_config_from_json with overrides containing secret
    cfg_overrides = settings.load_runtime_config_from_json(
        {"serial_port": "/dev/ttyS0"},
        overrides={"serial_shared_secret": b"overridden_secret"},
    )
    assert cfg_overrides.serial_shared_secret == b"overridden_secret"

    # 4. configure_logging when SysLogHandler fails with OSError triggers fallback StreamHandler (lines 88-89)
    mocker.patch.object(Path, "exists", return_value=True)
    mocker.patch("mcubridge.config.logging.SysLogHandler", side_effect=OSError("syslog unavailable"))
    configure_logging(runtime_config, console=False)

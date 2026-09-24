"""Test suite for Flash Protection mechanisms (SIL-2 Safety)."""

from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

from mcubridge.config import common
from mcubridge.config.settings import load_runtime_config


def test_file_system_root_must_be_volatile(mocker: MockerFixture) -> None:
    """Ensure file_system_root raises RuntimeError if not in volatile path."""
    unsafe_conf = common.get_default_config()
    unsafe_conf.update(
        {
            "file_system_root": "/mnt/flash/data",
            "allow_non_tmp_paths": False,
            "serial_shared_secret": "valid_secret_1234",
            "serial_baud": 57600,
            "serial_safe_baud": 9600,
            "serial_port": "/dev/ttyS0",
            "cloud_host": "localhost",
            "cloud_port": 1883,
            "topic_prefix": "bridge",
            "cloud_tls": True,
        }
    )
    mocker.patch("mcubridge.config.settings.get_uci_config", return_value=unsafe_conf)
    with pytest.raises(RuntimeError):
        load_runtime_config()


def test_cloud_spool_dir_must_be_volatile(mocker: MockerFixture) -> None:
    """Ensure cloud_spool_dir raises RuntimeError if not in volatile path."""
    unsafe_conf = common.get_default_config()
    unsafe_conf.update(
        {
            "cloud_spool_dir": "/mnt/flash/spool",
            "allow_non_tmp_paths": False,
            "serial_shared_secret": "valid_secret_1234",
            "serial_baud": 57600,
            "serial_safe_baud": 9600,
            "serial_port": "/dev/ttyS0",
            "cloud_host": "localhost",
            "cloud_port": 1883,
            "topic_prefix": "bridge",
            "cloud_tls": True,
        }
    )
    mocker.patch("mcubridge.config.settings.get_uci_config", return_value=unsafe_conf)
    with pytest.raises(RuntimeError):
        load_runtime_config()


def test_override_flag_allows_unsafe_fs_root(mocker: MockerFixture) -> None:
    """Ensure allow_non_tmp_paths=True bypasses check for file_system_root."""
    unsafe_conf = common.get_default_config()
    unsafe_conf.update(
        {
            "file_system_root": "/etc/custom",
            "allow_non_tmp_paths": True,
            "serial_shared_secret": "valid_secret_1234",
            "serial_baud": 57600,
            "serial_safe_baud": 9600,
            "serial_port": "/dev/ttyS0",
            "cloud_host": "localhost",
            "cloud_port": 1883,
            "topic_prefix": "bridge",
            "cloud_tls": True,
            "cloud_spool_dir": ".tmp_tests/spool",
        }
    )
    mocker.patch("mcubridge.config.settings.get_uci_config", return_value=unsafe_conf)
    config = load_runtime_config()
    assert config.file_system_root == "/etc/custom"

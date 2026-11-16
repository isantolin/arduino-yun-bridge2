"""Test suite for Flash Protection mechanisms (SIL-2 Safety)."""

from __future__ import annotations

import pytest
from mcubridge.config import common
from mcubridge.config.settings import load_runtime_config
from pytest_mock import MockerFixture


def test_file_system_root_must_be_in_approved_root(mocker: MockerFixture) -> None:
    """ADR-0007: file_system_root outside /srv/mcubridge, /mnt and RAM needs allow_non_tmp_paths."""
    unsafe_conf = common.get_default_config()
    unsafe_conf.update(
        {
            "file_system_root": "/etc/mcubridge-data",
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


@pytest.mark.parametrize(
    ("root", "accepted"),
    [
        ("/srv/mcubridge/files", True),
        ("/mnt/sda1/mcubridge", True),
        ("/tmp/mcubridge-files", True),
        ("/srv/mcubridge-evil", False),
        ("/srv/mcubridge/../../etc", False),
        ("/mntx/data", False),
        ("relative/path", False),
    ],
)
def test_persistent_storage_roots_are_confined(root: str, accepted: bool) -> None:
    """ADR-0007: approved roots are matched as whole path components, after normalization."""
    from mcubridge.protocol import protocol

    roots = protocol.VOLATILE_STORAGE_PATHS | protocol.PERSISTENT_STORAGE_PATHS
    assert protocol.path_within_roots(root, roots) is accepted


def test_default_file_system_root_is_persistent() -> None:
    """ADR-0007: the default storage root is persistent and accepted without allow_non_tmp_paths."""
    from mcubridge.protocol import protocol

    defaults = common.get_default_config()
    assert defaults["file_system_root"] == "/srv/mcubridge/files"
    assert defaults["file_storage_quota_bytes"] == 2 * 1024 * 1024
    assert protocol.RUNTIME_CONFIG_METADATA["file_system_root"].get("volatile") is False

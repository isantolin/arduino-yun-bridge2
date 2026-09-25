"""Tests for RuntimeConfig normalization and validation. [SIL-2]"""

from __future__ import annotations

from hypothesis import given, strategies as st
from pytest_mock import MockerFixture
import pytest

from mcubridge.config import settings as config_settings
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.structures import validate_config


def _valid_base_config() -> RuntimeConfig:
    cfg = RuntimeConfig()
    cfg.serial_port = "/dev/null"
    cfg.topic_prefix = "mcubridge"
    cfg.file_system_root = "/tmp/fs"
    cfg.cloud_spool_dir = "/tmp/spool"
    cfg.serial_shared_secret = b"secret1234"
    cfg.allow_non_tmp_paths = True
    return cfg


def test_runtime_config_topic_and_paths(mocker: MockerFixture) -> None:
    raw = {
        "serial_port": "/dev/null",
        "topic_prefix": "/demo//prefix/",
        "file_system_root": "/tmp/tests//bridge/test/..",
        "cloud_spool_dir": "/tmp/spool",
        "serial_shared_secret": b"secret1234",
        "allow_non_tmp_paths": True,
    }
    mocker.patch.object(config_settings, "_load_raw_config", return_value=(raw, "test"))
    config = config_settings.load_runtime_config()
    assert config.topic_prefix == "/demo//prefix/"
    assert config.file_system_root == "/tmp/tests/bridge"


def test_runtime_config_rejects_empty_topic(mocker: MockerFixture) -> None:
    raw = {
        "serial_port": "/dev/null",
        "topic_prefix": "//",
        "file_system_root": "/tmp/fs",
        "cloud_spool_dir": "/tmp/spool",
        "serial_shared_secret": b"secret1234",
        "allow_non_tmp_paths": True,
    }
    mocker.patch.object(config_settings, "_load_raw_config", return_value=(raw, "test"))
    with pytest.raises(ValueError, match=r"topic_prefix: does not match regex pattern"):
        config_settings.load_runtime_config()


def test_runtime_config_rejects_non_positive_status_interval() -> None:
    cfg = _valid_base_config()
    cfg.status_interval = 0
    with pytest.raises(ValueError, match="status_interval"):
        validate_config(cfg)


@given(interval=st.floats(min_value=-1000.0, max_value=0.49).filter(lambda x: not (x != x)))
def test_runtime_config_requires_watchdog_interval_when_enabled(interval: float) -> None:
    cfg = _valid_base_config()
    cfg.watchdog_enabled = True
    cfg.watchdog_interval = interval
    with pytest.raises(ValueError, match="watchdog_interval"):
        validate_config(cfg)


@given(port=st.one_of(st.just(0), st.integers(min_value=65536, max_value=200000)))
def test_runtime_config_rejects_invalid_cloud_port(port: int) -> None:
    cfg = _valid_base_config()
    cfg.cloud_port = port
    with pytest.raises(ValueError, match="cloud_port"):
        validate_config(cfg)

"""Exhaustive gap closure suite 20 for Python daemon SIL-2 coverage (95%+ target)."""

import os
from unittest.mock import MagicMock, patch

import pytest
from mcubridge.security.security import verify_crypto_integrity
from mcubridge.watchdog import WatchdogKeepalive
import mcubridge_client.env as env_mod
from mcubridge_client.definitions import build_bridge_args
from mcubridge_client.env import read_uci_general


@pytest.mark.asyncio
async def test_security_fips_kat_and_watchdog_no_state():
    # 1. verify_crypto_integrity
    res = verify_crypto_integrity()
    assert res is True

    # 2. WatchdogKeepalive kick without state
    wd = WatchdogKeepalive(write=MagicMock(), state=None)
    wd.kick()


@pytest.mark.asyncio
async def test_env_openwrt_and_uci_gaps():
    is_openwrt_fn = getattr(env_mod, "_is_openwrt")

    # 1. MCUBRIDGE_FORCE_UCI = 1 and openwrt_version path
    with patch.dict(os.environ, {"MCUBRIDGE_FORCE_UCI": "1"}):
        assert is_openwrt_fn() is True

    import pathlib

    def mock_path_exists(self_obj: object = None) -> bool:
        return True

    with (
        patch.dict(os.environ, {"MCUBRIDGE_FORCE_UCI": "0"}),
        patch.object(pathlib.PosixPath, "exists", side_effect=mock_path_exists),
    ):
        assert is_openwrt_fn() is True

    # 2. read_uci_general when find_spec returns None
    with (
        patch("mcubridge_client.env._is_openwrt", return_value=True),
        patch("importlib.util.find_spec", return_value=None),
    ):
        res1 = read_uci_general()
        assert res1 == {}

    # 3. read_uci_general when module has no get_uci_config
    mock_mod = MagicMock(spec=[])
    with (
        patch("mcubridge_client.env._is_openwrt", return_value=True),
        patch("importlib.import_module", return_value=mock_mod),
    ):
        res2 = read_uci_general()
        assert res2 == {}


@pytest.mark.asyncio
async def test_definitions_empty_prefix_and_logging_no_syslog():
    from mcubridge.config.logging import configure_logging
    from mcubridge.config.settings import load_runtime_config

    # 1. build_bridge_args with topic_prefix=""
    args = build_bridge_args(topic_prefix="")
    assert "topic_prefix" not in args

    # 2. configure_logging when both /dev/log and /var/run/log do NOT exist
    cfg = load_runtime_config()
    with patch("mcubridge.config.logging.Path.exists", return_value=False):
        configure_logging(cfg)

"""Shared test helpers — importable from any test module."""

from __future__ import annotations

import msgspec

from mcubridge.config.common import get_default_config
from mcubridge.config.settings import RuntimeConfig


def make_test_config(**overrides: object) -> RuntimeConfig:
    """Shared test config factory — avoids duplicated boilerplate across test modules."""
    raw = get_default_config()
    raw.update(
        serial_port="/dev/null",
        serial_shared_secret=b"s_e_c_r_e_t_mock",
    )
    raw.update(overrides)
    return msgspec.convert(raw, RuntimeConfig, strict=False)

"""Reusable OpenWrt python-ubus connection and response helpers. [SIL-2]"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any, cast

__all__ = ["ubus_connection", "unwrap_ubus_response"]


@contextlib.contextmanager
def ubus_connection(ubus_module: Any) -> Iterator[Any]:
    """Validate and open a native OpenWrt python-ubus connection."""
    if ubus_module is None:
        raise RuntimeError("Native OpenWrt UBUS module unavailable")
    if ubus_module.connect() is not True:
        raise OSError("ubus.connect() did not return True")
    yield ubus_module


def unwrap_ubus_response(response: Any) -> object:
    """Unwrap the single-element list convention returned by python-ubus.call()."""
    if isinstance(response, list) and response:
        return cast(list[object], response)[0]
    return cast(object, response)

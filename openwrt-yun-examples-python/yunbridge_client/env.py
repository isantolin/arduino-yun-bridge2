"""Helpers to introspect environment variables for the Yun bridge client."""
from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

_BROKER_ENV_VARS: tuple[str, ...] = (
    "YUN_BROKER_IP",
    "YUN_BROKER_PORT",
    "YUN_BROKER_USER",
    "YUN_BROKER_PASS",
)


def dump_client_env(logger: Optional[logging.Logger] = None) -> None:
    """Log the MQTT-related environment variables for quick diagnostics."""

    def _emit(message: str) -> None:
        if logger is not None:
            logger.info(message)
        else:
            print(message, flush=True)

    _emit("Yun Bridge client environment snapshot:")
    for key in _BROKER_ENV_VARS:
        value = os.environ.get(key)
        if value is None:
            _emit(f"  {key}=<unset>")
        else:
            _emit(f"  {key}='{value}'")


__all__: Iterable[str] = ("dump_client_env",)

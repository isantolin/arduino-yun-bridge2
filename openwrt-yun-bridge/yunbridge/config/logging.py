"""Logging helpers for Yun Bridge daemon."""
from __future__ import annotations

import logging

from .settings import RuntimeConfig


def configure_logging(config: RuntimeConfig) -> None:
    """Configure root logging based on runtime settings."""

    level = logging.DEBUG if config.debug_logging else logging.INFO
    fmt = (
        "%(asctime)s - %(name)s - %(levelname)s "
        "[%(filename)s:%(lineno)d] - %(message)s"
        if level == logging.DEBUG
        else "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    logging.basicConfig(level=level, format=fmt)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(fmt))

    logging.getLogger("yunbridge").info(
        "Logging configured at level %s", logging.getLevelName(level)
    )


"""Logging helpers for Yun Bridge daemon."""
from __future__ import annotations

import logging
from logging import Handler
from logging.handlers import SysLogHandler
from pathlib import Path

from .settings import RuntimeConfig

SYSLOG_SOCKET = Path("/dev/log")


class YunbridgeFormatter(logging.Formatter):
    """Formatter that strips the common 'yunbridge.' prefix."""

    PREFIX = "yunbridge."

    def format(self, record: logging.LogRecord) -> str:
        original_name = record.name
        if original_name.startswith(self.PREFIX):
            record.name = original_name[len(self.PREFIX):]
        try:
            return super().format(record)
        finally:
            record.name = original_name


def _build_handler(level: int, fmt: str) -> Handler:
    formatter = YunbridgeFormatter(fmt)
    if SYSLOG_SOCKET.exists():
        syslog_handler = SysLogHandler(
            address=str(SYSLOG_SOCKET),
            facility=SysLogHandler.LOG_DAEMON,
        )
        # Ensure messages are tagged as yunbridge within syslog
        syslog_handler.ident = "yunbridge "
        handler = syslog_handler
    else:
        handler = logging.StreamHandler()

    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


def configure_logging(config: RuntimeConfig) -> None:
    """Configure root logging based on runtime settings."""

    level = logging.DEBUG if config.debug_logging else logging.INFO
    # Match OpenWrt syslog style by keeping formatter minimal; syslog adds
    # timestamp, severity and process automatically.
    fmt = (
        "%(name)s %(levelname)s: %(message)s"
        if level == logging.DEBUG
        else "%(name)s: %(message)s"
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    root_logger.addHandler(_build_handler(level, fmt))

    logging.getLogger("yunbridge").info(
        "Logging configured at level %s", logging.getLevelName(level)
    )

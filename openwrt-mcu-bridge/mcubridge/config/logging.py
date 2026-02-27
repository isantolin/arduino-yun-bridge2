"""Logging configuration for MCU Bridge daemon using Rich."""

from __future__ import annotations

import logging
import os
from logging.config import dictConfig
from pathlib import Path
from typing import Any

from rich.logging import RichHandler

from .settings import RuntimeConfig

SYSLOG_SOCKET = Path("/dev/log")
SYSLOG_SOCKET_FALLBACK = Path("/var/run/log")


def configure_logging(config: RuntimeConfig) -> None:
    """Configure root logging using RichHandler for enhanced observability."""

    level = "DEBUG" if getattr(config, "debug_logging", False) else "INFO"
    force_stream = bool(os.environ.get("MCUBRIDGE_LOG_STREAM"))

    # Determine if we should use RichHandler (Console) or SysLog (Production)
    # On OpenWrt, we prefer SysLog for background daemon, but Rich for CLI/Debug.
    use_syslog = not force_stream and (SYSLOG_SOCKET.exists() or SYSLOG_SOCKET_FALLBACK.exists())

    handlers: dict[str, dict[str, Any]] = {
        "console": {
            "class": "rich.logging.RichHandler",
            "level": level,
            "rich_tracebacks": True,
            "markup": True,
            "show_path": level == "DEBUG",
        }
    }

    # Reference RichHandler to satisfy linters (F401)
    _ = RichHandler

    if use_syslog:
        socket_path = SYSLOG_SOCKET if SYSLOG_SOCKET.exists() else SYSLOG_SOCKET_FALLBACK
        handlers["syslog"] = {
            "class": "logging.handlers.SysLogHandler",
            "address": str(socket_path),
            "facility": "daemon",
            "level": level,
            "formatter": "syslog_fmt",
        }

    dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "minimal": {"format": "%(message)s"},
            "syslog_fmt": {
                "format": "mcubridge[%(process)d]: %(levelname)s %(name)s - %(message)s"
            },
        },
        "handlers": handlers,
        "root": {
            "level": level,
            "handlers": ["syslog" if use_syslog else "console"],
        },
    })

    logging.getLogger("mcubridge").info(
        "Logging established at level [bold]%s[/bold] (Mode: %s)",
        level,
        "SysLog" if use_syslog else "RichConsole"
    )

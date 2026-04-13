"""Modernized Logging configuration for MCU Bridge daemon (SIL-2)."""

from __future__ import annotations

import logging.config
import os
from pathlib import Path
from typing import Any

import structlog

from .settings import RuntimeConfig

SYSLOG_SOCKET = Path("/dev/log")
SYSLOG_SOCKET_FALLBACK = Path("/var/run/log")


def hexdump_processor(
    _: Any, __: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Format binary fields as standardized hex strings [DE AD BE EF]."""
    for key, value in event_dict.items():
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            event_dict[key] = f"[{raw.hex(' ').upper()}]" if raw else "[]"
    return event_dict


def configure_logging(config: RuntimeConfig) -> None:
    """Configure logging using declarative dictConfig and structlog native processors."""

    level = "DEBUG" if getattr(config, "debug_logging", False) else "INFO"
    force_stream = bool(os.environ.get("MCUBRIDGE_LOG_STREAM"))
    use_syslog = not force_stream and (
        SYSLOG_SOCKET.exists() or SYSLOG_SOCKET_FALLBACK.exists()
    )

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", key="ts"),
        structlog.processors.format_exc_info,
        structlog.stdlib.ExtraAdder(),
        hexdump_processor,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Declarative Logging Configuration
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": structlog.stdlib.ProcessorFormatter,
                "processors": [
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer() if use_syslog else structlog.dev.ConsoleRenderer(),
                ],
                "foreign_pre_chain": shared_processors,
            },
        },
        "handlers": {
            "default": {
                "level": level,
                "()": logging.handlers.SysLogHandler if use_syslog else logging.StreamHandler,
                "formatter": "default",
            },
        },
        "loggers": {
            "": {
                "handlers": ["default"],
                "level": level,
            },
        },
    }

    if use_syslog:
        socket_path = str(SYSLOG_SOCKET if SYSLOG_SOCKET.exists() else SYSLOG_SOCKET_FALLBACK)
        log_config["handlers"]["default"]["address"] = socket_path
        log_config["handlers"]["default"]["facility"] = logging.handlers.SysLogHandler.LOG_DAEMON

    logging.config.dictConfig(log_config)

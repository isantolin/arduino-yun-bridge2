"""Logging configuration for MCU Bridge daemon using structlog."""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any

import structlog

from .settings import RuntimeConfig

SYSLOG_SOCKET = Path("/dev/log")
SYSLOG_SOCKET_FALLBACK = Path("/var/run/log")


def hexdump_processor(_: Any, __: str, event_dict: structlog.types.EventDict) -> structlog.types.EventDict:
    """Format binary fields as standardized hex strings [DE AD BE EF]."""
    for key, value in event_dict.items():
        if isinstance(value, memoryview):
            raw = value.tobytes()
        elif isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
        else:
            continue
        event_dict[key] = f"[{raw.hex(' ').upper()}]" if raw else "[]"
    return event_dict


def configure_logging(config: RuntimeConfig) -> None:
    """Configure logging with structlog: JSON for syslog, colored for console."""

    level = "DEBUG" if getattr(config, "debug_logging", False) else "INFO"
    force_stream = bool(os.environ.get("MCUBRIDGE_LOG_STREAM"))
    use_syslog = not force_stream and (SYSLOG_SOCKET.exists() or SYSLOG_SOCKET_FALLBACK.exists())

    pre_chain: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", key="ts"),
        structlog.stdlib.ExtraAdder(),
        hexdump_processor,
    ]

    structlog.configure(
        processors=[*pre_chain, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    if use_syslog:
        socket_path = SYSLOG_SOCKET if SYSLOG_SOCKET.exists() else SYSLOG_SOCKET_FALLBACK
        renderer: Any = structlog.processors.JSONRenderer()
        handler: logging.Handler = logging.handlers.SysLogHandler(
            address=str(socket_path),
            facility=logging.handlers.SysLogHandler.LOG_DAEMON,
        )
    else:
        renderer = structlog.dev.ConsoleRenderer()
        handler = logging.StreamHandler()

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=pre_chain,
    )
    handler.setFormatter(formatter)
    handler.setLevel(level)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

"""Modernized Logging configuration for MCU Bridge daemon (SIL-2)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, cast

import structlog

from .settings import RuntimeConfig


def hexdump_processor(
    _: Any, __: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Format binary fields as standardized hex strings [DE AD BE EF]."""
    for key, value in event_dict.items():
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(cast(Any, value))
            event_dict[key] = f"[{raw.hex(' ').upper()}]" if raw else "[]"
    return event_dict


def configure_logging(config: RuntimeConfig) -> None:
    """Configure logging using structlog native processors and zero standard library overhead."""

    level = logging.DEBUG if getattr(config, "debug", False) else logging.INFO
    force_stream = bool(os.environ.get("MCUBRIDGE_LOG_STREAM"))

    # Check for syslog sockets
    syslog_socket = Path("/dev/log")
    syslog_fallback = Path("/var/run/log")
    use_syslog = not force_stream and (
        syslog_socket.exists() or syslog_fallback.exists()
    )

    # [SIL-2] Native processors for high-performance zero-wrapper logging
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.TimeStamper(fmt="iso", key="ts"),
        hexdump_processor,
    ]

    # Configure native structlog
    structlog.configure(
        processors=[
            *processors,
            (
                structlog.processors.JSONRenderer()
                if use_syslog
                else structlog.dev.ConsoleRenderer()
            ),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )

    # Minimal stdlib configuration to capture logs from third-party libraries (aiomqtt, etc.)
    # without wrapping our own loggers.
    logging.basicConfig(
        format="%(message)s",
        level=level,
        handlers=[logging.StreamHandler()],
    )

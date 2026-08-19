"""Modernized Logging configuration for MCU Bridge daemon (SIL-2)."""

from __future__ import annotations

import logging
from logging.handlers import SysLogHandler
import os
from pathlib import Path
from typing import Any, cast

import structlog

from .settings import RuntimeConfig


def hexdump_processor(_: Any, __: str, event_dict: structlog.types.EventDict) -> structlog.types.EventDict:
    """Format binary fields as standardized hex strings [DE AD BE EF]."""
    for key, value in event_dict.items():
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(cast(Any, value))
            event_dict[key] = f"[{raw.hex(' ').upper()}]" if raw else "[]"
    return event_dict


def configure_logging(
    config: RuntimeConfig | None = None,
    *,
    debug: bool | None = None,
    console: bool = False,
) -> None:
    """Configure structured logging with syslog-first transport and hex-safe payload rendering.

    Centralized Single Source of Truth for logging configuration across the ecosystem.
    """
    if debug is not None:
        is_debug = debug
    elif config is not None:
        is_debug = getattr(config, "debug", False)
    else:
        is_debug = os.environ.get("MCUBRIDGE_DEBUG", "").lower() in ("1", "true", "yes")

    level = logging.DEBUG if is_debug else logging.INFO
    force_stream = console or bool(os.environ.get("MCUBRIDGE_LOG_STREAM"))

    syslog_address: str | None = None
    if not force_stream:
        if Path("/dev/log").exists():
            syslog_address = "/dev/log"
        elif Path("/var/run/log").exists():
            syslog_address = "/var/run/log"

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
        structlog.processors.TimeStamper(fmt="iso", key="ts"),
        hexdump_processor,
    ]

    structlog.configure(
        processors=[
            *processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )

    renderer = (
        structlog.dev.ConsoleRenderer()
        if (force_stream and not syslog_address and console)
        else structlog.processors.JSONRenderer()
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=processors,
    )

    handler: logging.Handler
    if syslog_address:
        handler = SysLogHandler(address=syslog_address, facility=SysLogHandler.LOG_DAEMON)
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    for old_handler in root_logger.handlers:
        old_handler.close()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)


def reset_handlers() -> None:
    """Close and clear all handlers on the root logger."""
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        try:
            handler.close()
        except (OSError, RuntimeError):
            pass
        root_logger.removeHandler(handler)

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


def configure_logging(config: RuntimeConfig) -> None:
    """Configure structured logging with syslog-first transport and hex-safe payload rendering."""

    level = logging.DEBUG if getattr(config, "debug", False) else logging.INFO
    force_stream = bool(os.environ.get("MCUBRIDGE_LOG_STREAM"))

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

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
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

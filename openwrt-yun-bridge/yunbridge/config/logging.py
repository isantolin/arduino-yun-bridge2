"""Logging helpers for Yun Bridge daemon."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging import Handler
from logging.config import dictConfig
from logging.handlers import SysLogHandler
from pathlib import Path
from typing import Any

from .settings import RuntimeConfig

SYSLOG_SOCKET = Path("/dev/log")
SYSLOG_SOCKET_FALLBACK = Path("/var/run/log")

_RESERVED_LOG_KEYS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
}


def _serialise_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class StructuredLogFormatter(logging.Formatter):
    """Emit JSON per log line while trimming the shared prefix."""

    PREFIX = "yunbridge."

    def format(self, record: logging.LogRecord) -> str:
        logger_name = record.name
        if logger_name.startswith(self.PREFIX):
            logger_name = logger_name[len(self.PREFIX):]

        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": logger_name,
            "message": record.getMessage(),
        }

        extras = {
            key: _serialise_value(value)
            for key, value in record.__dict__.items()
            if key not in _RESERVED_LOG_KEYS and not key.startswith("_")
        }
        if extras:
            payload["extra"] = extras

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def _build_handler() -> Handler:
    candidates: tuple[Path, ...]
    if str(SYSLOG_SOCKET) == "/dev/log":
        candidates = (SYSLOG_SOCKET, SYSLOG_SOCKET_FALLBACK)
    else:
        candidates = (SYSLOG_SOCKET,)

    socket_path: Path | None = None
    for candidate in candidates:
        if candidate.exists():
            socket_path = candidate
            break

    if socket_path is not None:
        syslog_handler = SysLogHandler(
            address=str(socket_path),
            facility=SysLogHandler.LOG_DAEMON,
        )
        syslog_handler.ident = "yunbridge "
        return syslog_handler
    return logging.StreamHandler()


def configure_logging(config: RuntimeConfig) -> None:
    """Configure root logging based on runtime settings."""

    debug_logging = getattr(config, "debug_logging", False)
    level_name = "DEBUG" if debug_logging else "INFO"

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "structured": {
                    "()": "yunbridge.config.logging.StructuredLogFormatter",
                }
            },
            "handlers": {
                "yunbridge": {
                    "()": _build_handler,
                    "level": level_name,
                    "formatter": "structured",
                }
            },
            "root": {
                "level": level_name,
                "handlers": ["yunbridge"],
            },
        }
    )

    logging.getLogger("yunbridge").info("Logging configured at level %s", level_name)

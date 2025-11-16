"""This file is part of Arduino Yun Ecosystem v2.

Copyright (C) 2025 Ignacio Santolin and contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
import logging
from typing import Any, Dict, Iterable, cast

# --- Logger ---
logger = logging.getLogger(__name__)


def get_uci_config() -> Dict[str, str]:
    """Reads the yunbridge configuration from OpenWrt's UCI system.

    Prefers the python3-uci bindings (version 0.9.0 or compatible) that are
    provided as part of the OpenWrt feeds. Falling back to default values keeps
    the daemon functional on development hosts where the bindings are not
    available.

    Returns:
        A dictionary containing the configuration key-value pairs.
    """

    try:
        import uci  # type: ignore
    except ImportError:
        logger.warning(
            "python3-uci bindings unavailable; falling back to defaults."
        )
        return get_default_config()

    uci_exception = getattr(uci, "UciException", Exception)

    try:
        with uci.Uci() as cursor:  # type: ignore[attr-defined]
            cursor = cast(Any, cursor)
            section: Any = cursor.get_all("yunbridge", "general")
    except uci_exception as exc:  # type: ignore[misc]
        logger.warning(
            "Failed to load UCI configuration via python3-uci: %s",
            exc,
        )
        return get_default_config()
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception(
            "Unexpected error while reading UCI configuration: %s",
            exc,
        )
        return get_default_config()

    if not isinstance(section, dict) or not section:
        logger.warning(
            "python3-uci returned no options for 'yunbridge'; using defaults."
        )
        return get_default_config()

    return {
        str(key): _stringify_value(value)
        for key, value in cast(Dict[Any, Any], section).items()
    }


def _stringify_value(value: Any) -> str:
    """Convert UCI values (strings or tuples) to space-separated strings."""

    if isinstance(value, (tuple, list)):
        iterable_value = cast(Iterable[Any], value)
        return " ".join(str(item) for item in iterable_value)
    return str(value)


def get_default_config() -> Dict[str, str]:
    """Provides a default configuration."""
    return {
        "mqtt_host": "127.0.0.1",
        "mqtt_port": "1883",
        "serial_port": "/dev/ttyATH0",
        "serial_baud": "115200",
        "debug": "0",
        "allowed_commands": "",
        "file_system_root": "/root/yun_files",
        "process_timeout": "10",
        "console_queue_limit_bytes": "16384",
        "mailbox_queue_limit": "64",
        "mailbox_queue_bytes_limit": "65536",
        "mqtt_queue_limit": "256",
    }

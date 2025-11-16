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
    uci_not_found = getattr(uci, "UciExceptionNotFound", uci_exception)

    try:
        with uci.Uci() as cursor:  # type: ignore[attr-defined]
            cursor = cast(Any, cursor)
            sections: Any = cursor.get("yunbridge")
    except uci_not_found as exc:  # type: ignore[misc]
        logger.warning(
            "UCI section 'yunbridge' not found via python3-uci: %s", exc
        )
        return get_default_config()
    except uci_exception as exc:  # type: ignore[misc]
        logger.warning(
            "Failed to load UCI configuration via python3-uci: %s", exc
        )
        return get_default_config()
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception(
            "Unexpected error while reading UCI configuration: %s", exc
        )
        return get_default_config()

    config = _flatten_uci_sections(sections)
    if not config:
        logger.warning(
            "python3-uci returned no options for 'yunbridge'; using defaults."
        )
        return get_default_config()
    return config


def _flatten_uci_sections(sections: Any) -> Dict[str, str]:
    """Convert python3-uci return structure into a flat key/value mapping."""

    if not isinstance(sections, dict):
        return {}

    typed_sections: Dict[str, Any] = cast(Dict[str, Any], sections)
    config: Dict[str, str] = {}

    # Prioritise the common 'general' section; fall back to the first
    # dictionary payload if it does not exist.
    ordered_sections: Iterable[str]
    if "general" in typed_sections and isinstance(
        typed_sections["general"], dict
    ):
        ordered_sections = ("general",)
    else:
        ordered_sections = tuple(typed_sections.keys())

    for section_name in ordered_sections:
        section = typed_sections.get(section_name)
        if not isinstance(section, dict):
            continue
        _merge_section(config, cast(Dict[str, Any], section))

    # Include any remaining sections (if 'general' was processed first).
    for section_name, section_obj in typed_sections.items():
        if section_name in ordered_sections:
            continue
        if not isinstance(section_obj, dict):
            continue
        _merge_section(
            config,
            cast(Dict[str, Any], section_obj),
            overwrite=False,
        )

    return config


def _merge_section(
    config: Dict[str, str],
    section: Dict[str, Any],
    *,
    overwrite: bool = True,
) -> None:
    """Merge a section dictionary into the flat config map."""

    for key, value in section.items():
        if not overwrite and key in config:
            continue
        config[key] = _stringify_value(value)


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

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
import subprocess
import logging
from typing import Dict

# --- Logger ---
logger = logging.getLogger(__name__)


def get_uci_config() -> Dict[str, str]:
    """Reads the yunbridge configuration from OpenWrt's UCI system.

    Returns:
        A dictionary containing the configuration key-value pairs.
    """
    config: Dict[str, str] = {}
    try:
        # Use the 'uci show' command to get all values
        # for the 'yunbridge' section
        result = subprocess.run(
            ["uci", "show", "yunbridge"],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.strip().split('\n'):
            # Line format is typically "yunbridge.main.key='value'"
            if '=' in line:
                key_part, value_part = line.split('=', 1)
                # Extract the actual key name (e.g., 'mqtt_host')
                key = key_part.split('.')[-1]
                # Strip quotes from the value
                value = value_part.strip("'")
                config[key] = value
    except FileNotFoundError:
        logger.critical(
            "The 'uci' command was not found. "
            "This script appears to be running on a non-OpenWrt system "
            "or in an environment where 'uci' is not in the PATH. "
            "Falling back to default configuration."
        )
        return get_default_config()
    except subprocess.CalledProcessError as e:
        logger.warning(
            "Failed to execute 'uci show yunbridge': %s. "
            "This may happen if the yunbridge package "
            "is not installed correctly. "
            "Falling back to default configuration.",
            e,
        )
        return get_default_config()
    return config


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

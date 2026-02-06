"""MCU Bridge Package Initialisation."""

__version__ = "2.5.1"

import logging
import sys

import paho.mqtt.client

logger = logging.getLogger(__name__)


def _check_dependencies():
    """Verify runtime environment meets strict SIL-2 requirements."""
    # [SIL-2] STRICT DEPENDENCY CHECK
    # We require paho-mqtt 2.x because we use CallbackAPIVersion.VERSION2.
    if not hasattr(paho.mqtt.client, "CallbackAPIVersion"):
        logger.critical(
            "FATAL: Incompatible paho-mqtt version detected. "
            "This bridge requires paho-mqtt 2.x with CallbackAPIVersion support. "
            "Update your OpenWrt feeds or install python3-paho-mqtt >= 2.0.0."
        )
        sys.exit(1)


# Run checks on import to ensure fail-fast behavior
_check_dependencies()

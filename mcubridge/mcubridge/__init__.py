"""MCU Bridge Package Initialisation."""

__version__ = "2.8.1"

import importlib
import importlib.util
import logging
import sys

logger = logging.getLogger(__name__)


def _check_dependencies():
    """Verify runtime environment meets strict SIL-2 requirements."""
    if importlib.util.find_spec("paho.mqtt.client") is None:
        # Build-time tools or environments without MQTT can still use core packages.
        return

    mqtt_client = importlib.import_module("paho.mqtt.client")
    # [SIL-2] STRICT DEPENDENCY CHECK
    # We require paho-mqtt 2.x because we use CallbackAPIVersion.VERSION2.
    if not hasattr(mqtt_client, "CallbackAPIVersion"):
        logger.critical(
            "FATAL: Incompatible paho-mqtt version detected. "
            "This bridge requires paho-mqtt 2.x with CallbackAPIVersion support. "
            "Update your OpenWrt feeds or install python3-paho-mqtt >= 2.0.0."
        )
        sys.exit(1)
_check_dependencies()

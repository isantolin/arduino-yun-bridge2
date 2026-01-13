"""MCU Bridge Package Initialisation."""

__version__ = "2.1.0"

import logging
import sys

logger = logging.getLogger(__name__)


def _check_dependencies():
    """Verify runtime environment meets strict SIL-2 requirements."""
    try:
        import paho.mqtt.client as mqtt

        # [SIL-2] STRICT DEPENDENCY CHECK
        # We require paho-mqtt 2.x because we use CallbackAPIVersion.VERSION2.
        # Older versions (v1.6.x) common in OpenWrt stable feeds will cause
        # silent failures or attribute errors at runtime.
        if not hasattr(mqtt, "CallbackAPIVersion"):
            logger.critical(
                "FATAL: Incompatible paho-mqtt version detected. "
                "This bridge requires paho-mqtt 2.x with CallbackAPIVersion support. "
                "Update your OpenWrt feeds or install python3-paho-mqtt >= 2.0.0."
            )
            sys.exit(1)

    except ImportError:
        # If imports are missing entirely, Python will raise ImportError naturally later.
        pass


# Run checks on import to ensure fail-fast behavior
_check_dependencies()

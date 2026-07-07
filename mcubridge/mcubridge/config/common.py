"""Utility helpers shared across MCU Bridge packages."""

from __future__ import annotations

import structlog
from typing import Any, Final

logger = structlog.get_logger(__name__)

_UCI_PACKAGE: Final[str] = "mcubridge"
_UCI_SECTION: Final[str] = "general"


def get_uci_config() -> dict[str, Any]:
    """Fetch configuration from OpenWrt UCI system with safe fallbacks.

    [SIL-2] Tipado estricto de excepciones y aislamiento de fallos para garantizar
    la integridad del sistema de configuración.
    """
    try:
        import uci

        # [SIL-2] Dynamic class detection to handle library variations
        UciClass = getattr(uci, "Uci", None) or getattr(uci, "UCI", None)
        if UciClass is None:
            return get_default_config()

        cursor_obj = UciClass()

        with cursor_obj as cursor:
            # Verify it's a real cursor with get_all method
            if not hasattr(cursor, "get_all"):
                return get_default_config()
            section = cursor.get_all(_UCI_PACKAGE, _UCI_SECTION)
            if not section:
                return get_default_config()

            # Clean UCI dictionary (remove internal keys)
            return {str(k): v for k, v in section.items() if not str(k).startswith((".", "_"))}
    except ImportError:
        return get_default_config()
    except (RuntimeError, ValueError, OSError) as err:
        # [SIL-2] Log only specific configuration/system errors to syslog.
        logger.error("UCI system system error", error=err)

    return get_default_config()


def get_default_config() -> dict[str, Any]:
    """Return the complete default configuration as a dictionary (SIL 2)."""
    from mcubridge.protocol import mcubridge_pb2 as pb
    from mcubridge.protocol import protocol
    from mcubridge.config import const
    from google.protobuf import json_format

    msg = pb.RuntimeConfig(
        serial_port=const.DEFAULT_SERIAL_PORT,
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        cloud_host=const.DEFAULT_CLOUD_HOST,
        cloud_port=const.DEFAULT_CLOUD_PORT,
        cloud_tls=True,
        cloud_cafile=const.DEFAULT_CLOUD_CAFILE,
        topic_prefix=protocol.CLOUD_DEFAULT_TOPIC_PREFIX,
        allowed_commands=[],
        file_system_root=const.DEFAULT_FILE_SYSTEM_ROOT,
        process_timeout=const.DEFAULT_PROCESS_TIMEOUT,
        cloud_tls_insecure=const.DEFAULT_CLOUD_TLS_INSECURE,
        file_write_max_bytes=const.DEFAULT_FILE_WRITE_MAX_BYTES,
        file_storage_quota_bytes=const.DEFAULT_FILE_STORAGE_QUOTA_BYTES,
        cloud_queue_limit=const.DEFAULT_CLOUD_QUEUE_LIMIT,
        reconnect_delay=protocol.DEFAULT_RECONNECT_DELAY,
        status_interval=const.DEFAULT_STATUS_INTERVAL,
        debug=const.DEFAULT_DEBUG,
        console_queue_limit_bytes=protocol.DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
        mailbox_queue_limit=const.DEFAULT_MAILBOX_QUEUE_LIMIT,
        mailbox_queue_bytes_limit=const.DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
        pending_pin_request_limit=const.DEFAULT_PENDING_PIN_REQUESTS,
        serial_retry_timeout=const.DEFAULT_SERIAL_RETRY_TIMEOUT,
        serial_response_timeout=const.DEFAULT_SERIAL_RESPONSE_TIMEOUT,
        serial_retry_attempts=protocol.DEFAULT_RETRY_LIMIT,
        serial_fallback_threshold=protocol.DEFAULT_SERIAL_FALLBACK_THRESHOLD,
        serial_handshake_min_interval=const.DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
        serial_handshake_fatal_failures=protocol.DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
        cloud_enabled=True,
        watchdog_enabled=True,
        watchdog_interval=const.DEFAULT_WATCHDOG_INTERVAL,
        serial_shared_secret=const.DEFAULT_SERIAL_SHARED_SECRET,
        cloud_spool_dir=const.DEFAULT_CLOUD_SPOOL_DIR,
        process_max_output_bytes=protocol.DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
        process_max_concurrent=const.DEFAULT_PROCESS_MAX_CONCURRENT,
        metrics_enabled=const.DEFAULT_METRICS_ENABLED,
        metrics_host=const.DEFAULT_METRICS_HOST,
        metrics_port=protocol.PROMETHEUS_PORT,
        bridge_summary_interval=const.DEFAULT_BRIDGE_SUMMARY_INTERVAL,
        bridge_handshake_interval=const.DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
        allow_non_tmp_paths=const.DEFAULT_ALLOW_NON_TMP_PATHS,
    )
    return json_format.MessageToDict(msg, preserving_proto_field_name=True)


__all__: Final[tuple[str, ...]] = (
    "get_default_config",
    "get_uci_config",
)

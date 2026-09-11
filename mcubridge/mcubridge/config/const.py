"""Operational constants for MCU Bridge daemon components.

[SIL-2 COMPLIANCE - IEC 61508]
All protocol sizes, cryptographic parameters, and configuration defaults
MUST be imported directly from the canonical Single Source of Truth
(mcubridge.protocol.protocol / mcubridge.proto) per Rule 15.

This module retains ONLY internal daemon-specific operational parameters,
supervisor intervals, status codes, and telemetry keys.
"""

from __future__ import annotations

from ssl import TLSVersion
from typing import Final

from ..protocol import protocol

# -- Serial Operational Parameters --
SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT: Final[float] = 2.0
SERIAL_HANDSHAKE_BACKOFF_BASE: Final[float] = 1.0
SERIAL_HANDSHAKE_BACKOFF_MAX: Final[float] = 60.0
SERIAL_MIN_ACK_TIMEOUT: Final[float] = float(protocol.DEFAULT_ACK_TIMEOUT_MS) / 1000.0

# -- Cloud TLS & Spool Parameters --
CLOUD_TLS_MIN_VERSION: Final[TLSVersion] = TLSVersion.TLSv1_2
SPOOL_BACKOFF_MULTIPLIER: Final[float] = 5.0
SPOOL_BACKOFF_MIN_SECONDS: Final[float] = 5.0
SPOOL_BACKOFF_MAX_SECONDS: Final[float] = 60.0

# -- File System Operational Parameters --
MCU_FS_PREFIX: Final[str] = "mcu/"
VOLATILE_STORAGE_PATHS: frozenset[str] = frozenset({"/tmp", "/var/run", "/run", "/dev/shm"})
SYSTEMD_PRIVATE_PREFIX: Final[str] = "systemd-private-"

# -- Telemetry & Status Paths --
STATUS_FILE_PATH: Final[str] = "/tmp/mcubridge_status.json"
BRIDGE_SNAPSHOT_EXPIRY_SECONDS: Final[int] = 30

# -- Security Constraints --
MIN_SERIAL_SHARED_SECRET_LEN: Final[int] = 8
ALLOWED_COMMAND_WILDCARD: Final[str] = "*"
TOPIC_FORBIDDEN_REASON: Final[str] = "topic-action-forbidden"

# -- Hardware Watchdog Tokens --
WATCHDOG_TRIGGER_TOKEN: Final[bytes] = b"WATCHDOG=trigger\n"
WATCHDOG_MIN_INTERVAL: Final[float] = 0.5

# -- Task Supervisor (Internal Timing) --
SUPERVISOR_DEFAULT_RESTART_INTERVAL: Final[float] = 60.0
SUPERVISOR_DEFAULT_MIN_BACKOFF: Final[float] = 1.0
SUPERVISOR_DEFAULT_MAX_BACKOFF: Final[float] = 30.0
SUPERVISOR_STATUS_RESTART_INTERVAL: Final[float] = 120.0
SUPERVISOR_PROMETHEUS_RESTART_INTERVAL: Final[float] = 300.0
SUPERVISOR_STATUS_MAX_BACKOFF: Final[float] = 10.0
SUPERVISOR_MIN_RESTART_WINDOW: Final[float] = 1.0

# -- Telemetry and Metadata Property Keys --
PROP_KEY_BRIDGE_SPOOL: Final[str] = "bridge-spool"
PROP_KEY_BRIDGE_FILES: Final[str] = "bridge-files"
PROP_KEY_WATCHDOG_ENABLED: Final[str] = "bridge-watchdog-enabled"
PROP_KEY_WATCHDOG_INTERVAL: Final[str] = "bridge-watchdog-interval"
PROP_KEY_BRIDGE_ERROR: Final[str] = "bridge-error"
PROP_KEY_BRIDGE_PIN: Final[str] = "bridge-pin"
PROP_KEY_BRIDGE_STATUS: Final[str] = "bridge-status"
PROP_KEY_BRIDGE_DATASTORE_KEY: Final[str] = "bridge-datastore-key"
PROP_KEY_BRIDGE_EVENT: Final[str] = "bridge-event"
PROP_KEY_BRIDGE_SNAPSHOT: Final[str] = "bridge-snapshot"
PROP_KEY_BRIDGE_REQUEST_TOPIC: Final[str] = "bridge-request-topic"
PROP_KEY_BRIDGE_SNAPSHOTS: Final[str] = "bridge-snapshots"

PROP_VAL_UNKNOWN: Final[str] = "unknown"
PROP_VAL_QUOTA_BLOCKED: Final[str] = "quota-blocked"
PROP_VAL_WRITE_LIMIT: Final[str] = "write-limit"
PROP_VAL_ENABLED_TRUE: Final[str] = "1"
PROP_VAL_ENABLED_FALSE: Final[str] = "0"

# -- Async Control and Process Timeouts (Seconds) --
STREAM_POLL_TIMEOUT_SECONDS: Final[float] = 0.01
PROCESS_TERM_GRACE_PERIOD_SECONDS: Final[float] = 0.5
FLOW_CONTROL_WAIT_TIMEOUT_SECONDS: Final[float] = 30.0

# -- Status Codes (Application Logic) --
SERIAL_FAILURE_STATUS_CODES: Final[frozenset[int]] = frozenset(
    {
        protocol.Status.ERROR.value,
        protocol.Status.CMD_UNKNOWN.value,
        protocol.Status.MALFORMED.value,
        protocol.Status.CRC_MISMATCH.value,
        protocol.Status.TIMEOUT.value,
        protocol.Status.NOT_IMPLEMENTED.value,
    }
)
SERIAL_SUCCESS_STATUS_CODES: Final[frozenset[int]] = frozenset({protocol.Status.OK.value})

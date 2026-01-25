"""Utility helpers shared across MCU Bridge packages."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import random
from collections.abc import Iterable, Awaitable, Callable
from typing import (
    Final,
    TYPE_CHECKING,
    cast,
    TypeVar,
    ParamSpec,
    Any,
)

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from mcubridge.rpc import protocol


from .const import (
    ALLOWED_COMMAND_WILDCARD,
    DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
    DEFAULT_BRIDGE_SUMMARY_INTERVAL,
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_METRICS_HOST,
    DEFAULT_METRICS_PORT,
    DEFAULT_MQTT_CAFILE,
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_MQTT_SPOOL_DIR,
    DEFAULT_PENDING_PIN_REQUESTS,
    DEFAULT_PROCESS_MAX_CONCURRENT,
    DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
    DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
    DEFAULT_SERIAL_PORT,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_STATUS_INTERVAL,
)


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass

_TRUE_STRINGS: Final[frozenset[str]] = frozenset(
    {"1", "yes", "on", "true", "enable", "enabled"}
)
_UCI_PACKAGE: Final[str] = "mcubridge"
_UCI_SECTION: Final[str] = "general"

P = ParamSpec("P")
R = TypeVar("R")


class _BackoffCall:
    """Callable wrapper implementing retry logic."""
    def __init__(
        self,
        func: Callable[..., Awaitable[Any]],
        retries: int,
        start_delay: float,
        max_delay: float,
        factor: float,
        jitter: bool,
        exceptions: tuple[type[BaseException], ...],
    ) -> None:
        self.func = func
        self.retries = retries
        self.start_delay = start_delay
        self.max_delay = max_delay
        self.factor = factor
        self.jitter = jitter
        self.exceptions = exceptions
        functools.update_wrapper(self, func)

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        delay = self.start_delay
        attempt = 0

        while True:
            try:
                return await self.func(*args, **kwargs)
            except Exception as e:
                # Check for direct match
                is_match = isinstance(e, self.exceptions)

                # Check for ExceptionGroup match
                if not is_match and isinstance(e, ExceptionGroup):
                    matches, remainder = e.split(self.exceptions) # type: ignore
                    if matches and not remainder:
                        is_match = True
                    elif matches and remainder:
                        # Mixed group (fatal + non-fatal). Raise remainder.
                        raise e

                if not is_match:
                    raise

                attempt += 1
                if self.retries != -1 and attempt > self.retries:
                    logger.error(
                        "Backoff limit reached for %s after %d attempts. Last error: %s",
                        self.func.__name__, attempt, e # type: ignore
                    )
                    raise

                sleep_time = delay
                if self.jitter:
                    sleep_time = random.uniform(delay * 0.5, delay * 1.5)

                logger.warning(
                    "Retrying %s in %.2fs (attempt %d/%s). Error: %s",
                    self.func.__name__, sleep_time, attempt,
                    "inf" if self.retries == -1 else self.retries, e # type: ignore
                )

                await asyncio.sleep(sleep_time)
                delay = min(delay * self.factor, self.max_delay)


class backoff:
    """Decorator for exponential backoff retry logic.

    Args:
        retries: Maximum number of retries (default 3).
                 Use -1 for infinite retries (use with caution).
        start_delay: Initial delay in seconds.
        max_delay: Maximum delay cap in seconds.
        factor: Multiplier for exponential backoff.
        jitter: Add randomness to delay.
        exceptions: Tuple of exceptions to catch and retry on.
    """
    def __init__(
        self,
        retries: int = 3,
        start_delay: float = 0.1,
        max_delay: float = 5.0,
        factor: float = 2.0,
        jitter: bool = True,
        exceptions: tuple[type[BaseException], ...] = (Exception,),
    ) -> None:
        self.retries = retries
        self.start_delay = start_delay
        self.max_delay = max_delay
        self.factor = factor
        self.jitter = jitter
        self.exceptions = exceptions

    def __call__(
        self, func: Callable[P, Awaitable[R]]
    ) -> Callable[P, Awaitable[R]]:
        wrapper = _BackoffCall(
            func,
            self.retries,
            self.start_delay,
            self.max_delay,
            self.factor,
            self.jitter,
            self.exceptions,
        )
        return cast(Callable[P, Awaitable[R]], wrapper)


def parse_bool(value: object) -> bool:
    """Parse a boolean value safely from various types."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return False
    s = str(value).lower().strip()
    return s in _TRUE_STRINGS


def parse_int(value: object, default: int) -> int:
    """Parse an integer value safely, handling floats and strings."""
    try:
        return int(float(value))  # type: ignore
    except (ValueError, TypeError):
        return default


def parse_float(value: object, default: float) -> float:
    """Parse a float value safely."""
    try:
        return float(value)  # type: ignore
    except (ValueError, TypeError):
        return default


def normalise_allowed_commands(commands: Iterable[str]) -> tuple[str, ...]:
    """Return a deduplicated, lower-cased allow-list preserving wildcards."""
    seen: set[str] = set()
    normalised: list[str] = []
    for item in commands:
        candidate = item.strip()
        if not candidate:
            continue
        lowered = candidate.lower()
        if lowered == ALLOWED_COMMAND_WILDCARD:
            return (ALLOWED_COMMAND_WILDCARD,)
        if lowered in seen:
            continue
        seen.add(lowered)
        normalised.append(lowered)
    return tuple(normalised)


def encode_status_reason(reason: str | None) -> bytes:
    """Return a UTF-8 encoded payload trimming to MAX frame limits."""
    if not reason:
        return b""
    payload = reason.encode("utf-8", errors="ignore")
    return payload[: protocol.MAX_PAYLOAD_SIZE]


def build_mqtt_properties(message: Any) -> Properties | None:
    """Construct Paho MQTT v5 properties from a message object."""
    has_props = any(
        [
            message.content_type,
            message.payload_format_indicator is not None,
            message.message_expiry_interval is not None,
            message.response_topic,
            message.correlation_data is not None,
            message.user_properties,
        ]
    )

    if not has_props:
        return None

    props = Properties(PacketTypes.PUBLISH)

    if message.content_type is not None:
        props.ContentType = message.content_type

    if message.payload_format_indicator is not None:
        props.PayloadFormatIndicator = message.payload_format_indicator

    if message.message_expiry_interval is not None:
        props.MessageExpiryInterval = int(message.message_expiry_interval)

    if message.response_topic:
        props.ResponseTopic = message.response_topic

    if message.correlation_data is not None:
        props.CorrelationData = message.correlation_data

    if message.user_properties:
        props.UserProperty = list(message.user_properties)

    return props


def build_mqtt_connect_properties() -> Properties:
    """Return default CONNECT properties for aiomqtt/paho clients."""

    props = Properties(PacketTypes.CONNECT)
    props.SessionExpiryInterval = 0
    props.RequestResponseInformation = 1
    props.RequestProblemInformation = 1
    return props


def get_uci_config() -> dict[str, str]:
    """Read MCU Bridge configuration directly from OpenWrt's UCI system.

    [SIL-2] STRICT MODE: On OpenWrt, failure to load UCI is FATAL.
    """
    try:
        from uci import Uci
    except ImportError:
        Uci = None

    if Uci is not None:
        try:
            with Uci() as cursor:
                section = cursor.get_all(_UCI_PACKAGE, _UCI_SECTION)

                if not section:
                    # Detect OpenWrt environment
                    if os.path.exists("/etc/openwrt_release") or os.path.exists("/etc/openwrt_version"):
                        raise RuntimeError(
                            f"UCI section '{_UCI_PACKAGE}.{_UCI_SECTION}' missing! "
                            "Re-install package to restore defaults."
                        )

                    logger.warning("UCI section '%s.%s' not found; using defaults.", _UCI_PACKAGE, _UCI_SECTION)
                    return get_default_config()

                # Clean internal UCI metadata (keys starting with dot/underscore)
                clean_config = get_default_config()
                for k, v in section.items():
                    if k.startswith((".", "_")):
                        continue
                    if isinstance(v, (list, tuple)):
                        # Explicitly cast v to Iterable to help type checker
                        items = cast(Iterable[object], v)
                        clean_config[k] = " ".join(str(item) for item in items)
                    else:
                        clean_config[k] = str(v)

                return clean_config

        except (OSError, ValueError) as e:
            # Detect OpenWrt environment
            if os.path.exists("/etc/openwrt_release") or os.path.exists("/etc/openwrt_version"):
                logger.critical("Failed to load UCI configuration: %s", e)
                raise RuntimeError(f"Critical UCI failure: {e}") from e

            logger.error("Failed to load UCI configuration: %s. Using defaults.", e)
            return get_default_config()

    # Fallback when Uci is None
    if os.path.exists("/etc/openwrt_release") or os.path.exists("/etc/openwrt_version"):
        logger.critical("CRITICAL: Running on OpenWrt but 'python3-uci' is missing!")
        raise RuntimeError("Missing dependency: python3-uci")

    logger.warning("UCI module not found; using default configuration.")
    return get_default_config()


def get_default_config() -> dict[str, str]:
    """Provide default MCU Bridge configuration values."""
    from .const import DEFAULT_SERIAL_SHARED_SECRET

    default_secret = ""
    if DEFAULT_SERIAL_SHARED_SECRET is not None:
        default_secret = DEFAULT_SERIAL_SHARED_SECRET.decode("utf-8")

    return {
        "mqtt_host": DEFAULT_MQTT_HOST,
        "mqtt_port": str(DEFAULT_MQTT_PORT),
        "mqtt_tls": "1",
        "mqtt_tls_insecure": "0",
        "mqtt_cafile": DEFAULT_MQTT_CAFILE,
        "mqtt_certfile": "",
        "mqtt_keyfile": "",
        "mqtt_user": "",
        "mqtt_pass": "",
        "mqtt_topic": protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        "mqtt_spool_dir": DEFAULT_MQTT_SPOOL_DIR,
        "mqtt_queue_limit": str(DEFAULT_MQTT_QUEUE_LIMIT),
        "serial_port": DEFAULT_SERIAL_PORT,
        "serial_baud": str(protocol.DEFAULT_BAUDRATE),
        "serial_shared_secret": default_secret,
        "serial_retry_timeout": str(DEFAULT_SERIAL_RETRY_TIMEOUT),
        "serial_response_timeout": str(DEFAULT_SERIAL_RESPONSE_TIMEOUT),
        "serial_retry_attempts": str(protocol.DEFAULT_RETRY_LIMIT),
        "serial_handshake_min_interval": str(DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL),
        "serial_handshake_fatal_failures": str(DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES),
        "debug": "0",
        "allowed_commands": "",
        "file_system_root": DEFAULT_FILE_SYSTEM_ROOT,
        "process_timeout": str(DEFAULT_PROCESS_TIMEOUT),
        "process_max_output_bytes": str(DEFAULT_PROCESS_MAX_OUTPUT_BYTES),
        "process_max_concurrent": str(DEFAULT_PROCESS_MAX_CONCURRENT),
        "console_queue_limit_bytes": str(DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES),
        "mailbox_queue_limit": str(DEFAULT_MAILBOX_QUEUE_LIMIT),
        "mailbox_queue_bytes_limit": str(DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT),
        "pending_pin_request_limit": str(DEFAULT_PENDING_PIN_REQUESTS),
        "reconnect_delay": str(DEFAULT_RECONNECT_DELAY),
        "status_interval": str(DEFAULT_STATUS_INTERVAL),
        "bridge_summary_interval": str(DEFAULT_BRIDGE_SUMMARY_INTERVAL),
        "bridge_handshake_interval": str(DEFAULT_BRIDGE_HANDSHAKE_INTERVAL),
        "mqtt_allow_file_read": "1",
        "mqtt_allow_file_write": "1",
        "mqtt_allow_file_remove": "1",
        "mqtt_allow_datastore_get": "1",
        "mqtt_allow_datastore_put": "1",
        "mqtt_allow_mailbox_read": "1",
        "mqtt_allow_mailbox_write": "1",
        "mqtt_allow_shell_run": "1",
        "mqtt_allow_shell_run_async": "1",
        "mqtt_allow_shell_poll": "1",
        "mqtt_allow_shell_kill": "1",
        "mqtt_allow_console_input": "1",
        "mqtt_allow_digital_write": "1",
        "mqtt_allow_digital_read": "1",
        "mqtt_allow_digital_mode": "1",
        "mqtt_allow_analog_write": "1",
        "mqtt_allow_analog_read": "1",
        "metrics_enabled": "0",
        "metrics_host": DEFAULT_METRICS_HOST,
        "metrics_port": str(DEFAULT_METRICS_PORT),
        "watchdog_enabled": "1",
        "watchdog_interval": "5",
        "allow_non_tmp_paths": "0",
    }


__all__: Final[tuple[str, ...]] = (
    "backoff",
    "normalise_allowed_commands",
    "parse_bool",
    "parse_int",
    "parse_float",
    "encode_status_reason",
    "get_default_config",
    "get_uci_config",
    "build_mqtt_connect_properties",
    "build_mqtt_properties",
    "log_hexdump",
)


def log_hexdump(
    logger_instance: logging.Logger, level: int, label: str, data: bytes
) -> None:
    """Log binary data in hexadecimal format using syslog-friendly output.

    Format: [LABEL] LEN=10 HEX=00 01 02 ...
    """
    if not logger_instance.isEnabledFor(level):
        return

    hex_str = " ".join(f"{b:02X}" for b in data)
    logger_instance.log(level, "[%s] LEN=%d HEX=%s", label, len(data), hex_str)


def format_hexdump(data: bytes, prefix: str = "") -> str:
    """Return a multi-line canonical hexdump string."""
    if not data:
        return f"{prefix}<empty>"

    lines: list[str] = []
    for offset in range(0, len(data), 16):
        chunk = data[offset : offset + 16]
        hex_parts = [" ".join(f"{b:02X}" for b in chunk[i : i + 4]) for i in range(0, len(chunk), 4)]
        hex_str = "  ".join(hex_parts).ljust(47)
        ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{prefix}{offset:04X}  {hex_str}  |{ascii_str}|")
    return "\n".join(lines)

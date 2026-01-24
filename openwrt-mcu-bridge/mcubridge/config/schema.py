"""Marshmallow schema for RuntimeConfig validation."""

from __future__ import annotations

import os
from typing import Any, Dict

from marshmallow import Schema, fields, validate, post_load, validates_schema, ValidationError, pre_load

from ..const import (
    MIN_SERIAL_SHARED_SECRET_LEN,
    DEFAULT_MQTT_SPOOL_DIR,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_METRICS_HOST,
)
from ..policy import TopicAuthorization
from .model import RuntimeConfig


class TopicAuthorizationSchema(Schema):
    """Schema for granular topic authorization flags."""
    file_read = fields.Bool(load_default=True)
    file_write = fields.Bool(load_default=True)
    file_remove = fields.Bool(load_default=True)
    datastore_get = fields.Bool(load_default=True)
    datastore_put = fields.Bool(load_default=True)
    mailbox_read = fields.Bool(load_default=True)
    mailbox_write = fields.Bool(load_default=True)
    shell_run = fields.Bool(load_default=True)
    shell_run_async = fields.Bool(load_default=True)
    shell_poll = fields.Bool(load_default=True)
    shell_kill = fields.Bool(load_default=True)
    console_input = fields.Bool(load_default=True)
    digital_write = fields.Bool(load_default=True)
    digital_read = fields.Bool(load_default=True)
    digital_mode = fields.Bool(load_default=True)
    analog_write = fields.Bool(load_default=True)
    analog_read = fields.Bool(load_default=True)

    @post_load
    def make_authorization(self, data: Dict[str, Any], **kwargs: Any) -> TopicAuthorization:
        return TopicAuthorization(**data)


class RuntimeConfigSchema(Schema):
    """Declarative validation schema for MCU Bridge configuration."""

    # Serial
    serial_port = fields.Str(required=True, validate=validate.Length(min=1))
    serial_baud = fields.Int(required=True, validate=validate.Range(min=300))
    serial_safe_baud = fields.Int(load_default=0, validate=validate.Range(min=0))
    serial_shared_secret = fields.Raw(required=True)  # Validated in validates_schema
    serial_retry_timeout = fields.Float(load_default=1.0, validate=validate.Range(min=0.1))
    serial_response_timeout = fields.Float(load_default=2.0, validate=validate.Range(min=0.1))
    serial_retry_attempts = fields.Int(load_default=5, validate=validate.Range(min=1))
    serial_handshake_min_interval = fields.Float(load_default=1.0, validate=validate.Range(min=0.0))
    serial_handshake_fatal_failures = fields.Int(load_default=10, validate=validate.Range(min=1))

    # MQTT
    mqtt_host = fields.Str(load_default="localhost", validate=validate.Length(min=1))
    mqtt_port = fields.Int(load_default=1883, validate=validate.Range(min=1, max=65535))
    mqtt_user = fields.Str(load_default=None, allow_none=True)
    mqtt_pass = fields.Str(load_default=None, allow_none=True)
    mqtt_tls = fields.Bool(load_default=False)
    mqtt_tls_insecure = fields.Bool(load_default=False)
    mqtt_cafile = fields.Str(load_default=None, allow_none=True)
    mqtt_certfile = fields.Str(load_default=None, allow_none=True)
    mqtt_keyfile = fields.Str(load_default=None, allow_none=True)
    mqtt_topic = fields.Str(load_default="br", validate=validate.Length(min=1))
    mqtt_queue_limit = fields.Int(load_default=100, validate=validate.Range(min=1))
    mqtt_spool_dir = fields.Str(load_default=DEFAULT_MQTT_SPOOL_DIR)

    # Components
    file_system_root = fields.Str(load_default=DEFAULT_FILE_SYSTEM_ROOT)
    file_write_max_bytes = fields.Int(load_default=4096, validate=validate.Range(min=1))
    file_storage_quota_bytes = fields.Int(load_default=131072, validate=validate.Range(min=1))

    process_timeout = fields.Int(load_default=5, validate=validate.Range(min=1))
    process_max_output_bytes = fields.Int(load_default=4096, validate=validate.Range(min=1024))
    process_max_concurrent = fields.Int(load_default=3, validate=validate.Range(min=1))
    allowed_commands = fields.List(fields.Str(), load_default=tuple)

    console_queue_limit_bytes = fields.Int(load_default=1024, validate=validate.Range(min=1))

    mailbox_queue_limit = fields.Int(load_default=10, validate=validate.Range(min=1))
    mailbox_queue_bytes_limit = fields.Int(load_default=1024, validate=validate.Range(min=1))

    pending_pin_request_limit = fields.Int(load_default=5, validate=validate.Range(min=1))

    # System
    reconnect_delay = fields.Int(load_default=5, validate=validate.Range(min=1))
    status_interval = fields.Int(load_default=60, validate=validate.Range(min=1))
    debug_logging = fields.Bool(load_default=False)
    watchdog_enabled = fields.Bool(load_default=False)
    watchdog_interval = fields.Float(load_default=60.0, validate=validate.Range(min=0.5))

    metrics_enabled = fields.Bool(load_default=False)
    metrics_host = fields.Str(load_default=DEFAULT_METRICS_HOST)
    metrics_port = fields.Int(load_default=9130, validate=validate.Range(min=0, max=65535))

    bridge_summary_interval = fields.Float(load_default=60.0, validate=validate.Range(min=0.0))
    bridge_handshake_interval = fields.Float(load_default=300.0, validate=validate.Range(min=0.0))

    allow_non_tmp_paths = fields.Bool(load_default=False)

    topic_authorization = fields.Nested(TopicAuthorizationSchema, load_default=TopicAuthorization())

    @validates_schema
    def validate_serial_secret(self, data: Dict[str, Any], **kwargs: Any) -> None:
        secret = data.get("serial_shared_secret")
        if not secret:
            raise ValidationError("serial_shared_secret must be configured", field_name="serial_shared_secret")

        if isinstance(secret, str):
            secret = secret.encode("utf-8")
            data["serial_shared_secret"] = secret

        if len(secret) < MIN_SERIAL_SHARED_SECRET_LEN:
            raise ValidationError(
                f"serial_shared_secret must be at least {MIN_SERIAL_SHARED_SECRET_LEN} bytes",
                field_name="serial_shared_secret"
            )

        if secret == b"changeme123":
            raise ValidationError("serial_shared_secret placeholder is insecure", field_name="serial_shared_secret")

        unique_symbols = {byte for byte in secret}
        if len(unique_symbols) < 4:
            raise ValidationError(
                "serial_shared_secret must contain at least four distinct bytes",
                field_name="serial_shared_secret"
            )

    @validates_schema
    def validate_queue_consistency(self, data: Dict[str, Any], **kwargs: Any) -> None:
        if data["mailbox_queue_bytes_limit"] < data["mailbox_queue_limit"]:
            raise ValidationError(
                "mailbox_queue_bytes_limit must be greater than or equal to mailbox_queue_limit",
                field_name="mailbox_queue_bytes_limit"
            )

    @validates_schema
    def validate_storage_consistency(self, data: Dict[str, Any], **kwargs: Any) -> None:
        if data["file_storage_quota_bytes"] < data["file_write_max_bytes"]:
            raise ValidationError(
                "file_storage_quota_bytes must be greater than or equal to file_write_max_bytes",
                field_name="file_storage_quota_bytes"
            )

    @validates_schema
    def validate_flash_protection(self, data: Dict[str, Any], **kwargs: Any) -> None:
        """[SIL-2] Enforce Flash Wear Protection."""
        root = self._normalize_path(data.get("file_system_root", ""))
        spool = self._normalize_path(data.get("mqtt_spool_dir", ""))

        # Spool MUST be in RAM (volatile) always to prevent rapid flash wear
        if not spool.startswith("/tmp"):
            raise ValidationError(
                f"FLASH PROTECTION: mqtt_spool_dir '{spool}' is not in /tmp.",
                field_name="mqtt_spool_dir"
            )

        # Filesystem root can be overridden
        if data.get("allow_non_tmp_paths"):
            return

        if not root.startswith("/tmp"):
            raise ValidationError(
                f"FLASH PROTECTION: file_system_root '{root}' is not in /tmp. "
                "Set 'allow_non_tmp_paths' to '1' if persistent storage is required.",
                field_name="file_system_root"
            )

    @staticmethod
    def _normalize_path(value: str) -> str:
        candidate = (value or "").strip()
        expanded = os.path.expanduser(candidate)
        return os.path.abspath(expanded)

    @pre_load
    def normalize_topic(self, data: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        if "mqtt_topic" in data:
            prefix = data["mqtt_topic"]
            segments = [segment for segment in prefix.split("/") if segment]
            normalized = "/".join(segments)
            # If normalized is empty string, set it back to data so validation fails (min=1)
            # instead of falling back to default.
            data["mqtt_topic"] = normalized
        return data

    @post_load
    def make_config(self, data: Dict[str, Any], **kwargs: Any) -> RuntimeConfig:
        # Serial secret needs to be bytes
        secret = data.get("serial_shared_secret")
        if isinstance(secret, str):
            data["serial_shared_secret"] = secret.encode("utf-8")

        # Ensure paths are normalized (post-validation)
        data["file_system_root"] = self._normalize_path(data["file_system_root"])
        data["mqtt_spool_dir"] = self._normalize_path(data["mqtt_spool_dir"])

        # Ensure allowed_commands is tuple
        if "allowed_commands" in data:
            data["allowed_commands"] = tuple(data["allowed_commands"])

        # Operational adjustments
        # serial_response_timeout = max(..., serial_retry_timeout * 2) logic
        # is better kept in schema or moved to model?
        # Moving operational clamp logic here to keep model pure data
        data["serial_response_timeout"] = max(
            data["serial_response_timeout"],
            data["serial_retry_timeout"] * 2
        )

        return RuntimeConfig(**data)

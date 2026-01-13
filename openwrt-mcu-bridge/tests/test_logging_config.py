"""Tests for the logging configuration."""

import json
import logging
from unittest.mock import patch

from mcubridge.config import logging as log_mod
from mcubridge.config.settings import RuntimeConfig


def test_serialise_value_handles_bytes_and_objects() -> None:
    record = logging.LogRecord(
        name="mcubridge.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.custom_bytes = b"caf\xc3\xa9"  # type: ignore
    record.custom_obj = object()  # type: ignore

    formatter = log_mod.StructuredLogFormatter()
    payload = json.loads(formatter.format(record))

    assert payload["logger"] == "test"
    assert payload["message"] == "hello"
    # [SIL-2] Updated expectation: bytes are now hex-formatted for safety/clarity
    # "café" in UTF-8 is 0x63 0x61 0x66 0xC3 0xA9
    assert payload["extra"]["custom_bytes"] == "[63 61 66 C3 A9]"
    assert str(record.custom_obj) in payload["extra"]["custom_obj"]


def test_configure_logging_syslog(tmp_path) -> None:
    fake_socket = tmp_path / "log"
    fake_socket.touch()

    config = RuntimeConfig(
        serial_port="/dev/ttyACM0",
        serial_baud=115200,
        serial_safe_baud=115200,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=True,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="arduino",
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=5,
        serial_shared_secret=b"valid_secret_1234",
    )

    with patch("mcubridge.config.logging.SYSLOG_SOCKET", fake_socket):
        with patch("logging.config.dictConfig") as mock_dict_config:
            log_mod.configure_logging(config)
            mock_dict_config.assert_called_once()
            config_arg = mock_dict_config.call_args[0][0]
            assert "handlers" in config_arg
            assert "mcubridge" in config_arg["handlers"]

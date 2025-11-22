import logging
from pathlib import Path
from typing import Any, Dict

import pytest

from yunbridge.config import logging as logging_module
from yunbridge.config import settings
from yunbridge.const import DEFAULT_SERIAL_SHARED_SECRET


@pytest.fixture(autouse=True)
def _stub_credentials_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "load_credentials_file", lambda _: {})


def _runtime_config_kwargs(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "serial_port": "/dev/ttyUSB0",
        "serial_baud": 9600,
        "mqtt_host": "localhost",
        "mqtt_port": 1883,
        "mqtt_user": None,
        "mqtt_pass": None,
        "mqtt_tls": True,
        "mqtt_cafile": "/etc/ssl/certs/ca-certificates.crt",
        "mqtt_certfile": None,
        "mqtt_keyfile": None,
        "mqtt_topic": "yunbridge",
        "allowed_commands": ("ls",),
        "file_system_root": "/tmp",
        "process_timeout": 30,
        "debug_logging": False,
        "serial_shared_secret": b"secure_secret_123",
    }
    base.update(overrides)
    return base


def test_load_runtime_config_applies_env_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("YUNBRIDGE_DEBUG", "1")
    monkeypatch.setenv("YUNBRIDGE_WATCHDOG_INTERVAL", "0.2")
    monkeypatch.delenv("PROCD_WATCHDOG", raising=False)

    raw_config = {
        "serial_port": "/dev/custom",
        "serial_baud": "57600",
        "mqtt_host": "broker",
        "mqtt_port": "321",
        "mqtt_user": " user ",
        "mqtt_pass": " pass ",
        "mqtt_tls": "1",
        "mqtt_cafile": " /etc/cafile ",
        "mqtt_certfile": " ",
        "mqtt_keyfile": "",
        "mqtt_topic": " custom/topic ",
        "allowed_commands": "  ls  ECHO ls  ",
        "file_system_root": "/data",
        "process_timeout": "60",
        "mqtt_queue_limit": "0",
        "reconnect_delay": "7",
        "status_interval": "5",
        "console_queue_limit_bytes": "4096",
        "mailbox_queue_limit": "3",
        "mailbox_queue_bytes_limit": "512",
        "serial_retry_timeout": "1.5",
        "serial_response_timeout": "1.0",
        "serial_retry_attempts": "0",
        "serial_shared_secret": " envsecret ",
    }

    monkeypatch.setattr(settings, "_load_raw_config", lambda: raw_config)

    config = settings.load_runtime_config()

    assert config.serial_port == "/dev/custom"
    assert config.serial_baud == 57600
    assert config.mqtt_host == "broker"
    assert config.mqtt_port == 321
    assert config.mqtt_user == "user"
    assert config.mqtt_pass == "pass"
    assert config.mqtt_tls is True
    assert config.mqtt_cafile == "/etc/cafile"
    assert config.mqtt_certfile is None
    assert config.mqtt_keyfile is None
    assert config.mqtt_topic == " custom/topic "
    assert config.allowed_commands == ("ls", "echo")
    assert config.allowed_policy.is_allowed("ls --help")
    assert config.file_system_root == "/data"
    assert config.process_timeout == 60
    assert config.mqtt_queue_limit == 1
    assert config.reconnect_delay == 7
    assert config.status_interval == 5
    assert config.debug_logging is True
    assert config.console_queue_limit_bytes == 4096
    assert config.mailbox_queue_limit == 3
    assert config.mailbox_queue_bytes_limit == 512
    assert config.serial_retry_timeout == 1.5
    assert config.serial_response_timeout == 3.0
    assert config.serial_retry_attempts == 1
    assert config.watchdog_enabled is True
    assert config.watchdog_interval == 0.5
    assert config.tls_enabled is True
    assert config.serial_shared_secret == b"envsecret"


def test_load_runtime_config_prefers_credentials_file(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("YUNBRIDGE_DEBUG", raising=False)
    monkeypatch.delenv("YUNBRIDGE_SERIAL_SECRET", raising=False)
    monkeypatch.setenv("YUNBRIDGE_CREDENTIALS_FILE", "/tmp/credfile")

    raw_config = {
        "serial_port": "/dev/cred",
        "serial_baud": "115200",
        "mqtt_tls": "1",
        "mqtt_host": "broker",
        "serial_shared_secret": " ",
        "mqtt_user": " ",
        "mqtt_pass": None,
        "mqtt_cafile": None,
    }

    credentials = {
        "serial_shared_secret": "fromfile",
        "YUNBRIDGE_MQTT_USER": "user_file",
        "YUNBRIDGE_MQTT_PASS": "pass_file",
        "YUNBRIDGE_MQTT_CAFILE": "/etc/cafile",
    }

    monkeypatch.setattr(settings, "_load_raw_config", lambda: raw_config)
    monkeypatch.setattr(
        settings, "load_credentials_file", lambda _: credentials
    )

    config = settings.load_runtime_config()

    assert config.serial_shared_secret == b"fromfile"
    assert config.mqtt_user == "user_file"
    assert config.mqtt_pass == "pass_file"
    assert config.mqtt_cafile == "/etc/cafile"
    assert config.credentials_file == "/tmp/credfile"


def test_load_runtime_config_prefers_uci_config(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("YUNBRIDGE_DEBUG", raising=False)
    monkeypatch.delenv("YUNBRIDGE_WATCHDOG_INTERVAL", raising=False)
    monkeypatch.delenv("PROCD_WATCHDOG", raising=False)

    uci_config = {
        "serial_port": "/dev/uci",
        "debug": "1",
        "mqtt_tls": "0",
    }

    monkeypatch.setattr(settings, "get_uci_config", lambda: uci_config)

    def _unexpected_default() -> dict[str, str]:
        raise AssertionError("default config should not be used")

    monkeypatch.setattr(settings, "get_default_config", _unexpected_default)

    with pytest.raises(ValueError, match="MQTT TLS must be enabled"):
        settings.load_runtime_config()


def test_load_runtime_config_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
):
    def _uci_failure() -> dict[str, str]:
        raise RuntimeError("uci unavailable")

    default_config = {
        "serial_port": "/dev/default",
        "serial_baud": "not-int",
        "mqtt_tls": "1",
        "mqtt_user": "  ",
        "mqtt_pass": "",
        "mqtt_cafile": "/etc/cafile",
        "mqtt_certfile": " ",
        "mqtt_keyfile": None,
        "mqtt_queue_limit": "-1",
        "serial_retry_timeout": "bad",
        "serial_response_timeout": "0.1",
        "serial_retry_attempts": "0",
        "allowed_commands": "* ",
        "serial_shared_secret": " defaultsecret ",
    }

    monkeypatch.setattr(settings, "get_uci_config", _uci_failure)
    monkeypatch.setattr(settings, "get_default_config", lambda: default_config)
    monkeypatch.delenv("YUNBRIDGE_WATCHDOG_INTERVAL", raising=False)
    monkeypatch.setenv("PROCD_WATCHDOG", "4000")

    config = settings.load_runtime_config()

    assert config.serial_port == "/dev/default"
    assert config.serial_baud == settings.DEFAULT_SERIAL_BAUD
    assert config.mqtt_tls is True
    assert config.mqtt_user is None
    assert config.mqtt_pass is None
    assert config.mqtt_cafile == "/etc/cafile"
    assert config.mqtt_certfile is None
    assert config.mqtt_keyfile is None
    assert config.mqtt_queue_limit == 1
    assert config.serial_retry_timeout == settings.DEFAULT_SERIAL_RETRY_TIMEOUT
    assert config.serial_response_timeout == (
        settings.DEFAULT_SERIAL_RETRY_TIMEOUT * 2
    )
    assert config.serial_retry_attempts == 1
    assert config.allowed_policy.allow_all is True
    assert config.watchdog_enabled is True
    assert config.watchdog_interval == 2.0
    assert config.serial_shared_secret == b"defaultsecret"


def test_resolve_watchdog_settings_uses_procd(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("YUNBRIDGE_WATCHDOG_INTERVAL", raising=False)
    monkeypatch.setenv("PROCD_WATCHDOG", "10000")

    enabled, interval = settings._resolve_watchdog_settings()

    assert enabled is True
    assert interval == settings.DEFAULT_WATCHDOG_INTERVAL


def test_configure_logging_stream_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    missing_socket = tmp_path / "no-syslog"
    monkeypatch.setattr(logging_module, "SYSLOG_SOCKET", missing_socket)

    config = settings.RuntimeConfig(
        **_runtime_config_kwargs(serial_shared_secret=b"testshared")
    )

    logging_module.configure_logging(config)

    root_logger = logging.getLogger()
    assert len(root_logger.handlers) == 1

    handler = root_logger.handlers[0]
    assert handler.level == logging.INFO
    assert isinstance(handler.formatter, logging_module.YunbridgeFormatter)
    assert handler.formatter._fmt == "%(name)s: %(message)s"

    record = logging.LogRecord(
        name="yunbridge.example",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )

    try:
        formatted = handler.format(record)
        assert formatted == "example: hello world"
    finally:
        root_logger.handlers.clear()


def test_runtime_config_rejects_placeholder_serial_secret() -> None:
    kwargs = _runtime_config_kwargs(
        serial_shared_secret=DEFAULT_SERIAL_SHARED_SECRET
    )
    with pytest.raises(ValueError, match="placeholder"):
        settings.RuntimeConfig(**kwargs)


def test_runtime_config_rejects_low_entropy_serial_secret() -> None:
    kwargs = _runtime_config_kwargs(serial_shared_secret=b"aaaaaaaa")
    with pytest.raises(ValueError, match="four distinct"):
        settings.RuntimeConfig(**kwargs)


def test_configure_logging_syslog_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    socket_path = tmp_path / "devlog"
    socket_path.touch()

    class DummySysLogHandler(logging.Handler):
        LOG_DAEMON = object()

        def __init__(self, *, address: str, facility: object):
            super().__init__()
            self.address = address
            self.facility = facility
            self.ident: str | None = None

        def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
            pass

    monkeypatch.setattr(logging_module, "SYSLOG_SOCKET", socket_path)
    monkeypatch.setattr(logging_module, "SysLogHandler", DummySysLogHandler)

    config = settings.RuntimeConfig(
        serial_port="/dev/ttyUSB0",
        serial_baud=9600,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=True,
        mqtt_cafile="/etc/ssl/certs/ca-certificates.crt",
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="yunbridge",
        allowed_commands=("ls",),
        file_system_root="/tmp",
        process_timeout=30,
        debug_logging=True,
        serial_shared_secret=b"testshared",
    )

    logging_module.configure_logging(config)

    handler = logging.getLogger().handlers[0]

    assert isinstance(handler, DummySysLogHandler)
    assert handler.address == str(socket_path)
    assert handler.facility is DummySysLogHandler.LOG_DAEMON
    assert handler.level == logging.DEBUG
    assert isinstance(handler.formatter, logging_module.YunbridgeFormatter)
    assert handler.formatter._fmt == "%(name)s %(levelname)s: %(message)s"
    assert handler.ident == "yunbridge "

    logging.getLogger().handlers.clear()


def test_yunbridge_formatter_preserves_original_name():
    formatter = logging_module.YunbridgeFormatter("%(name)s %(message)s")
    record = logging.LogRecord(
        name="yunbridge.sub",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="hello",
        args=(),
        exc_info=None,
    )

    output = formatter.format(record)

    assert output == "sub hello"
    assert record.name == "yunbridge.sub"


def test_yunbridge_formatter_no_prefix():
    formatter = logging_module.YunbridgeFormatter("%(name)s %(message)s")
    record = logging.LogRecord(
        name="other",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="hello",
        args=(),
        exc_info=None,
    )

    output = formatter.format(record)

    assert output == "other hello"
    assert record.name == "other"

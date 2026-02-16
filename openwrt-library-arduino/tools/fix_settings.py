import os
import sys

path = "../../openwrt-mcu-bridge/mcubridge/config/settings.py"
if not os.path.exists(path):
    sys.stderr.write(f"File not found: {path}\n")
    sys.exit(1)

lines = open(path).readlines()
start = -1
for i, line in enumerate(lines):
    if "def load_runtime_config" in line:
        start = i
        break

if start != -1:
    new_code = [
        'def load_runtime_config() -> RuntimeConfig:\n',
        '    "Load configuration from UCI/defaults using msgspec for efficient validation."\n',
        '\n',
        '    raw_config, source = _load_raw_config()\n',
        '    _CONFIG_STATE.source = source\n',
        '\n',
        '    if "allowed_commands" in raw_config:\n',
        '        allowed_raw = raw_config["allowed_commands"]\n',
        '        if isinstance(allowed_raw, str):\n',
        '            raw_config["allowed_commands"] = normalise_allowed_commands(allowed_raw.split())\n',
        '\n',
        '    if "debug" in raw_config:\n',
        '        raw_config["debug_logging"] = parse_bool(raw_config.pop("debug"))\n',
        '\n',
        '    if "serial_shared_secret" in raw_config:\n',
        '        secret = raw_config["serial_shared_secret"]\n',
        '        if isinstance(secret, str):\n',
        '            raw_config["serial_shared_secret"] = secret.strip().encode("utf-8")\n',
        '\n',
        '    # Normalization\n',
        '    if "file_system_root" in raw_config:\n',
        '        raw_config["file_system_root"] = os.path.abspath(os.path.expanduser(raw_config["file_system_root"]))\n',
        '    if "mqtt_spool_dir" in raw_config:\n',
        '        raw_config["mqtt_spool_dir"] = os.path.abspath(os.path.expanduser(raw_config["mqtt_spool_dir"]))\n',
        '    if "mqtt_topic" in raw_config:\n',
        '        if isinstance(raw_config["mqtt_topic"], str):\n',
        '            raw_config["mqtt_topic"] = "/".join(s.strip() for s in raw_config["mqtt_topic"].split("/") if s.strip())\n',
        '\n',
        '    try:\n',
        '        config = msgspec.convert(raw_config, RuntimeConfig, strict=False)\n',
        '        \n',
        '        # Validation\n',
        '        if config.serial_shared_secret == b"changeme123":\n',
        '            raise ValueError("serial_shared_secret placeholder is insecure")\n',
        '        if not config.serial_shared_secret:\n',
        '            raise ValueError("serial_shared_secret must be configured")\n',
        '        if len(config.serial_shared_secret) < 8:\n',
        '            raise ValueError("serial_shared_secret must be at least 8 bytes")\n',
        '        if len(set(config.serial_shared_secret)) < 4:\n',
        '            raise msgspec.ValidationError("serial_shared_secret must contain at least four distinct bytes")\n',
        '        \n',
        '        if config.mailbox_queue_bytes_limit < config.mailbox_queue_limit:\n',
        '             raise msgspec.ValidationError("mailbox_queue_bytes_limit must be greater than or equal to mailbox_queue_limit")\n',
        '\n',
        '        if not config.allow_non_tmp_paths:\n',
        '            from .const import VOLATILE_STORAGE_PATHS\n',
        '            for attr in ("file_system_root", "mqtt_spool_dir"):\n',
        '                val = getattr(config, attr)\n',
        '                if not any(val.startswith(p) for p in VOLATILE_STORAGE_PATHS):\n',
        '                    raise ValueError(f"FLASH PROTECTION: {attr} must be in a volatile location")\n',
        '        \n',
        '        if config.status_interval <= 0:\n',
        '            raise ValueError("status_interval must be a positive integer")\n',
        '        if config.serial_handshake_fatal_failures <= 0:\n',
        '            raise ValueError("serial_handshake_fatal_failures must be a positive integer")\n',
        '        if config.watchdog_enabled and config.watchdog_interval <= 0:\n',
        '            raise ValueError("watchdog_interval must be a positive number")\n',
        '\n',
        '        return config\n',
        '    except (msgspec.ValidationError, TypeError, ValueError) as e:\n',
        '        if "pytest" in sys.modules and source == "test":\n',
        '             raise\n',
        '        logger.critical("Configuration validation failed: %s", e)\n',
        '        logger.warning("Falling back to safe defaults due to validation error.")\n',
        '        return msgspec.convert(get_default_config(), RuntimeConfig, strict=False)\n'
    ]
    lines[start:] = new_code
    with open(path, "w") as f:
        f.writelines(lines)
    sys.stdout.write("Successfully patched load_runtime_config\n")
else:
    sys.stderr.write("Could not find load_runtime_config\n")
    sys.exit(1)

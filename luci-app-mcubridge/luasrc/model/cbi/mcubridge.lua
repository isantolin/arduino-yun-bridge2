local uci = require "luci.model.uci".cursor()
local sys = require "luci.sys"

-- Guarantee the daemon always has a 'general' section to edit.
local function ensure_general_section()
    local has_general = false

    uci:foreach("mcubridge", "general", function()
        has_general = true
        return false
    end)

    if has_general then
        return
    end

    local fallback_name
    local fallback_data

    uci:foreach("mcubridge", nil, function(section)
        fallback_name = section[".name"] or "general"
        fallback_data = {}

        for key, value in pairs(section) do
            if key:sub(1, 1) ~= "." then
                fallback_data[key] = value
            end
        end

        return false
    end)

    if fallback_data then
        uci:delete("mcubridge", fallback_name)
        uci:section("mcubridge", "general", "general", fallback_data)
    else
        uci:section("mcubridge", "general", "general", {
            enabled = "1",
            debug = "0",
            serial_port = "/dev/ttyATH0",
            serial_baud = "115200",
            serial_safe_baud = "115200",
            mqtt_host = "127.0.0.1",
            mqtt_port = "8883",
            mqtt_topic = "br",
            mqtt_user = "",
            mqtt_pass = "",
            mqtt_ws_port = "9001",
            mqtt_tls = "1",
            mqtt_tls_insecure = "0",
            mqtt_cafile = "/etc/ssl/certs/ca-certificates.crt",
            mqtt_certfile = "",
            mqtt_keyfile = "",
            -- Default to tmpfs to avoid Flash wear on devices without external storage.
            file_system_root = "/tmp/yun_files",
            mqtt_spool_dir = "/tmp/mcubridge/spool",
            process_timeout = "10",
            allowed_commands = "",
        })
    end

    uci:commit("mcubridge")
end

ensure_general_section()

local m = Map(
    "mcubridge",
    translate("McuBridge Configuration"),
    translate(
        "Configure the McuBridge daemon which proxies RPC frames between the MCU and MQTT. " ..
        "Runtime status snapshots are written to /tmp/mcubridge_status.json (tmpfs) and are cleared on reboot."
    )
)

local s = m:section(TypedSection, "general", translate("Daemon Settings"))
s.anonymous = true
s.addremove = false

local enabled = s:option(Flag, "enabled", translate("Enable Daemon"))
enabled.rmempty = false
enabled.default = "1"

local debug = s:option(Flag, "debug", translate("Enable Debug Logging"))
debug.rmempty = false
debug.default = "0"

local serial_port = s:option(Value, "serial_port", translate("Serial Port"))
serial_port.placeholder = "/dev/ttyATH0"
serial_port.rmempty = false

local serial_baud = s:option(ListValue, "serial_baud", translate("Serial Baud Rate"))
serial_baud:value("2400")
serial_baud:value("4800")
serial_baud:value("9600")
serial_baud:value("19200")
serial_baud:value("38400")
serial_baud:value("57600")
serial_baud:value("115200")
serial_baud:value("230400")
serial_baud:value("250000")
serial_baud:value("460800")
serial_baud:value("500000")
serial_baud:value("921600")
serial_baud:value("1000000")
serial_baud.default = "115200"
serial_baud.rmempty = false

local serial_safe_baud = s:option(
    ListValue,
    "serial_safe_baud",
    translate("Safe Serial Baud Rate"),
    translate("Initial baudrate for negotiation. Use 115200 for safety.")
)
serial_safe_baud:value("2400")
serial_safe_baud:value("4800")
serial_safe_baud:value("9600")
serial_safe_baud:value("19200")
serial_safe_baud:value("38400")
serial_safe_baud:value("57600")
serial_safe_baud:value("115200")
serial_safe_baud:value("230400")
serial_safe_baud:value("250000")
serial_safe_baud:value("460800")
serial_safe_baud:value("500000")
serial_safe_baud:value("921600")
serial_safe_baud:value("1000000")
serial_safe_baud.default = "115200"
serial_safe_baud.rmempty = false

local mqtt_host = s:option(Value, "mqtt_host", translate("MQTT Host"))
mqtt_host.placeholder = "127.0.0.1"
mqtt_host.rmempty = false

local mqtt_port = s:option(Value, "mqtt_port", translate("MQTT Port"))
mqtt_port.datatype = "port"
mqtt_port.placeholder = "8883"
mqtt_port.rmempty = false

local mqtt_user = s:option(Value, "mqtt_user", translate("MQTT Username"))
mqtt_user.rmempty = true

local mqtt_pass = s:option(Value, "mqtt_pass", translate("MQTT Password"))
mqtt_pass.password = true
mqtt_pass.rmempty = true

local mqtt_tls = s:option(Flag, "mqtt_tls", translate("Enable TLS/SSL"))
mqtt_tls.rmempty = false
mqtt_tls.default = "1"
mqtt_tls.description = translate("Strongly recommended. Disabling TLS sends MQTT credentials " ..
    "and payloads in plaintext.")

local mqtt_tls_insecure = s:option(
    Flag,
    "mqtt_tls_insecure",
    translate("Disable TLS Hostname Verification")
)
mqtt_tls_insecure.rmempty = true
mqtt_tls_insecure.default = "0"
mqtt_tls_insecure:depends("mqtt_tls", "1")
mqtt_tls_insecure.description = translate(
    "Equivalent to mosquitto --insecure. Allows connecting via IP even when the broker certificate " ..
    "CN/SAN is a DNS name. Less secure; use only for trusted/self-hosted brokers."
)

local mqtt_cafile = s:option(Value, "mqtt_cafile", translate("CA File Path"))
mqtt_cafile.placeholder = "/etc/ssl/certs/ca-certificates.crt"
mqtt_cafile:depends("mqtt_tls", "1")
mqtt_cafile.rmempty = true

function mqtt_cafile.validate(_, value, _)
    return value
end

local mqtt_certfile = s:option(Value, "mqtt_certfile", translate("Client Certificate Path"))
mqtt_certfile.placeholder = "/etc/mcubridge/client.crt"
mqtt_certfile:depends("mqtt_tls", "1")
mqtt_certfile.rmempty = true
mqtt_certfile.description = translate(
    "Optional. Only required if your MQTT broker enforces client certificates (mTLS)."
)

local mqtt_keyfile = s:option(Value, "mqtt_keyfile", translate("Client Key Path"))
mqtt_keyfile.placeholder = "/etc/mcubridge/client.key"
mqtt_keyfile:depends("mqtt_tls", "1")
mqtt_keyfile.rmempty = true
mqtt_keyfile.description = translate(
    "Optional. Only required if your MQTT broker enforces client certificates (mTLS)."
)

local mqtt_topic = s:option(Value, "mqtt_topic", translate("MQTT Topic Prefix"))
mqtt_topic.placeholder = "br"
mqtt_topic.rmempty = false
mqtt_topic.description = translate("Base topic used for bridge messages (for example br/d/<pin>).")
function mqtt_topic.validate(_, value, _)
    if not value or value == "" then
        return nil, translate("Topic prefix cannot be empty.")
    end

    if value:find("[#+]") then
        return nil, translate("Topic prefix cannot contain MQTT wildcards.")
    end

    return value
end

local mqtt_ws_port = s:option(Value, "mqtt_ws_port", translate("MQTT WebSocket Port"))
mqtt_ws_port.datatype = "port"
mqtt_ws_port.placeholder = "9001"
mqtt_ws_port.rmempty = true

local mqtt_spool_dir = s:option(Value, "mqtt_spool_dir", translate("MQTT Spool Directory"))
mqtt_spool_dir.placeholder = "/tmp/mcubridge/spool"
mqtt_spool_dir.rmempty = false
mqtt_spool_dir.description = translate(
    "Directory used to spool MQTT messages when the broker is unavailable. " ..
    "Keep this on /tmp (tmpfs) or an external mount to avoid Flash wear."
)
function mqtt_spool_dir.validate(_, value, _)
    if not value or value == "" then
        return nil, translate("Spool directory cannot be empty.")
    end
    if value:sub(1, 1) ~= "/" then
        return nil, translate("Spool directory must be an absolute path.")
    end
    if value:match("^/tmp") or value:match("^/run") or value:match("^/var/run") or value:match("^/mnt") then
        return value
    end
    return nil, translate("For Flash safety, use a path under /tmp, /run, /var/run, or /mnt.")
end

local fs_root = s:option(Value, "file_system_root", translate("File System Root"))
fs_root.placeholder = "/tmp/yun_files"
fs_root.rmempty = false
fs_root.description = translate(
    "Directory exposed for MCU file operations. Use /tmp for tmpfs (Flash-safe) or /mnt/<device> for external storage."
)

local process_timeout = s:option(Value, "process_timeout", translate("Process Timeout (s)"))
process_timeout.datatype = "uinteger"
process_timeout.placeholder = "10"
process_timeout.rmempty = false

local allowed_commands = s:option(Value, "allowed_commands", translate("Allowed Shell Commands"))
allowed_commands.placeholder = "date uptime"
allowed_commands.rmempty = true
allowed_commands.description = translate("Space separated whitelist for shell execution (leave empty to disable).")

-- MQTT topic permissions ----------------------------------------------------
local function mqtt_acl_option(key, label, description)
    local opt = s:option(Flag, key, translate(label))
    opt.rmempty = false
    opt.default = "1"
    if description then
        opt.description = translate(description)
    end
    return opt
end

mqtt_acl_option(
    "mqtt_allow_file_read",
    "Allow file reads",
    "Accept MQTT requests that read files via br/fs/read."
)
mqtt_acl_option(
    "mqtt_allow_file_write",
    "Allow file writes",
    "Accept MQTT requests that write files via br/fs/write."
)
mqtt_acl_option(
    "mqtt_allow_file_remove",
    "Allow file deletes",
    "Accept MQTT requests that delete files via br/fs/remove."
)
mqtt_acl_option(
    "mqtt_allow_datastore_get",
    "Allow datastore get",
    "Allow clients to read key/value pairs via br/datastore/get."
)
mqtt_acl_option(
    "mqtt_allow_datastore_put",
    "Allow datastore put",
    "Allow clients to modify key/value pairs via br/datastore/put."
)
mqtt_acl_option(
    "mqtt_allow_mailbox_read",
    "Allow mailbox read",
    "Permit MQTT reads from the MCU mailbox."
)
mqtt_acl_option(
    "mqtt_allow_mailbox_write",
    "Allow mailbox write",
    "Permit MQTT writes into the MCU mailbox queue."
)
mqtt_acl_option(
    "mqtt_allow_shell_run",
    "Allow shell run",
    "Allow synchronous shell execution via br/sh/run."
)
mqtt_acl_option(
    "mqtt_allow_shell_run_async",
    "Allow shell run_async",
    "Allow asynchronous shell execution via br/sh/run_async."
)
mqtt_acl_option(
    "mqtt_allow_shell_poll",
    "Allow shell poll",
    "Allow polling of asynchronous shell jobs via br/sh/poll."
)
mqtt_acl_option(
    "mqtt_allow_shell_kill",
    "Allow shell kill",
    "Allow canceling asynchronous shell jobs via br/sh/kill."
)
mqtt_acl_option(
    "mqtt_allow_console_input",
    "Allow console input",
    "Permit MQTT writes to br/console/in to reach the MCU console."
)
mqtt_acl_option(
    "mqtt_allow_digital_write",
    "Allow digital write",
    "Allow MQTT writes to br/d/<pin>/write."
)
mqtt_acl_option(
    "mqtt_allow_digital_read",
    "Allow digital read",
    "Allow MQTT reads via br/d/<pin>/read."
)
mqtt_acl_option(
    "mqtt_allow_digital_mode",
    "Allow digital mode",
    "Allow MQTT access to br/d/<pin>/mode."
)
mqtt_acl_option(
    "mqtt_allow_analog_write",
    "Allow analog write",
    "Allow MQTT writes to br/a/<pin>/write."
)
mqtt_acl_option(
    "mqtt_allow_analog_read",
    "Allow analog read",
    "Allow MQTT reads via br/a/<pin>/read."
)

local serial_secret = s:option(Value, "serial_shared_secret", translate("Serial Shared Secret"))
serial_secret.password = true
serial_secret.rmempty = false
serial_secret.description = translate("Shared secret for serial authentication (BRIDGE_SERIAL_SHARED_SECRET).")

-- Helper script to toggle MQTT port based on TLS setting
local script_s = m:section(SimpleSection)
script_s.template = "mcubridge/mqtt_port_helper"

-- Deleted: Manual required_field_rules table and validation functions.
-- LuCI handles dependencies (depends) and required fields (rmempty=false) natively.

function m.on_commit(_)
    -- Restart the daemon so changes take effect immediately.
    sys.call("/etc/init.d/mcubridge restart >/dev/null 2>&1")
end

return m
local uci = require "luci.model.uci".cursor()
local sys = require "luci.sys"

-- Guarantee the daemon always has a 'general' section to edit.
local function ensure_general_section()
    local has_general = false

    uci:foreach("yunbridge", "general", function()
        has_general = true
        return false
    end)

    if has_general then
        return
    end

    local fallback_name
    local fallback_data

    uci:foreach("yunbridge", nil, function(section)
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
        uci:delete("yunbridge", fallback_name)
        uci:section("yunbridge", "general", "general", fallback_data)
    else
        uci:section("yunbridge", "general", "general", {
            enabled = "1",
            debug = "0",
            serial_port = "/dev/ttyATH0",
            serial_baud = "115200",
            mqtt_host = "127.0.0.1",
            mqtt_port = "8883",
            mqtt_topic = "br",
            mqtt_user = "",
            mqtt_pass = "",
            mqtt_ws_port = "9001",
            mqtt_tls = "1",
            mqtt_cafile = "/etc/ssl/certs/ca-certificates.crt",
            mqtt_certfile = "",
            mqtt_keyfile = "",
            file_system_root = "/root/yun_files",
            process_timeout = "10",
            allowed_commands = "",
            serial_shared_secret = "changeme123",
        })
    end

    uci:commit("yunbridge")
end

ensure_general_section()

local m = Map(
    "yunbridge",
    translate("YunBridge Configuration"),
    translate("Configure the YunBridge daemon which proxies RPC frames between the MCU and MQTT.")
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

local serial_baud = s:option(Value, "serial_baud", translate("Serial Baud Rate"))
serial_baud.datatype = "uinteger"
serial_baud.placeholder = "115200"
serial_baud.rmempty = false

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

local mqtt_cafile = s:option(Value, "mqtt_cafile", translate("CA File Path"))
mqtt_cafile.placeholder = "/etc/ssl/certs/ca-certificates.crt"
mqtt_cafile:depends("mqtt_tls", "1")
mqtt_cafile.rmempty = true

local function is_tls_enabled(section)
    local form_value = mqtt_tls:formvalue(section)
    if form_value == nil then
        form_value = uci:get("yunbridge", section, "mqtt_tls")
    end
    return tostring(form_value or "0") == "1"
end

function mqtt_cafile.validate(_, value, section)
    if is_tls_enabled(section) then
        if not value or value == "" then
            return nil, translate("CA file is required when TLS is enabled.")
        end
    end
    return value
end

local mqtt_certfile = s:option(Value, "mqtt_certfile", translate("Client Certificate Path"))
mqtt_certfile.placeholder = "/etc/yunbridge/client.crt"
mqtt_certfile:depends("mqtt_tls", "1")
mqtt_certfile.rmempty = true

local mqtt_keyfile = s:option(Value, "mqtt_keyfile", translate("Client Key Path"))
mqtt_keyfile.placeholder = "/etc/yunbridge/client.key"
mqtt_keyfile:depends("mqtt_tls", "1")
mqtt_keyfile.rmempty = true

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

local fs_root = s:option(Value, "file_system_root", translate("File System Root"))
fs_root.placeholder = "/root/yun_files"
fs_root.rmempty = false
fs_root.description = translate("Directory exposed for MCU file operations.")

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

local serial_secret = s:option(DummyValue, "_serial_shared_secret", translate("Serial Shared Secret"))
function serial_secret.cfgvalue()
    return translate("Managed via UCI. Use the Credentials & TLS tab to rotate secrets.")
end

-- Deleted: Manual required_field_rules table and validation functions.
-- LuCI handles dependencies (depends) and required fields (rmempty=false) natively.

function m.on_commit(_)
    -- Restart the daemon so changes take effect immediately.
    sys.call("/etc/init.d/yunbridge restart >/dev/null 2>&1")
end

return m
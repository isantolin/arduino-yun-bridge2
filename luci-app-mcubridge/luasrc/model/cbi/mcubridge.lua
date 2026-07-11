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
            cloud_host = "127.0.0.1",
            cloud_port = "8443",
            topic_prefix = "br",
            cloud_user = "",
            cloud_pass = "",
            cloud_tls = "1",
            cloud_tls_insecure = "0",
            cloud_cafile = "/etc/ssl/certs/ca-certificates.crt",
            cloud_certfile = "",
            cloud_keyfile = "",
            -- Default to tmpfs to avoid Flash wear on devices without external storage.
            file_system_root = "/tmp/yun_files",
            cloud_spool_dir = "/tmp/mcubridge/spool",
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
        "Configure the McuBridge daemon which proxies RPC frames between the MCU and the Cloud. " ..
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
serial_safe_baud:value("460800")
serial_safe_baud:value("500000")
serial_safe_baud:value("921600")
serial_safe_baud:value("1000000")
serial_safe_baud.default = "115200"
serial_safe_baud.rmempty = false

local cloud_host = s:option(Value, "cloud_host", translate("Cloud Host"))
cloud_host.placeholder = "127.0.0.1"
cloud_host.rmempty = false

local cloud_port = s:option(Value, "cloud_port", translate("Cloud Port"))
cloud_port.datatype = "port"
cloud_port.placeholder = "8443"
cloud_port.rmempty = false

local cloud_user = s:option(Value, "cloud_user", translate("Cloud Username"))
cloud_user.rmempty = true

local cloud_pass = s:option(Value, "cloud_pass", translate("Cloud Password"))
cloud_pass.password = true
cloud_pass.rmempty = true

local cloud_tls = s:option(Flag, "cloud_tls", translate("Enable TLS/SSL"))
cloud_tls.rmempty = false
cloud_tls.default = "1"
cloud_tls.description = translate("Strongly recommended. Disabling TLS sends credentials " ..
    "and payloads in plaintext.")

local cloud_tls_insecure = s:option(
    Flag,
    "cloud_tls_insecure",
    translate("Disable TLS Hostname Verification")
)
cloud_tls_insecure.rmempty = true
cloud_tls_insecure.default = "0"
cloud_tls_insecure:depends("cloud_tls", "1")
cloud_tls_insecure.description = translate(
    "Equivalent to insecure TLS. Allows connecting via IP even when the certificate " ..
    "CN/SAN is a DNS name. Less secure; use only for trusted/self-hosted gateways."
)

local cloud_cafile = s:option(Value, "cloud_cafile", translate("CA File Path"))
cloud_cafile.placeholder = "/etc/ssl/certs/ca-certificates.crt"
cloud_cafile:depends("cloud_tls", "1")
cloud_cafile.rmempty = true

function cloud_cafile.validate(_, value, _)
    return value
end

local cloud_certfile = s:option(Value, "cloud_certfile", translate("Client Certificate Path"))
cloud_certfile.placeholder = "/etc/mcubridge/client.crt"
cloud_certfile:depends("cloud_tls", "1")
cloud_certfile.rmempty = true
cloud_certfile.description = translate(
    "Optional. Only required if your Cloud Gateway enforces client certificates (mTLS)."
)

local cloud_keyfile = s:option(Value, "cloud_keyfile", translate("Client Key Path"))
cloud_keyfile.placeholder = "/etc/mcubridge/client.key"
cloud_keyfile:depends("cloud_tls", "1")
cloud_keyfile.rmempty = true
cloud_keyfile.description = translate(
    "Optional. Only required if your Cloud Gateway enforces client certificates (mTLS)."
)

local topic_prefix = s:option(Value, "topic_prefix", translate("Topic Prefix"))
topic_prefix.placeholder = "br"
topic_prefix.rmempty = false
topic_prefix.description = translate("Base prefix used for messages (for example br/d/<pin>).")
function topic_prefix.validate(_, value, _)
    if not value or value == "" then
        return nil, translate("Topic prefix cannot be empty.")
    end

    if value:find("[#+]") then
        return nil, translate("Topic prefix cannot contain wildcards.")
    end

    return value
end

local cloud_spool_dir = s:option(Value, "cloud_spool_dir", translate("Cloud Spool Directory"))
cloud_spool_dir.placeholder = "/tmp/mcubridge/spool"
cloud_spool_dir.rmempty = false
cloud_spool_dir.description = translate(
    "Directory used to spool messages when the Cloud Gateway is unavailable. " ..
    "Keep this on /tmp (tmpfs) or an external mount to avoid Flash wear."
)
function cloud_spool_dir.validate(_, value, _)
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

-- Cloud topic permissions ----------------------------------------------------
local function cloud_acl_option(key, label, description)
    local opt = s:option(Flag, key, translate(label))
    opt.rmempty = false
    opt.default = "1"
    if description then
        opt.description = translate(description)
    end
    return opt
end

cloud_acl_option(
    "cloud_allow_file_read",
    "Allow file reads",
    "Accept requests that read files via br/fs/read."
)
cloud_acl_option(
    "cloud_allow_file_write",
    "Allow file writes",
    "Accept requests that write files via br/fs/write."
)
cloud_acl_option(
    "cloud_allow_file_remove",
    "Allow file deletes",
    "Accept requests that delete files via br/fs/remove."
)
cloud_acl_option(
    "cloud_allow_datastore_get",
    "Allow datastore get",
    "Allow clients to read key/value pairs via br/datastore/get."
)
cloud_acl_option(
    "cloud_allow_datastore_put",
    "Allow datastore put",
    "Allow clients to modify key/value pairs via br/datastore/put."
)
cloud_acl_option(
    "cloud_allow_mailbox_read",
    "Allow mailbox read",
    "Permit reads from the MCU mailbox."
)
cloud_acl_option(
    "cloud_allow_mailbox_write",
    "Allow mailbox write",
    "Permit writes into the MCU mailbox queue."
)
cloud_acl_option(
    "cloud_allow_shell_run",
    "Allow shell run",
    "Allow synchronous shell execution via br/sh/run."
)
cloud_acl_option(
    "cloud_allow_shell_run_async",
    "Allow shell run_async",
    "Allow asynchronous shell execution via br/sh/run_async."
)
cloud_acl_option(
    "cloud_allow_shell_poll",
    "Allow shell poll",
    "Allow polling of asynchronous shell jobs via br/sh/poll."
)
cloud_acl_option(
    "cloud_allow_shell_kill",
    "Allow shell kill",
    "Allow canceling asynchronous shell jobs via br/sh/kill."
)
cloud_acl_option(
    "cloud_allow_console_input",
    "Allow console input",
    "Permit writes to br/console/in to reach the MCU console."
)
cloud_acl_option(
    "cloud_allow_digital_write",
    "Allow digital write",
    "Allow writes to br/d/<pin>/write."
)
cloud_acl_option(
    "cloud_allow_digital_read",
    "Allow digital read",
    "Allow reads via br/d/<pin>/read."
)
cloud_acl_option(
    "cloud_allow_digital_mode",
    "Allow digital mode",
    "Allow access to br/d/<pin>/mode."
)
cloud_acl_option(
    "cloud_allow_analog_write",
    "Allow analog write",
    "Allow writes to br/a/<pin>/write."
)
cloud_acl_option(
    "cloud_allow_analog_read",
    "Allow analog read",
    "Allow reads via br/a/<pin>/read."
)

local serial_secret = s:option(Value, "serial_shared_secret", translate("Serial Shared Secret"))
serial_secret.password = true
serial_secret.rmempty = false
serial_secret.description = translate("Shared secret for serial authentication (BRIDGE_SERIAL_SHARED_SECRET).")

-- Deleted: Manual required_field_rules table and validation functions.
-- LuCI handles dependencies (depends) and required fields (rmempty=false) natively.

function m.on_commit(_)
    -- Restart the daemon so changes take effect immediately.
    sys.call("/etc/init.d/mcubridge restart >/dev/null 2>&1")
end

return m
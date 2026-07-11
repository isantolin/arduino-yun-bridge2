--
-- This file is part of Arduino MCU Ecosystem v2.
--
-- Copyright (C) 2025 Ignacio Santolin and contributors
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU General Public License as published by
-- the Free Software Foundation, either version 3 of the License, or
-- (at your option) any later version.
--
-- This program is distributed in the hope that it will be useful,
-- but WITHOUT ANY WARRANTY; without even the implied warranty of
-- MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
-- GNU General Public License for more details.
--
-- You should have received a copy of the GNU General Public License
-- along with this program.  If not, see <https://www.gnu.org/licenses/>.
--
module("luci.controller.mcubridge", package.seeall)

local fs = require "nixio.fs"
local uci = require "luci.model.uci".cursor()
local sys = require "luci.sys"
local nixio = require "nixio"

local function encode_varint(n)
    local bytes = {}
    while n >= 128 do
        bytes[#bytes + 1] = string.char((n % 128) + 128)
        n = math.floor(n / 128)
    end
    bytes[#bytes + 1] = string.char(n)
    return table.concat(bytes)
end

local function encode_string(field_num, s)
    if not s or s == "" then return "" end
    local tag = string.char((field_num * 8) + 2)
    return tag .. encode_varint(#s) .. s
end

local function serialize_publish(topic, payload)
    local f1 = encode_string(1, topic)
    local f2 = encode_string(2, payload)
    local body = f1 .. f2
    local len = #body
    local len_bytes = string.char(
        math.floor(len / 16777216) % 256,
        math.floor(len / 65536) % 256,
        math.floor(len / 256) % 256,
        len % 256
    )
    return len_bytes .. body
end

local function socket_publish(topic, payload)
    local socket_path = uci:get("mcubridge", "general", "socket_path") or "/var/run/mcubridge.sock"
    local sock = nixio.socket("unix", "stream")
    if not sock then return false end

    if not sock:connect(socket_path) then
        sock:close()
        return false
    end

    local data = serialize_publish(topic, payload)
    local sent = sock:send(data)
    sock:close()
    return sent == #data
end


function index()
    -- Configuration and status pages
    entry({"admin", "services", "mcubridge"}, cbi("mcubridge"), "McuBridge", 90).dependent = true
    entry({"admin", "services", "mcubridge", "webui"}, template("mcubridge/webui"), "Web UI", 100).dependent = false
    entry({"admin", "services", "mcubridge", "status"}, template("mcubridge/status"), "Daemon Status", 110)
        .dependent = false
    entry({"admin", "services", "mcubridge", "capabilities"},
        template("mcubridge/capabilities"), "Device Capabilities", 115).dependent = false
    entry({"admin", "services", "mcubridge", "credentials"}, template("mcubridge/credentials"),
        "Credentials & TLS", 120).dependent = false

    -- Internal actions for status page
    entry({"admin", "services", "mcubridge", "status_raw"}, call("action_status")).leaf = true
    -- Logs actions removed

    entry({"admin", "services", "mcubridge", "rotate_credentials"}, call("action_rotate_credentials")).leaf = true
    entry({"admin", "services", "mcubridge", "hw_smoke"}, call("action_hw_smoke")).leaf = true

    -- REST API Endpoint
    entry({"admin", "services", "mcubridge", "api"}, call("action_api")).leaf = true
end

-- Helper function to send a standardized JSON response
local function send_json(code, data)
    luci.http.status(code)
    luci.http.prepare_content("application/json")
    luci.http.write(require("luci.jsonc").stringify(data))
end

local function redact_secret_lines(output)
    if not output or output == "" then
        return output
    end
    local redacted = output:gsub("SERIAL_SECRET=[^\r\n]*", "SERIAL_SECRET=[redacted]")
    redacted = redacted:gsub("CLOUD_PASSWORD=[^\r\n]*", "CLOUD_PASSWORD=[redacted]")
    redacted = redacted:gsub("CLOUD_PASS=[^\r\n]*", "CLOUD_PASS=[redacted]")
    return redacted
end

-- REST API action handler
function action_api(...)
    local args = {...}
    local resource_type = args[1]
    local pin_number = args[2]

    -- Validate URL structure: /api/pin/<number>
    if not (resource_type == "pin" and pin_number and pin_number:match("^%d+$")) then
        return send_json(400, {
            status = "error",
            message = "Invalid API endpoint. Use /pin/<number>."
        })
    end

    -- Only POST is supported for pin control
    local method = luci.http.getenv("REQUEST_METHOD") or "GET"
    if method ~= "POST" then
        return send_json(405, {
            status = "error",
            message = "Method " .. method .. " not allowed."
        })
    end

    -- Parse JSON body
    local body = luci.http.content()
    local success, data = pcall(require("luci.jsonc").parse, body)
    if not success or type(data) ~= "table" then
        return send_json(400, { status = "error", message = "Invalid or empty JSON body." })
    end

    -- Validate state from body
    local state = data.state and string.upper(data.state) or ""
    if state ~= "ON" and state ~= "OFF" then
        return send_json(400, { status = "error", message = 'State must be "ON" or "OFF".' })
    end

    -- Send command via UNIX socket to the daemon
    local topic_prefix = uci:get("mcubridge", "general", "topic_prefix") or "br"
    local payload = (state == "ON") and "1" or "0"
    local topic = string.format("%s/d/%s", topic_prefix, pin_number)

    local ok = socket_publish(topic, payload)

    if ok then
        send_json(200, {
            status = "ok",
            pin = tonumber(pin_number),
            state = state,
            message = "Command sent via UNIX socket."
        })
    else
        send_json(500, {
            status = "error",
            message = "Failed to communicate with MCU Bridge daemon via UNIX socket."
        })
    end
end




function action_status()
    local content = fs.readfile("/tmp/mcubridge_status.json")
    if not content or content == "" then
        content = '{"status": "error", "message": "' ..
            "Status file not found or is empty. The daemon may be stopped, starting up, " ..
            "or the device may have rebooted (the status file lives on /tmp tmpfs)." ..
            '"}'
    end
    luci.http.prepare_content("application/json")
    luci.http.write(content)
end

-- Deleted: action_log_daemon, action_log_gateway, action_log_script and helper functions

function action_rotate_credentials()
    local cmd = "/usr/bin/mcubridge-rotate-credentials"
    local tmp = (sys.exec("mktemp -p /tmp mcubridge-luci.XXXXXX 2>/dev/null") or ""):gsub("%s+$", "")
    if tmp == "" then tmp = os.tmpname() end
    local rc = sys.call(string.format("%s >%q 2>&1", cmd, tmp))
    local output = fs.readfile(tmp) or ""
    fs.remove(tmp)
    local status = rc == 0 and 200 or 500
    local serial_secret
    if output then
        serial_secret = output:match("SERIAL_SECRET=([0-9a-fA-F]+)")
    end
    local sketch_snippet
    if serial_secret then
        sketch_snippet = string.format(
            '#define BRIDGE_SERIAL_SHARED_SECRET "%s"\n' ..
            '#define BRIDGE_SERIAL_SHARED_SECRET_LEN (sizeof(BRIDGE_SERIAL_SHARED_SECRET) - 1)',
            serial_secret
        )
    end
    send_json(status, {
        status = (rc == 0) and "ok" or "error",
        output = redact_secret_lines(output),
        serial_secret = serial_secret,
        sketch_snippet = sketch_snippet,
    })
end

function action_hw_smoke()
    local cmd = "/usr/bin/mcubridge-hw-smoke"
    local tmp = (sys.exec("mktemp -p /tmp mcubridge-luci.XXXXXX 2>/dev/null") or ""):gsub("%s+$", "")
    if tmp == "" then tmp = os.tmpname() end
    local rc = sys.call(string.format("%s >%q 2>&1", cmd, tmp))
    local output = fs.readfile(tmp) or ""
    fs.remove(tmp)
    local status = rc == 0 and 200 or 500
    send_json(status, {
        status = (rc == 0) and "ok" or "error",
        output = output,
    })
end
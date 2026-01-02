--
-- This file is part of Arduino Yun Ecosystem v2.
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
module("luci.controller.yunbridge", package.seeall)

local fs = require "nixio.fs"
local uci = require "luci.model.uci".cursor()
local sys = require "luci.sys"
local posix = require "posix"
local unistd = require "posix.unistd"
local sys_wait = require "posix.sys.wait"
local errno = require "posix.errno"

local function nanosleep(seconds)
    if seconds <= 0 then
        return
    end
    local whole = math.floor(seconds)
    local fraction = seconds - whole
    if whole > 0 then
        unistd.sleep(whole)
    end
    if fraction > 0 then
        unistd.usleep(math.floor(fraction * 1e6))
    end
end

local function spawn_mosquitto(argv)
    local pid, fork_err = unistd.fork()
    if pid == 0 then
        posix.setenv("PATH", os.getenv("PATH") or "")
        local _, _, exec_errno = unistd.execp("mosquitto_pub", argv)
        unistd._exit(exec_errno or 127)
    elseif not pid then
        return nil, {
            fork_error = fork_err,
            errno = errno.errno() or 0,
        }
    end

    while true do
        local wait_pid, reason, status = sys_wait.wait(pid)
        if wait_pid == pid then
            return {
                reason = reason,
                status = status,
            }
        end
        local err = errno.errno() or 0
        if err ~= errno.EINTR then
            return {
                reason = reason,
                status = status,
                errno = err,
            }
        end
    end
end

local function mosquitto_publish_with_retries(argv, attempts, base_delay)
    attempts = attempts or 3
    base_delay = base_delay or 0.5
    local delay = base_delay
    local last_error

    for attempt = 1, attempts do
        local result, err = spawn_mosquitto(argv)
        if result and result.reason == "exited" and result.status == 0 then
            return true
        end

        last_error = result or err
        if attempt < attempts then
            nanosleep(delay)
            delay = math.min(delay * 2, 4.0)
        end
    end

    return false, last_error
end

function index()
    -- Configuration and status pages
    entry({"admin", "services", "yunbridge"}, cbi("yunbridge"), "YunBridge", 90).dependent = true
    entry({"admin", "services", "yunbridge", "webui"}, template("yunbridge/webui"), "Web UI", 100).dependent = false
    entry({"admin", "services", "yunbridge", "status"}, template("yunbridge/status"), "Daemon Status", 110)
        .dependent = false
    entry({"admin", "services", "yunbridge", "credentials"}, template("yunbridge/credentials"),
        "Credentials & TLS", 120).dependent = false

    -- Internal actions for status page
    entry({"admin", "services", "yunbridge", "status_raw"}, call("action_status")).leaf = true
    -- Logs actions removed

    entry({"admin", "services", "yunbridge", "mqtt_ws_auth"}, call("action_mqtt_ws_auth")).leaf = true
    entry({"admin", "services", "yunbridge", "mqtt_ws_url"}, call("action_mqtt_ws_url")).leaf = true
    entry({"admin", "services", "yunbridge", "rotate_credentials"}, call("action_rotate_credentials")).leaf = true
    entry({"admin", "services", "yunbridge", "hw_smoke"}, call("action_hw_smoke")).leaf = true

    -- REST API Endpoint
    entry({"admin", "services", "yunbridge", "api"}, call("action_api")).leaf = true
end

-- Helper function to send a standardized JSON response
local function send_json(code, data)
    luci.http.status(code)
    luci.http.prepare_content("application/json")
    luci.http.write(require("luci.jsonc").stringify(data))
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

    -- Get MQTT config from UCI
    local host = uci:get("yunbridge", "general", "mqtt_host") or "127.0.0.1"
    local port = uci:get("yunbridge", "general", "mqtt_port") or "8883"
    local topic_prefix = uci:get("yunbridge", "general", "mqtt_topic") or "br"
    local tls = uci:get("yunbridge", "general", "mqtt_tls") or "1"
    local cafile = uci:get("yunbridge", "general", "mqtt_cafile") or ""

    -- Prepare mosquitto_pub arguments without relying on a shell
    local payload = (state == "ON") and "1" or "0"
    local topic = string.format("%s/d/%s", topic_prefix, pin_number)
    local client_id = string.format("luci-api-%s-%s", pin_number, os.time())
    local pub_args = {
        "mosquitto_pub",
        "-h", host,
        "-p", tostring(port),
        "-t", topic,
        "-m", payload,
        "-i", client_id,
        "-r"
    }

    if tls == "1" then
        if cafile ~= "" then
            pub_args[#pub_args + 1] = "--cafile"
            pub_args[#pub_args + 1] = cafile
        end
        pub_args[#pub_args + 1] = "--tls-version"
        pub_args[#pub_args + 1] = "tlsv1.2"
    end

    local ok, last_error = mosquitto_publish_with_retries(pub_args, 3, 0.5)

    if ok then
        send_json(200, {
            status = "ok",
            pin = tonumber(pin_number),
            state = state,
            message = "Command sent via MQTT."
        })
    else
        send_json(500, {
            status = "error",
            message = "Failed to execute mosquitto_pub. Is mosquitto-client installed?",
            detail = last_error,
            argv = args
        })
    end
end


function action_mqtt_ws_auth()
    local user = uci:get("yunbridge", "general", "mqtt_user") or ""
    local pass = uci:get("yunbridge", "general", "mqtt_pass") or ""
    local topic_prefix = uci:get("yunbridge", "general", "mqtt_topic") or "br"
    send_json(200, {user = user, pass = pass, topic_prefix = topic_prefix})
end

function action_mqtt_ws_url()
    local host = uci:get("yunbridge", "general", "mqtt_host") or "127.0.0.1"
    local ws_port = uci:get("yunbridge", "general", "mqtt_ws_port") or "9001"
    local url = string.format("ws://%s:%s", host, ws_port)
    luci.http.prepare_content("text/plain")
    luci.http.write(url)
end

function action_status()
    local content = fs.readfile("/tmp/yunbridge_status.json")
    if not content or content == "" then
        content = '{"status": "error", "message": "' ..
            "Status file not found or is empty. The daemon may be stopped, starting up, " ..
            "or the device may have rebooted (the status file lives on /tmp tmpfs)." ..
            '"}'
    end
    luci.http.prepare_content("application/json")
    luci.http.write(content)
end

-- Deleted: action_log_daemon, action_log_mqtt, action_log_script and helper functions

local function run_and_capture(cmd)
    local tmp = os.tmpname()
    local wrapped = string.format("%s >%s 2>&1", cmd, tmp)
    local rc = sys.call(wrapped)
    local output = fs.readfile(tmp) or ""
    fs.remove(tmp)
    return rc, output
end

function action_rotate_credentials()
    local cmd = "/usr/bin/yunbridge-rotate-credentials"
    local rc, output = run_and_capture(cmd)
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
        output = output,
        serial_secret = serial_secret,
        sketch_snippet = sketch_snippet,
    })
end

function action_hw_smoke()
    local cmd = "/usr/bin/yunbridge-hw-smoke"
    local rc, output = run_and_capture(cmd)
    local status = rc == 0 and 200 or 500
    send_json(status, {
        status = (rc == 0) and "ok" or "error",
        output = output,
    })
end
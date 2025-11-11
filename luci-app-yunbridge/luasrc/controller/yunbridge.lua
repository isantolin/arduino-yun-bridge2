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

function index()
    -- Configuration and status pages
    entry({"admin", "services", "yunbridge"}, cbi("yunbridge"), "YunBridge", 90).dependent = true
    entry({"admin", "services", "yunbridge", "webui"}, template("yunbridge/webui"), "Web UI", 100).dependent = false
    entry({"admin", "services", "yunbridge", "status"}, template("yunbridge/status"), "Daemon Status", 110).dependent = false

    -- Internal actions for status page
    entry({"admin", "services", "yunbridge", "status_raw"}, call("action_status")).leaf = true
    entry({"admin", "services", "yunbridge", "log_daemon"}, call("action_log_daemon")).leaf = true
    entry({"admin", "services", "yunbridge", "log_mqtt"}, call("action_log_mqtt")).leaf = true
    entry({"admin", "services", "yunbridge", "log_script"}, call("action_log_script")).leaf = true
    entry({"admin", "services", "yunbridge", "mqtt_ws_auth"}, call("action_mqtt_ws_auth")).leaf = true
    entry({"admin", "services", "yunbridge", "mqtt_ws_url"}, call("action_mqtt_ws_url")).leaf = true

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
    if luci.http.request.method ~= "POST" then
        return send_json(405, {
            status = "error",
            message = "Method " .. luci.http.request.method .. " not allowed."
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
    local port = uci:get("yunbridge", "general", "mqtt_port") or "1883"
    local topic_prefix = uci:get("yunbridge", "general", "mqtt_topic") or "br"

    -- Construct mosquitto_pub command
    local payload = (state == "ON") and "1" or "0"
    local topic = string.format("%s/d/%s", topic_prefix, pin_number)
    -- Use -i for a unique client ID to avoid conflicts, and -r to prevent retained messages
    local command = string.format("mosquitto_pub -h %s -p %s -t '%s' -m '%s' -i 'luci-api-%s-%s' -r &",
                                  host, port, topic, payload, pin_number, os.time())

    -- Execute command
    local result = os.execute(command)
    if result == 0 then -- 0 indicates success for os.execute on Unix-like systems
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
            command_for_debug = command,
            os_execute_result = result -- Include the raw result for debugging
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
        content = [[{"status": "error", "message": "Status file not found or is empty."}]]
    end
    luci.http.prepare_content("application/json")
    luci.http.write(content)
end

local function tail_file(path, n)
    local f = io.open(path, "r")
    if not f then return nil end
    local lines = {}
    for line in f:lines() do
        table.insert(lines, line)
        if #lines > n then table.remove(lines, 1) end
    end
    f:close()
    return table.concat(lines, "\n")
end

local function serve_log_file(path, error_msg)
    local content = tail_file(path, 50) or error_msg
    luci.http.prepare_content("text/plain")
    luci.http.write(content)
end

function action_log_daemon()
    serve_log_file("/var/log/yun-bridge.log", "No daemon log file found.")
end

function action_log_mqtt()
    serve_log_file("/var/log/yunbridge_mqtt_plugin.log", "No MQTT log file found.")
end

function action_log_script()
    serve_log_file("/var/log/yunbridge_script.log", "No script log file found.")
end

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


function index()
    -- Entradas de configuración y template (están correctas)
    entry({"admin", "services", "yunbridge"}, cbi("yunbridge"), "YunBridge", 90).dependent = true
    entry({"admin", "services", "yunbridge", "webui"}, template("yunbridge/webui"), "Web UI", 100).dependent = false
    entry({"admin", "services", "yunbridge", "status"}, template("yunbridge/status"), "Daemon Status", 110).dependent = false

    -- Entradas de acción (corregidas con call())
    -- Usar call("nombre_de_la_funcion") asegura que el dispatcher de LuCI las mapee correctamente.
    entry({"admin", "services", "yunbridge", "status_raw"}, call("action_status")).leaf = true
    entry({"admin", "services", "yunbridge", "log_daemon"}, call("action_log_daemon")).leaf = true
    entry({"admin", "services", "yunbridge", "log_mqtt"}, call("action_log_mqtt")).leaf = true
    entry({"admin", "services", "yunbridge", "log_script"}, call("action_log_script")).leaf = true
    entry({"admin", "services", "yunbridge", "mqtt_ws_auth"}, call("action_mqtt_ws_auth")).leaf = true
    entry({"admin", "services", "yunbridge", "mqtt_ws_url"}, call("action_mqtt_ws_url")).leaf = true
end

function action_mqtt_ws_auth()
    -- Read UCI config for mqtt_user and mqtt_pass
    local uci = require "luci.model.uci".cursor()
    local user = uci:get("yunbridge", "main", "mqtt_user") or ""
    local pass = uci:get("yunbridge", "main", "mqtt_pass") or ""
    local obj = {user = user, pass = pass}
    luci.http.prepare_content("application/json")
    luci.http.write(require("luci.jsonc").stringify(obj))
end

function action_mqtt_ws_url()
    -- Read UCI config for mqtt_host and mqtt_ws_port (default 9001)
    local host = "127.0.0.1"
    local port = "9001" -- default WebSocket port
    local uci = require "luci.model.uci".cursor()
    local h = uci:get("yunbridge", "main", "mqtt_host")
    local ws_port = uci:get("yunbridge", "main", "mqtt_ws_port")
    if h and #h > 0 then host = h end
    if ws_port and #ws_port > 0 then port = tostring(ws_port) end
    local url = string.format("ws://%s:%s", host, port)
    luci.http.prepare_content("text/plain")
    luci.http.write(url)
end

function action_status()
    local content = fs.readfile("/tmp/yunbridge_status.json") or "No status file found."
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

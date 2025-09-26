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
    -- Read UCI config for mqtt_host and mqtt_port
    local host = "127.0.0.1"
    local port = "9001" -- default WebSocket port, can be changed if needed
    local uci = require "luci.model.uci".cursor()
    local h = uci:get("yunbridge", "main", "mqtt_host")
    local p = uci:get("yunbridge", "main", "mqtt_port")
    if h and #h > 0 then host = h end
    if p and #p > 0 then port = tostring(tonumber(p) + 800) end -- assume ws port = mqtt_port + 800
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

function action_log_daemon()
    local content = tail_file("/tmp/yunbridge_daemon.log", 50) or "No daemon log file found."
    luci.http.prepare_content("text/plain")
    luci.http.write(content)
end

function action_log_mqtt()
    local content = tail_file("/tmp/yunbridge_mqtt_plugin.log", 50) or "No MQTT log file found."
    luci.http.prepare_content("text/plain")
    luci.http.write(content)
end

function action_log_script()
    local content = tail_file("/tmp/yunbridge_script.log", 50) or "No script log file found."
    luci.http.prepare_content("text/plain")
    luci.http.write(content)
end

module("luci.controller.yunbridge", package.seeall)


local fs = require "nixio.fs"

function index()
	entry({"admin", "services", "yunbridge"}, cbi("yunbridge"), "YunBridge", 90).dependent = true
	entry({"admin", "services", "yunbridge", "webui"}, template("yunbridge/webui"), "Web UI", 100).dependent = false
	entry({"admin", "services", "yunbridge", "status"}, template("yunbridge/status"), "Daemon Status", 110).dependent = false
	entry({"admin", "services", "yunbridge", "status_raw"}, call("action_status"), nil).leaf = true
	entry({"admin", "services", "yunbridge", "log_raw"}, call("action_log"), nil).leaf = true
end

function action_status()
	local content = fs.readfile("/tmp/yunbridge_status.json") or "No status file found."
	luci.http.prepare_content("application/json")
	luci.http.write(content)
end

function action_log()
	local content = fs.readfile("/tmp/yunbridge_debug.log") or "No log file found."
	luci.http.prepare_content("text/plain")
	luci.http.write(content)
end

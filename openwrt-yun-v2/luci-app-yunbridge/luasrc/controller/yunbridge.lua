module("luci.controller.yunbridge", package.seeall)

function index()
	entry({"admin", "services", "yunbridge"}, cbi("yunbridge"), "YunBridge", 90).dependent = true
	entry({"admin", "services", "yunbridge", "webui"}, template("yunbridge/webui"), "Web UI", 100).dependent = false
end

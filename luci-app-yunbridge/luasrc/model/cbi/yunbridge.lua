m = Map("yunbridge", translate("YunBridge Configuration"))

s = m:section(NamedSection, "main", "yunbridge", translate("Main Settings"))


-- MQTT Host: must be a valid hostname or IP
host = s:option(Value, "mqtt_host", translate("MQTT Host"))
host.datatype = "host"
host.placeholder = "127.0.0.1"
host.rmempty = false

-- MQTT Port: must be integer 1-65535
-- MQTT Topic Prefix: non-empty, basic topic validation
topic = s:option(Value, "mqtt_topic", "MQTT Topic Prefix")
topic.datatype = "string"
topic.placeholder = "yun"
topic.rmempty = false
function topic.validate(self, value, section)
	if not value or #value == 0 then
		return nil, "Topic prefix cannot be empty."
	end
	if value:find("[#+]") then
		return nil, "Topic prefix cannot contain wildcards (# or +)."
	end
	return value
end

-- Serial Port: must be non-empty, suggest /dev/ttyATH0
serial = s:option(Value, "serial_port", "Serial Port")
serial.datatype = "string"
serial.placeholder = "/dev/ttyATH0"
serial.rmempty = false
function serial.validate(self, value, section)
	if not value or #value == 0 then
		return nil, "Serial port cannot be empty."
	end
	if not value:match("^/dev/tty") then
		return nil, "Serial port should start with /dev/tty."
	end
	return value
end

-- Serial Baudrate: must be integer, common baud rates
baud = s:option(Value, "serial_baud", "Serial Baudrate")
baud.datatype = "uinteger"
baud.placeholder = "115200"
baud.rmempty = false
baud:value("9600", "9600")
baud:value("19200", "19200")
baud:value("38400", "38400")
baud:value("57600", "57600")
baud:value("115200", "115200")
baud:value("115200", "115200")
function baud.validate(self, value, section)
	local allowed = { ["9600"]=1, ["19200"]=1, ["38400"]=1, ["57600"]=1, ["115200"]=1 }
	if not allowed[value] then
		return nil, "Invalid baudrate. Choose a standard value."
	end
	return value
end

debug = s:option(Flag, "debug", "Debug Mode")
debug.default = "1"



-- Google Pub/Sub Integration
pubsub_enabled = s:option(Flag, "pubsub_enabled", translate("Enable Google Pub/Sub"))
pubsub_enabled.default = "0"

pubsub_project = s:option(Value, "pubsub_project", translate("Google Cloud Project ID"))
pubsub_project.placeholder = "your-gcp-project-id"
pubsub_project.rmempty = false
function pubsub_project.validate(self, value, section)
	if pubsub_enabled:formvalue(section) == "1" and (not value or #value == 0) then
		return nil, translate("Project ID is required when Pub/Sub is enabled.")
	end
	return value
end

pubsub_topic = s:option(Value, "pubsub_topic", translate("Pub/Sub Topic Name"))
pubsub_topic.placeholder = "your-topic-name"
pubsub_topic.rmempty = false
function pubsub_topic.validate(self, value, section)
	if pubsub_enabled:formvalue(section) == "1" and (not value or #value == 0) then
		return nil, translate("Topic name is required when Pub/Sub is enabled.")
	end
	return value
end

pubsub_subscription = s:option(Value, "pubsub_subscription", translate("Pub/Sub Subscription Name"))
pubsub_subscription.placeholder = "your-subscription-name"
pubsub_subscription.rmempty = false
function pubsub_subscription.validate(self, value, section)
	if pubsub_enabled:formvalue(section) == "1" and (not value or #value == 0) then
		return nil, translate("Subscription name is required when Pub/Sub is enabled.")
	end
	return value
end

pubsub_credentials = s:option(Value, "pubsub_credentials", translate("Service Account Credentials Path"))
pubsub_credentials.placeholder = "/etc/yunbridge/gcp-service-account.json"
pubsub_credentials.rmempty = false
function pubsub_credentials.validate(self, value, section)
	if pubsub_enabled:formvalue(section) == "1" and (not value or #value == 0) then
		return nil, translate("Credentials path is required when Pub/Sub is enabled.")
	end
	if value and #value > 0 and not value:match("%.json$") then
		return nil, translate("Credentials file must be a .json file.")
	end
	return value
end

return m

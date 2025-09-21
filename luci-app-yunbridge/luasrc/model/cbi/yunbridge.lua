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
baud:value("250000", "250000")
function baud.validate(self, value, section)
	local allowed = { ["9600"]=1, ["19200"]=1, ["38400"]=1, ["57600"]=1, ["115200"]=1, ["250000"]=1 }
	if not allowed[value] then
		return nil, "Invalid baudrate. Choose a standard value."
	end
	return value
end

debug = s:option(Flag, "debug", "Debug Mode")
debug.default = "1"

return m

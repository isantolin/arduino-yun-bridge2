sns_region = s:option(Value, "sns_region", translate("AWS Region"))

m = Map("yunbridge", translate("YunBridge Configuration"))
s = m:section(NamedSection, "main", "yunbridge", translate("Main Settings"))

-- Amazon SNS Integration
sns_enabled = s:option(Flag, "sns_enabled", translate("Enable Amazon SNS"))
sns_enabled.default = "0"

sns_region = s:option(Value, "sns_region", translate("AWS Region"))
sns_region.placeholder = "us-east-1"
sns_region.rmempty = false
function sns_region.validate(self, value, section)
	if sns_enabled:formvalue(section) == "1" and (not value or #value == 0) then
		return nil, translate("Region is required when SNS is enabled.")
	end
	return value
end

sns_topic_arn = s:option(Value, "sns_topic_arn", translate("SNS Topic ARN"))
sns_topic_arn.placeholder = "arn:aws:sns:us-east-1:123456789012:YourTopic"
sns_topic_arn.rmempty = false
function sns_topic_arn.validate(self, value, section)
	if sns_enabled:formvalue(section) == "1" and (not value or #value == 0) then
		return nil, translate("Topic ARN is required when SNS is enabled.")
	end
	if value and #value > 0 and not value:match("^arn:aws:sns:") then
		return nil, translate("Topic ARN must start with 'arn:aws:sns:'.")
	end
	return value
end

sns_access_key = s:option(Value, "sns_access_key", translate("AWS Access Key ID"))
sns_access_key.placeholder = "AKIA..."
sns_access_key.rmempty = false
function sns_access_key.validate(self, value, section)
	if sns_enabled:formvalue(section) == "1" and (not value or #value == 0) then
		return nil, translate("Access Key ID is required when SNS is enabled.")
	end
	return value
end

sns_secret_key = s:option(Value, "sns_secret_key", translate("AWS Secret Access Key"))
sns_secret_key.placeholder = "..."
sns_secret_key.password = true
sns_secret_key.rmempty = false
function sns_secret_key.validate(self, value, section)
	if sns_enabled:formvalue(section) == "1" and (not value or #value == 0) then
		return nil, translate("Secret Access Key is required when SNS is enabled.")
	end
	return value
end
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



-- Exclusividad de sistemas de mensajería
function update_backend_exclusivity(section)
	local sns = sns_enabled:formvalue(section) == "1"
	local mqtt = (host:formvalue(section) or "") ~= ""

	-- Deshabilitar dinámicamente los otros backends si uno está activo
	if sns then
		host.readonly = true
		host.description = translate("Disabled: SNS is active")
	elseif mqtt then
		sns_enabled.readonly = true
		sns_enabled.description = translate("Disabled: MQTT is active")
	else
		host.readonly = false
		sns_enabled.description = nil
		host.description = nil
	end
end

-- Hook para actualizar exclusividad en cada render
m.on_parse = function(self)
	local section = "main"
	update_backend_exclusivity(section)
end

-- Validación cruzada al guardar
function m.on_commit(self)
	local section = "main"
	local sns = sns_enabled:formvalue(section) == "1"
	local mqtt = (host:formvalue(section) or "") ~= ""
	local count = 0
	if sns then count = count + 1 end
	if mqtt then count = count + 1 end
	if count > 1 then
		error_msg = translate("Only one messaging backend can be enabled at a time (MQTT, Pub/Sub, or SNS). Please disable the others.")
		self.message = error_msg
		return false
	end
	return true
end

-- Mensaje informativo en la UI
s:option(DummyValue, "backend_exclusivity_info").value = translate("Note: Only one messaging backend (MQTT, Pub/Sub, or SNS) can be enabled at a time. Selecting one will disable the others.")

return m

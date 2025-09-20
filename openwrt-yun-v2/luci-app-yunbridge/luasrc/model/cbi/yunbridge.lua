m = Map("yunbridge", "YunBridge Configuration")

s = m:section(NamedSection, "main", "yunbridge", "Main Settings")

s:option(Value, "mqtt_host", "MQTT Host")
s:option(Value, "mqtt_port", "MQTT Port")
s:option(Value, "mqtt_topic", "MQTT Topic Prefix")
s:option(Value, "serial_port", "Serial Port")
s:option(Value, "serial_baud", "Serial Baudrate")
debug = s:option(Flag, "debug", "Debug Mode")
debug.default = "1"

return m

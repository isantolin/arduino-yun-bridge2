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
-- cbi/yunbridge.lua

-- Define el mapa de configuración para "yunbridge"
m = Map("yunbridge",
    translate("YunBridge Configuration"),
    translate("Configuration for the YunBridge daemon, which bridges MQTT messages to the serial port."))

-- Define la sección principal dentro del mapa
s = m:section(NamedSection, "main", "yunbridge", translate("Main Settings"))

--------------------------------------------------------------------------
-- Opciones de configuración
--------------------------------------------------------------------------

host = s:option(Value, "mqtt_host", translate("MQTT Host"))
host.datatype = "host"
host.placeholder = "localhost"
host.rmempty = false
host.description = translate("Hostname or IP address of the MQTT broker.")

port = s:option(Value, "mqtt_port", translate("MQTT Port"))
port.datatype = "port"
port.placeholder = "1883"
port.rmempty = false
port.description = translate("Port of the MQTT broker. Typically 1883 for non-TLS and 8883 for TLS.")

-- Opción para el usuario MQTT
mqtt_user = s:option(Value, "mqtt_user", translate("MQTT Username"))
mqtt_user.datatype = "string"
mqtt_user.placeholder = ""
mqtt_user.rmempty = true
mqtt_user.description = translate("Optional username for MQTT authentication.")

-- Opción para la contraseña MQTT
mqtt_pass = s:option(Value, "mqtt_pass", translate("MQTT Password"))
mqtt_pass.datatype = "string"
mqtt_pass.placeholder = ""
mqtt_pass.rmempty = true
mqtt_pass.password = true
mqtt_pass.description = translate("Optional password for MQTT authentication.")

-- Opción para habilitar TLS
tls = s:option(Flag, "mqtt_tls", translate("Enable TLS/SSL"))
tls.default = "0"
tls.rmempty = false
tls.description = translate("Encrypt the connection to the MQTT broker. You will need to provide at least a CA file.")

-- Opciones para los archivos de TLS
cafile = s:option(Value, "mqtt_cafile", translate("CA File Path"))
cafile.datatype = "string"
cafile.placeholder = "/etc/yunbridge/ca.crt"
cafile.rmempty = true
cafile:depends("mqtt_tls", "1")
cafile.description = translate("Path to the Certificate Authority file for TLS validation.")

certfile = s:option(Value, "mqtt_certfile", translate("Client Certificate Path"))
certfile.datatype = "string"
certfile.placeholder = "/etc/yunbridge/client.crt"
certfile.rmempty = true
certfile:depends("mqtt_tls", "1")
certfile.description = translate("Path to the client certificate file for mutual TLS authentication.")

keyfile = s:option(Value, "mqtt_keyfile", translate("Client Key Path"))
keyfile.datatype = "string"
keyfile.placeholder = "/etc/yunbridge/client.key"
keyfile.rmempty = true
keyfile:depends("mqtt_tls", "1")
keyfile.description = translate("Path to the client private key file for mutual TLS authentication.")

-- Opción para el prefijo del Topic MQTT
topic = s:option(Value, "mqtt_topic", "MQTT Topic Prefix")
topic.datatype = "string"
topic.placeholder = "yun"
topic.rmempty = false
topic.description = translate("The base topic used for communication. E.g., 'yun' will result in topics like 'yun/d/13'.")
function topic.validate(self, value, section)
    if not value or #value == 0 then
        return nil, "Topic prefix cannot be empty."
    end
    if value:find("[#+]") then
        return nil, "Topic prefix cannot contain wildcards (# or +)."
    end
    return value
end

-- Opción para el puerto serie
serial = s:option(Value, "serial_port", "Serial Port")
serial.datatype = "string"
serial.placeholder = "/dev/ttyATH0"
serial.rmempty = false
serial.description = translate("The serial device connected to the Arduino microcontroller.")
function serial.validate(self, value, section)
    if not value or #value == 0 then
        return nil, "Serial port cannot be empty."
    end
    if not value:match("^/dev/tty") then
        return nil, "Serial port should start with /dev/tty."
    end
    return value
end

-- Opción para la velocidad del puerto serie (Baudrate)
baud = s:option(Value, "serial_baud", "Serial Baudrate")
baud.datatype = "uinteger"
baud.placeholder = "115200"
baud.rmempty = false
baud:value("9600", "9600")
baud:value("19200", "19200")
baud:value("38400", "38400")
baud:value("57600", "57600")
baud:value("115200", "115200")
baud.description = translate("Baud rate for the serial communication. Must match the rate set in the Arduino sketch.")

-- Opción para el puerto WebSocket MQTT
mqtt_ws_port = s:option(Value, "mqtt_ws_port", "MQTT WebSocket Port")
mqtt_ws_port.datatype = "port"
mqtt_ws_port.placeholder = "9001"
mqtt_ws_port.rmempty = true
mqtt_ws_port.description = translate("Optional. If set, enables a WebSocket bridge for direct browser-based MQTT clients.")

-- Opción para la ruta del sistema de archivos
fs_root = s:option(Value, "file_system_root", translate("File System Root"))
fs_root.datatype = "string"
fs_root.placeholder = "/root/yun_files"
fs_root.rmempty = false
fs_root.description = translate("The base directory for file operations requested by the MCU or MQTT.")

-- Opción para habilitar el modo de depuración (Debug)
debug = s:option(Flag, "debug", "Debug Mode")
debug.default = "1"
debug.rmempty = false
debug.description = translate("Enable verbose logging for the bridge daemon. Logs are sent to the system log.")

-- Opción para la lista de comandos permitidos
allowed_commands = s:option(Value, "allowed_commands", translate("Allowed Commands"))
allowed_commands.datatype = "string"
allowed_commands.placeholder = "date uptime reboot"
allowed_commands.rmempty = true
allowed_commands.description = translate("A space-separated list of commands that the daemon is allowed to execute. If empty, no commands are allowed.")


-- Callback para reiniciar el proceso yunbridge al aplicar cambios
function m.on_commit(map)
    os.execute("/etc/init.d/yunbridge restart >/dev/null 2>&1")
end

return m
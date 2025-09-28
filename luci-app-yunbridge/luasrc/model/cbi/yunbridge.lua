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
m = Map("yunbridge", translate("YunBridge Configuration"), translate("Configuration for the YunBridge daemon, which bridges MQTT messages to the serial port."))

-- Define la sección principal dentro del mapa
s = m:section(NamedSection, "main", "yunbridge", translate("Main Settings"))

--------------------------------------------------------------------------
-- Opciones de configuración
--------------------------------------------------------------------------

host = s:option(Value, "mqtt_host", translate("MQTT Host"))
host.datatype = "host"
host.placeholder = "localhost"
host.rmempty = false

-- Opción para el usuario MQTT
mqtt_user = s:option(Value, "mqtt_user", translate("MQTT Username"))
mqtt_user.datatype = "string"
mqtt_user.placeholder = ""
mqtt_user.rmempty = true

-- Opción para la contraseña MQTT
mqtt_pass = s:option(Value, "mqtt_pass", translate("MQTT Password"))
mqtt_pass.datatype = "string"
mqtt_pass.placeholder = ""
mqtt_pass.rmempty = true
mqtt_pass.password = true

-- Opción para el prefijo del Topic MQTT
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

-- Opción para el puerto serie
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
function baud.validate(self, value, section)
    -- CORRECCIÓN: Validar contra una tabla local.
    -- El método 'get_values' no existe en el objeto 'self' de una opción.
    local allowed_baudrates = {
        ["9600"] = true,
        ["19200"] = true,
        ["38400"] = true,
        ["57600"] = true,
        ["115200"] = true
    }
    if not value or not allowed_baudrates[tostring(value)] then
        return nil, "Invalid baudrate. Please choose a standard value from the list."
    end
    return value
end

-- Opción para el puerto WebSocket MQTT
mqtt_ws_port = s:option(Value, "mqtt_ws_port", "MQTT WebSocket Port")
mqtt_ws_port.datatype = "port"
mqtt_ws_port.placeholder = "9001"
mqtt_ws_port.rmempty = true

-- Opción para habilitar el modo de depuración (Debug)
debug = s:option(Flag, "debug", "Debug Mode")
debug.default = "1"
debug.rmempty = false


-- Callback para reiniciar el proceso yunbridge al aplicar cambios
function m.on_commit(map)
    os.execute("/etc/init.d/yunbridge restart >/dev/null 2>&1")
end

return m

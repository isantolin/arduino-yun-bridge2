"""
This file is part of Arduino Yun Ecosystem v2.

Copyright (C) 2025 Ignacio Santolin and contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

#!/usr/bin/env python3
"""
Example: Test mailbox via MQTT (nuevo flujo)
Publica mensajes arbitrarios en yun/mailbox/send y escucha respuestas en yun/mailbox/recv
"""
import time
import paho.mqtt.client as mqtt
try:
	from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
	CallbackAPIVersion = None

BROKER = 'localhost'
PORT = 1883
TOPIC_SEND = 'yun/mailbox/send'
TOPIC_RECV = 'yun/mailbox/recv'

def on_connect(client, userdata, flags, rc, properties=None):
	print("Connected with result code " + str(rc))
	client.subscribe(TOPIC_RECV, qos=2)

def on_message(client, userdata, msg):
	print(f"[MQTT] Received on {msg.topic}: {msg.payload.decode(errors='replace')}")

if CallbackAPIVersion is not None:
	client = mqtt.Client(CallbackAPIVersion.VERSION2)
else:
	client = mqtt.Client()
client.on_connect = on_connect
client.on_message = on_message
client.connect(BROKER, PORT, 60)
client.loop_start()

print("Enviando mensaje a mailbox via MQTT...")
client.publish(TOPIC_SEND, 'hola_desde_mqtt', qos=2)
time.sleep(2)
print("Listo. Esperando posibles respuestas en yun/mailbox/recv...")
time.sleep(3)
client.loop_stop()
client.disconnect()
print("Fin.")

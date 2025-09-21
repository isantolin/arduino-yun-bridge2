def on_connect(client, userdata, flags, rc, properties=None):
	print("Connected with result code " + str(rc))

#!/usr/bin/env python3
"""
Example: Test console via MQTT
Sends a message to a generic MQTT command topic
"""
import time
import paho.mqtt.client as mqtt
try:
	from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
	CallbackAPIVersion = None

BROKER = 'localhost'
PORT = 1883
TOPIC_CMD = 'yun/command'

if CallbackAPIVersion is not None:
	client = mqtt.Client(CallbackAPIVersion.VERSION2)
else:
	client = mqtt.Client()
client.on_connect = on_connect
client.connect(BROKER, PORT, 60)
client.loop_start()

print("Sending CONSOLE command via MQTT...")
client.publish(TOPIC_CMD, 'CONSOLE hello_console')
time.sleep(1)
client.loop_stop()
client.disconnect()
print("Done.")

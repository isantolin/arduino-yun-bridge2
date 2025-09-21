def on_connect(client, userdata, flags, rc, properties=None):
	# This function is called when the client connects to the broker
	print("Connected with result code " + str(rc))
	# Subscribe to a topic or perform other actions here

	# Example of subscribing to a topic
	client.subscribe("some/topic")

	# You can handle properties if needed

	# Additional logic can be added here

	pass  # Placeholder for actual implementation
#!/usr/bin/env python3
"""
Example: Test key-value store via MQTT
Sends SET and GET commands to the yun/command topic
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
client.connect(BROKER, PORT, 60)
client.loop_start()

print("Setting key via MQTT...")
client.publish(TOPIC_CMD, 'SET foo bar')
time.sleep(1)
print("Getting key via MQTT...")
client.publish(TOPIC_CMD, 'GET foo')
time.sleep(1)
client.loop_stop()
client.disconnect()
print("Done.")

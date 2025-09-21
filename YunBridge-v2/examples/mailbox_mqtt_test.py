def on_connect(client, userdata, flags, rc, properties=None):
	print("Connected with result code " + str(rc))

#!/usr/bin/env python3
"""
Example: Test mailbox via MQTT
Sends MAILBOX SEND and MAILBOX RECV commands to the yun/command topic
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

print("Sending to mailbox via MQTT...")
client.publish(TOPIC_CMD, 'MAILBOX SEND hello_mailbox')
time.sleep(1)
print("Receiving from mailbox via MQTT...")
client.publish(TOPIC_CMD, 'MAILBOX RECV')
time.sleep(1)
client.loop_stop()
client.disconnect()
print("Done.")

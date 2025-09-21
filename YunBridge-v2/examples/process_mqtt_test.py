#!/usr/bin/env python3
"""
Example: Test process execution via MQTT
Sends RUN command to the yun/command topic
"""
import time
import paho.mqtt.client as mqtt

BROKER = 'localhost'
PORT = 1883
TOPIC_CMD = 'yun/command'

client = mqtt.Client(protocol=mqtt.MQTTv311, callback_api_version=5)
client.connect(BROKER, PORT, 60)
client.loop_start()

print("Running process via MQTT...")
client.publish(TOPIC_CMD, 'RUN echo hello_from_yun')
time.sleep(1)
client.loop_stop()
client.disconnect()
print("Done.")

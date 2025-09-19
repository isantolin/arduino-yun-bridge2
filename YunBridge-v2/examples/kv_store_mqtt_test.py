#!/usr/bin/env python3
"""
Example: Test key-value store via MQTT
Sends SET and GET commands to the yun/command topic
"""
import time
import paho.mqtt.client as mqtt

BROKER = 'localhost'
PORT = 1883
TOPIC_CMD = 'yun/command'

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

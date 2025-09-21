#!/usr/bin/env python3
"""
Example: Test console via MQTT
Sends a message to a generic MQTT command topic
"""
import time
import paho.mqtt.client as mqtt

BROKER = 'localhost'
PORT = 1883
TOPIC_CMD = 'yun/command'

client = mqtt.Client(protocol=mqtt.MQTTv311, callback_api_version=5)
client.connect(BROKER, PORT, 60)
client.loop_start()

print("Sending CONSOLE command via MQTT...")
client.publish(TOPIC_CMD, 'CONSOLE hello_console')
time.sleep(1)
client.loop_stop()
client.disconnect()
print("Done.")

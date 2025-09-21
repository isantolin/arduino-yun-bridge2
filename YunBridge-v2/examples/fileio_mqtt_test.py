#!/usr/bin/env python3
"""
Example: Test file I/O via MQTT
Sends WRITEFILE and READFILE commands to the yun/command topic
"""
import time
import paho.mqtt.client as mqtt

BROKER = 'localhost'
PORT = 1883
TOPIC_CMD = 'yun/command'

client = mqtt.Client(protocol=mqtt.MQTTv311, callback_api_version=5)
client.connect(BROKER, PORT, 60)
client.loop_start()

TEST_FILE = '/tmp/bridge_test.txt'

print("Writing file via MQTT...")
client.publish(TOPIC_CMD, f'WRITEFILE {TEST_FILE} hello_bridge')
time.sleep(1)
print("Reading file via MQTT...")
client.publish(TOPIC_CMD, f'READFILE {TEST_FILE}')
time.sleep(1)
client.loop_stop()
client.disconnect()
print("Done.")

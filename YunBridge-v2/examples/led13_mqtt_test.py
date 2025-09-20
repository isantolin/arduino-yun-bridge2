#!/usr/bin/env python3
"""
Example: Test generic pin control via MQTT
Sends MQTT messages to control and monitor any pin state (default: 13)
"""
import time
import paho.mqtt.client as mqtt


import sys

BROKER = 'localhost'  # Change if needed
PORT = 1883
PIN = 13  # Default pin
# Pass pin number as first argument, e.g. python3 led13_mqtt_test.py 7
if len(sys.argv) > 1:
    try:
        PIN = int(sys.argv[1])
    except Exception:
        pass
TOPIC_SET = f'yun/pin/{PIN}/set'
TOPIC_STATE = f'yun/pin/{PIN}/state'

# Callback for state updates
def on_message(client, userdata, msg):
    print(f"[MQTT] {msg.topic}: {msg.payload.decode()}")

client = mqtt.Client()
client.on_message = on_message
client.connect(BROKER, PORT, 60)
client.loop_start()
client.subscribe(TOPIC_STATE)


print(f"Turning pin {PIN} ON via MQTT...")
client.publish(TOPIC_SET, 'ON')
time.sleep(2)
print(f"Turning pin {PIN} OFF via MQTT...")
client.publish(TOPIC_SET, 'OFF')
time.sleep(2)
print("Done. Waiting for state updates...")
time.sleep(2)
client.loop_stop()
client.disconnect()

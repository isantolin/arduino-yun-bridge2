#!/usr/bin/env python3
"""
Example: Test LED 13 control via MQTT
Sends MQTT messages to control and monitor LED 13 state
"""
import time
import paho.mqtt.client as mqtt

BROKER = 'localhost'  # Change if needed
PORT = 1883
TOPIC_SET = 'yun/bridge/led13/set'
TOPIC_STATE = 'yun/bridge/led13/state'

# Callback for state updates
def on_message(client, userdata, msg):
    print(f"[MQTT] {msg.topic}: {msg.payload.decode()}")

client = mqtt.Client()
client.on_message = on_message
client.connect(BROKER, PORT, 60)
client.loop_start()
client.subscribe(TOPIC_STATE)

print("Turning LED 13 ON via MQTT...")
client.publish(TOPIC_SET, 'ON')
time.sleep(2)
print("Turning LED 13 OFF via MQTT...")
client.publish(TOPIC_SET, 'OFF')
time.sleep(2)
print("Done. Waiting for state updates...")
time.sleep(2)
client.loop_stop()
client.disconnect()

#!/usr/bin/env python3
"""
Example Community Plugin: Auto Toggle Pin 13

This script toggles pin 13 ON and OFF every 10 seconds using MQTT.
You can use it as a template for your own automations.
"""
import time
import paho.mqtt.client as mqtt

MQTT_HOST = '127.0.0.1'  # Change if needed
MQTT_PORT = 1883
MQTT_TOPIC_SET = 'yun/pin/13/set'
MQTT_USER = ''  # Set if needed
MQTT_PASS = ''  # Set if needed

client = mqtt.Client()
if MQTT_USER:
    client.username_pw_set(MQTT_USER, MQTT_PASS)

client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_start()

try:
    state = True
    while True:
        payload = 'ON' if state else 'OFF'
        print(f"Publishing {payload} to {MQTT_TOPIC_SET}")
        client.publish(MQTT_TOPIC_SET, payload)
        state = not state
        time.sleep(10)
except KeyboardInterrupt:
    print("Exiting plugin.")
finally:
    client.loop_stop()
    client.disconnect()

#!/usr/bin/env python3
"""
Example: Test all MQTT features of YunBridge v2
- LED 13 control
- Key-value store
- File I/O
- Mailbox
- Process execution
"""
import time
import paho.mqtt.client as mqtt

BROKER = 'localhost'
PORT = 1883
TOPIC_SET = 'yun/bridge/led13/set'
TOPIC_STATE = 'yun/bridge/led13/state'
TOPIC_CMD = 'yun/command'

# Callback for all state updates
def on_message(client, userdata, msg):
    print(f"[MQTT] {msg.topic}: {msg.payload.decode()}")

client = mqtt.Client()
client.on_message = on_message
client.connect(BROKER, PORT, 60)
client.loop_start()

# Subscribe to all relevant topics
client.subscribe(TOPIC_STATE)
client.subscribe('yun/command/response')

print("Turning LED 13 ON via MQTT...")
client.publish(TOPIC_SET, 'ON')
time.sleep(1)
print("Turning LED 13 OFF via MQTT...")
client.publish(TOPIC_SET, 'OFF')
time.sleep(1)

print("Setting key foo=bar via MQTT...")
client.publish(TOPIC_CMD, 'SET foo bar')
time.sleep(1)
print("Getting key foo via MQTT...")
client.publish(TOPIC_CMD, 'GET foo')
time.sleep(1)

print("Writing file via MQTT...")
client.publish(TOPIC_CMD, 'WRITEFILE /tmp/bridge_test.txt hello_bridge')
time.sleep(1)
print("Reading file via MQTT...")
client.publish(TOPIC_CMD, 'READFILE /tmp/bridge_test.txt')
time.sleep(1)

print("Sending to mailbox via MQTT...")
client.publish(TOPIC_CMD, 'MAILBOX SEND hello_mailbox')
time.sleep(1)
print("Receiving from mailbox via MQTT...")
client.publish(TOPIC_CMD, 'MAILBOX RECV')
time.sleep(1)

print("Running process via MQTT...")
client.publish(TOPIC_CMD, 'RUN echo hello_from_yun')
time.sleep(2)

print("Done. Waiting for state updates and responses...")
time.sleep(2)
client.loop_stop()
client.disconnect()

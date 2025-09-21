def on_connect(client, userdata, flags, rc, properties=None):
    print("Connected with result code " + str(rc))

#!/usr/bin/env python3
"""
Example: Test all MQTT features of YunBridge v2
- Generic pin control (default: 13, can specify any pin)
- Key-value store
- File I/O
- Mailbox (topics yun/mailbox/send y yun/mailbox/recv)
- Process execution
"""
import time
import paho.mqtt.client as mqtt
try:
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    CallbackAPIVersion = None

import sys


BROKER = 'localhost'
PORT = 1883
PIN = 13  # Default pin
if len(sys.argv) > 1:
    try:
        PIN = int(sys.argv[1])
    except Exception:
        pass
TOPIC_SET = f'yun/pin/{PIN}/set'
TOPIC_STATE = f'yun/pin/{PIN}/state'
TOPIC_CMD = 'yun/command'
TOPIC_MAILBOX_SEND = 'yun/mailbox/send'
TOPIC_MAILBOX_RECV = 'yun/mailbox/recv'


# Callback for all state updates
def on_message(client, userdata, msg):
    print(f"[MQTT] {msg.topic}: {msg.payload.decode()}")

if CallbackAPIVersion is not None:
    client = mqtt.Client(CallbackAPIVersion.VERSION2)
else:
    client = mqtt.Client()
client.on_connect = on_connect
client.on_message = on_message
client.connect(BROKER, PORT, 60)
client.loop_start()

# Subscribe to all relevant topics


client.subscribe(TOPIC_STATE)
client.subscribe('yun/command/response')
client.subscribe(TOPIC_MAILBOX_RECV)

print(f"Turning pin {PIN} ON via MQTT...")
client.publish(TOPIC_SET, 'ON')
time.sleep(1)
print(f"Turning pin {PIN} OFF via MQTT...")
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


print("Sending to mailbox via MQTT (nuevo flujo)...")
client.publish(TOPIC_MAILBOX_SEND, 'hello_mailbox')
time.sleep(1)
print("Waiting for mailbox response on yun/mailbox/recv...")
time.sleep(1)

print("Running process via MQTT...")
client.publish(TOPIC_CMD, 'RUN echo hello_from_yun')
time.sleep(2)

print("Done. Waiting for state updates and responses...")
time.sleep(2)
client.loop_stop()
client.disconnect()

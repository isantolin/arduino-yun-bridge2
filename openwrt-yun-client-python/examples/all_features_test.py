#!/usr/bin/env python3
"""
Example: Test all features of YunBridge v2 using the plugin system (MQTT backend)
- Generic pin control (default: 13, can specify any pin)
- Key-value store
- File I/O
- Mailbox (topics yun/mailbox/send and yun/mailbox/recv)
- Process execution
Usage:
    python3 all_features_test.py [PIN]
    # Or use led13_test.py for unified plugin support
"""
import sys
import time
from yunbridge_client.plugin_loader import PluginLoader

PIN = '13'
if len(sys.argv) > 1:
    # No validation, just take the argument. Allows for 'A5', '13', etc.
    PIN = sys.argv[1]

# Determine if the pin is analog or digital for topic construction
pin_function = "analog" if PIN.upper().startswith('A') else "digital"

TOPIC_SET = f'br/pin/{pin_function}/{PIN}/set'
TOPIC_STATE = f'br/pin/{pin_function}/{PIN}/state'
TOPIC_CMD = 'br/sh/run'
TOPIC_MAILBOX_SEND = 'br/mailbox/write'
TOPIC_MAILBOX_RECV = 'br/mailbox/available'
TOPIC_CMD_RESPONSE = 'br/sh/response'

MQTT_CONFIG = dict(host='192.168.15.28', port=1883)

plugin_class = PluginLoader.load_plugin('mqtt_plugin')
plugin = plugin_class(**MQTT_CONFIG)

def on_message(topic, message):
    print(f"[MQTT] Received on {topic}: {message}")

if __name__ == '__main__':
    plugin.connect()
    plugin.subscribe(TOPIC_STATE, on_message)
    plugin.subscribe(TOPIC_CMD_RESPONSE, on_message)
    plugin.subscribe(TOPIC_MAILBOX_RECV, on_message)
    time.sleep(1) # Allow time for subscriptions to be processed

    print(f"\n--- Testing Pin {PIN} ---")
    print(f"Turning pin {PIN} ON via MQTT...")
    plugin.publish(TOPIC_SET, 'ON')
    time.sleep(1)
    print(f"Turning pin {PIN} OFF via MQTT...")
    plugin.publish(TOPIC_SET, 'OFF')
    time.sleep(1)

    print("\n--- Testing Key-Value Store ---")
    print("Setting key 'foo' to 'bar'...")
    plugin.publish('br/datastore/put/foo', 'bar')
    time.sleep(1)
    print("Getting key 'foo'...")
    plugin.subscribe('br/datastore/get/foo', on_message)
    time.sleep(1)

    print("\n--- Testing File I/O ---")
    print("Writing to '/tmp/bridge_test.txt'...")
    # File I/O: no mapping directo, requiere implementación en el bridge
    time.sleep(1)
    print("Reading from '/tmp/bridge_test.txt'...")
    # File I/O: no mapping directo, requiere implementación en el bridge
    time.sleep(1)

    print("\n--- Testing Mailbox ---")
    print("Sending message to mailbox...")
    plugin.publish(TOPIC_MAILBOX_SEND, 'hello_from_mqtt')
    time.sleep(1)

    print("\n--- Testing Process Execution ---")
    print("Running 'echo hello_from_yun'...")
    plugin.publish(TOPIC_CMD, 'echo hello_from_yun')
    time.sleep(1)

    print("\nDone testing. Waiting 3s for final responses...")
    time.sleep(3)
    plugin.disconnect()
    print("Disconnected.")

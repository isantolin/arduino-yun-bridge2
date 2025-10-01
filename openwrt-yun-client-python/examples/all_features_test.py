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

PIN = 13
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
MQTT_CONFIG = dict(host='localhost', port=1883)

plugin_class = PluginLoader.load_plugin('mqtt_plugin')
plugin = plugin_class(**MQTT_CONFIG)

def on_message(topic, message):
    print(f"[MQTT] {topic}: {message}")

if __name__ == '__main__':
    plugin.connect()
    plugin.subscribe(TOPIC_STATE, on_message)
    plugin.subscribe('yun/command/response', on_message)
    plugin.subscribe(TOPIC_MAILBOX_RECV, on_message)

    print(f"Turning pin {PIN} ON via MQTT...")
    plugin.publish(TOPIC_SET, 'ON')
    time.sleep(1)
    print(f"Turning pin {PIN} OFF via MQTT...")
    plugin.publish(TOPIC_SET, 'OFF')
    time.sleep(1)

    print("Setting key foo=bar via MQTT...")
    plugin.publish(TOPIC_CMD, 'SET foo bar')
    time.sleep(1)
    print("Getting key foo via MQTT...")
    plugin.publish(TOPIC_CMD, 'GET foo')
    time.sleep(1)

    print("Writing file via MQTT...")
    plugin.publish(TOPIC_CMD, 'WRITEFILE /tmp/bridge_test.txt hello_bridge')
    time.sleep(1)
    print("Reading file via MQTT...")
    plugin.publish(TOPIC_CMD, 'READFILE /tmp/bridge_test.txt')
    time.sleep(1)

    print("Sending message to mailbox via MQTT...")
    plugin.publish(TOPIC_MAILBOX_SEND, 'hello_from_mqtt')
    time.sleep(1)

    print("Running process via MQTT...")
    plugin.publish(TOPIC_CMD, 'RUN echo hello_from_yun')
    time.sleep(1)

    print("Done. Waiting for state updates and responses...")
    time.sleep(3)
    plugin.disconnect()



#!/usr/bin/env python3
"""
Example: Test file I/O using the YunBridge plugin system (MQTT backend)
Sends WRITEFILE and READFILE commands to the yun/command topic
Usage:
    python3 fileio_mqtt_test.py
    # Or use led13_test.py for unified plugin support
"""
import sys
import time
from yunbridge_client.plugin_loader import PluginLoader

TOPIC_CMD = 'yun/command'
TOPIC_CMD_RESPONSE = 'yun/command/response'
TEST_FILE = '/tmp/bridge_test.txt'

# Example: MQTT plugin (default)
MQTT_CONFIG = dict(host='192.168.15.28', port=1883)

plugin_class = PluginLoader.load_plugin('mqtt_plugin')
plugin = plugin_class(**MQTT_CONFIG)

def on_message(topic, message):
    """Callback to print responses from the bridge."""
    print(f"[MQTT] Response on {topic}: {message}")

plugin.connect()
plugin.subscribe(TOPIC_CMD_RESPONSE, on_message)
time.sleep(1) # Allow time for subscription

print("Writing file via MQTT...")
plugin.publish(TOPIC_CMD, f'WRITEFILE {TEST_FILE} hello_bridge')
time.sleep(1) # Wait for response

print("Reading file via MQTT...")
plugin.publish(TOPIC_CMD, f'READFILE {TEST_FILE}')
time.sleep(1) # Wait for response

plugin.disconnect()
print("Done.")

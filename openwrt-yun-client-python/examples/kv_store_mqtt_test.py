

#!/usr/bin/env python3
"""
Example: Test key-value store using the YunBridge plugin system (MQTT backend)
Sends SET and GET commands to the yun/command topic
Usage:
    python3 kv_store_mqtt_test.py
    # Or use led13_test.py for unified plugin support
"""
import sys
import time
from yunbridge_client.plugin_loader import PluginLoader

TOPIC_CMD = 'yun/command'
TOPIC_CMD_RESPONSE = 'yun/command/response'

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

print("Setting key 'foo' to 'bar' via MQTT...")
plugin.publish('br/datastore/put/foo', 'bar')
time.sleep(1) # Wait for response

print("Getting key 'foo' via MQTT...")
plugin.subscribe('br/datastore/get/foo', on_message)
time.sleep(1) # Wait for response

plugin.disconnect()
print("Done.")

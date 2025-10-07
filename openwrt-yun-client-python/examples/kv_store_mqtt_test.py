#!/usr/bin/env python3
"""
Example: Test key-value store using the YunBridge plugin system (MQTT backend).
Uses the 'br/datastore/put/<key>' and 'br/datastore/get/<key>' topics.
"""
import sys
import time
from yunbridge_client.plugin_loader import PluginLoader

# --- Configuration ---
KEY_TO_TEST = 'foo'
VALUE_TO_TEST = 'bar'
TOPIC_PUT = f'br/datastore/put/{KEY_TO_TEST}'
TOPIC_GET = f'br/datastore/get/{KEY_TO_TEST}'

MQTT_CONFIG = dict(host='192.168.15.28', port=1883)

def on_get_response(topic, message):
    """Callback to print the value received from a 'get' command."""
    print(f"[MQTT] Received value for key '{KEY_TO_TEST}' on topic {topic}: {message}")

if __name__ == '__main__':
    plugin = None
    try:
        plugin_class = PluginLoader.load_plugin('mqtt_plugin')
        plugin = plugin_class(**MQTT_CONFIG)
        plugin.connect()

        # Subscribe to the 'get' topic to receive the value after we request it.
        plugin.subscribe(TOPIC_GET, on_get_response)
        print(f"Subscribed to {TOPIC_GET} to wait for value.")
        time.sleep(1)

        # --- Test PUT ---
        print(f"Setting key '{KEY_TO_TEST}' to '{VALUE_TO_TEST}' via MQTT...")
        plugin.publish(TOPIC_PUT, VALUE_TO_TEST)
        # The daemon will now publish the new value to the TOPIC_GET topic.
        # Our subscription will catch it.
        print("Waiting 2s for confirmation...")
        time.sleep(2)

        # The 'get' is implicitly tested by the 'put', as the daemon publishes
        # the new value on the 'get' topic.

    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        if plugin:
            plugin.disconnect()
        print("Done.")

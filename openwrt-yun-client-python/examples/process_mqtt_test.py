#!/usr/bin/env python3
"""
Example: Test process execution using the YunBridge plugin system (MQTT backend).
Sends a command to the br/sh/run topic.
"""
import time
import sys
from yunbridge_client.plugin_loader import PluginLoader

# Configuration
TOPIC_CMD = 'br/sh/run'
TOPIC_CMD_RESPONSE = 'br/sh/response'
MQTT_CONFIG = dict(host='192.168.15.28', port=1883)

def on_response(topic, message):
    """Callback to print responses from the bridge."""
    print(f"[MQTT] Response on {topic}:\n{message}")

if __name__ == '__main__':
    plugin = None
    try:
        # Load and instantiate the plugin
        plugin_class = PluginLoader.load_plugin('mqtt_plugin')
        plugin = plugin_class(**MQTT_CONFIG)
        plugin.connect()

        # Subscribe to the response topic
        plugin.subscribe(TOPIC_CMD_RESPONSE, on_response)
        print(f"Subscribed to {TOPIC_CMD_RESPONSE}")
        time.sleep(1) # Allow time for subscription

        command_to_run = 'echo hello_from_yun && sleep 1 && date'
        print(f"Sending command to '{TOPIC_CMD}': '{command_to_run}'")
        plugin.publish(TOPIC_CMD, command_to_run)
        
        print("Waiting 3s for responses...")
        time.sleep(3) # Give a moment for the command to be processed and response sent

    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        if plugin:
            plugin.disconnect()
        print("Done.")

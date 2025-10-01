#!/usr/bin/env python3
"""
Example: Test process execution using the YunBridge plugin system (MQTT backend).
Sends a RUN command to the yun/command topic.
"""
import time
import sys
from yunbridge_client.plugin_loader import PluginLoader

# Configuration
TOPIC_CMD = 'yun/command'
TOPIC_CMD_RESPONSE = 'yun/command/response'
MQTT_CONFIG = dict(host='192.168.15.28', port=1883)

# Load and instantiate the plugin
try:
    plugin_class = PluginLoader.load_plugin('mqtt_plugin')
    plugin = plugin_class(**MQTT_CONFIG)
except ValueError as e:
    print(f"Error loading plugin: {e}")
    sys.exit(1)

def on_response(topic, message):
    """Callback to print responses from the bridge."""
    print(f"[MQTT] Response on {topic}: {message}")

if __name__ == '__main__':
    try:
        plugin.connect()

        # Subscribe to a potential response topic
        plugin.subscribe(TOPIC_CMD_RESPONSE, on_response)
        time.sleep(1) # Allow time for subscription

        command_to_run = 'RUN echo hello_from_yun'
        print(f"Sending command to '{TOPIC_CMD}': '{command_to_run}'")
        plugin.publish(TOPIC_CMD, command_to_run)
        
        print("Waiting for responses...")
        time.sleep(2) # Give a moment for the command to be processed and response sent

    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        if plugin:
            plugin.disconnect()
        print("Done.")

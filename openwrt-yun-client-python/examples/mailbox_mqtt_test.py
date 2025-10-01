
#!/usr/bin/env python3
"""
Example: Test mailbox feature using the YunBridge plugin system (MQTT backend).
Publishes a message to yun/mailbox/send and listens for responses on yun/mailbox/recv.
"""
import sys
import time
from yunbridge_client.plugin_loader import PluginLoader

# Configuration
TOPIC_SEND = 'yun/mailbox/send'
TOPIC_RECV = 'yun/mailbox/recv'
MQTT_CONFIG = dict(host='localhost', port=1883)

# Load and instantiate the plugin
try:
    plugin_class = PluginLoader.load_plugin('mqtt_plugin')
    plugin = plugin_class(**MQTT_CONFIG)
except ValueError as e:
    print(f"Error loading plugin: {e}")
    sys.exit(1)

def on_message_received(topic, message):
    """Callback function to handle incoming messages."""
    print(f"[MQTT] Received on {topic}: {message}")

if __name__ == '__main__':
    try:
        # Connect to the broker
        plugin.connect()

        # Subscribe to the receive topic
        plugin.subscribe(TOPIC_RECV, on_message_received)
        print(f"Subscribed to {TOPIC_RECV} to listen for responses.")
        time.sleep(1) # Give time for subscription to be acknowledged

        # Publish a message to the send topic
        message_to_send = 'hello_from_plugin'
        print(f"Sending message to {TOPIC_SEND}: '{message_to_send}'")
        plugin.publish(TOPIC_SEND, message_to_send)

        # Wait for a few seconds to receive potential responses
        print("Waiting for responses...")
        time.sleep(3)

    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        # Disconnect gracefully
        if 'plugin' in locals() and plugin:
            plugin.disconnect()
        print("Done.")

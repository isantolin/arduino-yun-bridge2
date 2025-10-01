
#!/usr/bin/env python3
"""
Example: Test console command using the YunBridge plugin system (MQTT backend)
Sends a message to the yun/command topic
Usage:
    python3 console_mqtt_test.py
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

print("Sending CONSOLE command via MQTT...")
plugin.publish(TOPIC_CMD, 'CONSOLE hello_console')
time.sleep(1) # Wait for response

plugin.disconnect()
print("Done.")

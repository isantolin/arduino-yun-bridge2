
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

# The daemon expects console messages on this specific topic
TOPIC_CONSOLE_IN = 'br/console/in'

# Example: MQTT plugin (default)
MQTT_CONFIG = dict(host='192.168.15.28', port=1883)

plugin_class = PluginLoader.load_plugin('mqtt_plugin')
plugin = plugin_class(**MQTT_CONFIG)

# The daemon does not publish a response for console messages,
# so a callback is not necessary for this test.

plugin.connect()

message_to_send = 'hello from console test'
print(f"Sending CONSOLE command via MQTT to {TOPIC_CONSOLE_IN}...")
plugin.publish(TOPIC_CONSOLE_IN, message_to_send)
time.sleep(1) # Wait for message to be sent

plugin.disconnect()
print("Done.")

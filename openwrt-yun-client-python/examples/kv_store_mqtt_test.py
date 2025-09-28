

#!/usr/bin/env python3
"""
Example: Test key-value store using the YunBridge plugin system (MQTT backend)
Sends SET and GET commands to the yun/command topic
Usage:
    python3 kv_store_mqtt_test.py
    # Or use led13_test.py for unified plugin support
"""
import time
from yunbridge_client.plugin_loader import PluginLoader

TOPIC_CMD = 'yun/command'

# Example: MQTT plugin (default)
MQTT_CONFIG = dict(host='localhost', port=1883)

PluginClass = PluginLoader.load_plugin('mqtt_plugin')
plugin = PluginClass(**MQTT_CONFIG)

plugin.connect()
print("Setting key via MQTT...")
plugin.publish(TOPIC_CMD, 'SET foo bar')  # plugin uses QoS 2
time.sleep(1)
print("Getting key via MQTT...")
plugin.publish(TOPIC_CMD, 'GET foo')  # plugin uses QoS 2
time.sleep(1)
plugin.disconnect()
print("Done.")

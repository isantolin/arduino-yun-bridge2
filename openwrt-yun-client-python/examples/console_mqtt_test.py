
#!/usr/bin/env python3
"""
Example: Test console command using the YunBridge plugin system (MQTT backend)
Sends a message to the yun/command topic
Usage:
    python3 console_mqtt_test.py
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
print("Sending CONSOLE command via MQTT...")
plugin.publish(TOPIC_CMD, 'CONSOLE hello_console')  # plugin uses QoS 2
time.sleep(1)
plugin.disconnect()
print("Done.")

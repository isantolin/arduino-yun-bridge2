

#!/usr/bin/env python3
"""
Example: Test file I/O using the YunBridge plugin system (MQTT backend)
Sends WRITEFILE and READFILE commands to the yun/command topic
Usage:
    python3 fileio_mqtt_test.py
    # Or use led13_test.py for unified plugin support
"""
import time
from yunbridge_client.plugin_loader import PluginLoader

TOPIC_CMD = 'yun/command'
TEST_FILE = '/tmp/bridge_test.txt'

# Example: MQTT plugin (default)
MQTT_CONFIG = dict(host='localhost', port=1883)

plugin_class = PluginLoader.load_plugin('mqtt_plugin')
plugin = plugin_class(**MQTT_CONFIG)

plugin.connect()
print("Writing file via MQTT...")
plugin.publish(TOPIC_CMD, f'WRITEFILE {TEST_FILE} hello_bridge')  # plugin uses QoS 2
time.sleep(1)
print("Reading file via MQTT...")
plugin.publish(TOPIC_CMD, f'READFILE {TEST_FILE}')  # plugin uses QoS 2
time.sleep(1)
plugin.disconnect()
print("Done.")

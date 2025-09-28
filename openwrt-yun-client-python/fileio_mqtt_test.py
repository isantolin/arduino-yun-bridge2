"""
This file is part of Arduino Yun Ecosystem v2.

Copyright (C) 2025 Ignacio Santolin and contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""


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

PluginClass = PluginLoader.load_plugin('mqtt_plugin')
plugin = PluginClass(**MQTT_CONFIG)

plugin.connect()
print("Writing file via MQTT...")
plugin.publish(TOPIC_CMD, f'WRITEFILE {TEST_FILE} hello_bridge')  # plugin uses QoS 2
time.sleep(1)
print("Reading file via MQTT...")
plugin.publish(TOPIC_CMD, f'READFILE {TEST_FILE}')  # plugin uses QoS 2
time.sleep(1)
plugin.disconnect()
print("Done.")

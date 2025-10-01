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
Example: Test generic pin control using the YunBridge plugin system (MQTT backend)
Sends messages to control and monitor any pin state (default: 13)
Usage:
    python3 led13_mqtt_test.py [PIN]
    # Or use led13_test.py for unified plugin support
"""
import sys
import time
from yunbridge_client.plugin_loader import PluginLoader

PIN = 13
if len(sys.argv) > 1:
    try:
        PIN = int(sys.argv[1])
    except Exception:
        pass

TOPIC_SET = f'yun/pin/{PIN}/set'
TOPIC_STATE = f'yun/pin/{PIN}/state'

# Example: MQTT plugin (default)
MQTT_CONFIG = dict(host='192.168.15.28', port=1883)

plugin_class = PluginLoader.load_plugin('mqtt_plugin')
plugin = plugin_class(**MQTT_CONFIG)


def on_message(topic, message):
    print(f"[MQTT] Received on {topic}: {message}")

plugin.connect()

# Subscribe to the state topic to receive feedback
plugin.subscribe(TOPIC_STATE, on_message)

print(f"Turning pin {PIN} ON via MQTT...")
plugin.publish(TOPIC_SET, 'ON')
time.sleep(2)

print(f"Turning pin {PIN} OFF via MQTT...")
plugin.publish(TOPIC_SET, 'OFF')
time.sleep(2)

print("Done. Disconnecting...")
plugin.disconnect()

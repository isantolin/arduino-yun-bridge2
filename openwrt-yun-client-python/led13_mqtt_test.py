

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
MQTT_CONFIG = dict(host='localhost', port=1883)
PluginClass = PluginLoader.load_plugin('mqtt_plugin')
plugin = PluginClass(**MQTT_CONFIG)

# Example: SNS plugin (uncomment to use)
# SNS_CONFIG = dict(region='us-east-1', topic_arn='arn:aws:sns:us-east-1:123456789012:YourTopic', access_key='AKIA...', secret_key='...')
# PluginClass = PluginLoader.load_plugin('sns_plugin')
# plugin = PluginClass(**SNS_CONFIG)


def on_message(topic, message):
    print(f"[MQTT] {topic}: {message}")

plugin.connect()
plugin.subscribe(TOPIC_STATE, on_message)

print(f"Turning pin {PIN} ON via MQTT...")
plugin.publish(TOPIC_SET, 'ON')
time.sleep(2)
print(f"Turning pin {PIN} OFF via MQTT...")
plugin.publish(TOPIC_SET, 'OFF')
time.sleep(2)
print("Done. Waiting for state updates...")
time.sleep(2)
plugin.disconnect()

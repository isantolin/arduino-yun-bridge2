


#!/usr/bin/env python3
"""
Example: Test mailbox using the YunBridge plugin system (MQTT backend)
Publishes messages to yun/mailbox/send and listens for responses on yun/mailbox/recv
Usage:
    python3 mailbox_mqtt_test.py
    # Or use led13_test.py for unified plugin support
"""
import time
from yunbridge_client.plugin_loader import PluginLoader

TOPIC_SEND = 'yun/mailbox/send'
TOPIC_RECV = 'yun/mailbox/recv'

# Example: MQTT plugin (default)
MQTT_CONFIG = dict(host='localhost', port=1883)
PluginClass = PluginLoader.load_plugin('mqtt_plugin')
plugin = PluginClass(**MQTT_CONFIG)

# Example: SNS plugin (uncomment to use)
# SNS_CONFIG = dict(region='us-east-1', topic_arn='arn:aws:sns:us-east-1:123456789012:YourTopic', access_key='AKIA...', secret_key='...')
# PluginClass = PluginLoader.load_plugin('sns_plugin')
# plugin = PluginClass(**SNS_CONFIG)



def on_message(topic, message):
    print(f"[MQTT] Received on {topic}: {message}")

if __name__ == '__main__':
    plugin.connect()
    plugin.subscribe(TOPIC_RECV, on_message)
    print("Sending message to mailbox via MQTT...")
    plugin.publish(TOPIC_SEND, 'hello_from_mqtt')
    time.sleep(2)
    print("Done. Waiting for possible responses on yun/mailbox/recv...")
    time.sleep(3)
    plugin.disconnect()
    print("Finished.")

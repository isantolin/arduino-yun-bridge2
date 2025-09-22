#!/usr/bin/env python3
"""
Example: Test process execution using the YunBridge plugin system (MQTT backend)
Sends RUN command to the yun/command topic
Usage:
    python3 process_mqtt_test.py
    # Or use led13_test.py for unified plugin support
"""
import time
from yunbridge_client.plugin_loader import PluginLoader

TOPIC_CMD = 'yun/command'

# Example: MQTT plugin (default)
MQTT_CONFIG = dict(host='localhost', port=1883)
PluginClass = PluginLoader.load_plugin('mqtt_plugin')
plugin = PluginClass(**MQTT_CONFIG)

# Example: SNS plugin (uncomment to use)
# SNS_CONFIG = dict(region='us-east-1', topic_arn='arn:aws:sns:us-east-1:123456789012:YourTopic', access_key='AKIA...', secret_key='...')
# PluginClass = PluginLoader.load_plugin('sns_plugin')
# plugin = PluginClass(**SNS_CONFIG)

# Example: PubSub plugin (uncomment to use)
# PUBSUB_CONFIG = dict(project_id='your-gcp-project', topic_name='your-topic', subscription_name='your-sub', credentials_path='/path/to/creds.json')
# PluginClass = PluginLoader.load_plugin('pubsub_plugin')
# plugin = PluginClass(**PUBSUB_CONFIG)

plugin.connect()
print("Running process via MQTT...")
plugin.publish(TOPIC_CMD, 'RUN echo hello_from_yun')
time.sleep(1)
plugin.disconnect()
print("Done.")

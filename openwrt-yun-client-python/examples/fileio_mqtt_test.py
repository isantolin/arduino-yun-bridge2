#!/usr/bin/env python3
"""
Example: Test file I/O using the YunBridge plugin system (MQTT backend).
Sends 'fwrite' and 'fread' commands to the mailbox topic 'br/mailbox/write'
and listens for responses on 'br/console/out'.
"""
import sys
import time
from yunbridge_client.plugin_loader import PluginLoader

# The topic to send commands to the Arduino sketch's mailbox
TOPIC_MAILBOX_WRITE = 'br/mailbox/write'

# The Arduino sketch prints file content to the console, which the daemon forwards here
TOPIC_CONSOLE_OUT = 'br/console/out'

TEST_FILE = '/tmp/bridge_test.txt'
TEST_CONTENT = 'hello from fileio_mqtt_test'

# Example: MQTT plugin (default)
MQTT_CONFIG = dict(host='192.168.15.28', port=1883)

def on_console_message(topic, message):
    """Callback to print any messages received from the Arduino console."""
    print(f"[MQTT] Received from {topic}: {message}")

if __name__ == '__main__':
    plugin = None
    try:
        plugin_class = PluginLoader.load_plugin('mqtt_plugin')
        plugin = plugin_class(**MQTT_CONFIG)
        plugin.connect()

        # Subscribe to the console output to see the results of our 'fread' command
        plugin.subscribe(TOPIC_CONSOLE_OUT, on_console_message)
        print(f"Subscribed to {TOPIC_CONSOLE_OUT} to see responses.")
        time.sleep(1)

        # --- Test File Write ---
        print(f"Sending 'fwrite' command to mailbox to write to {TEST_FILE}...")
        write_payload = f'fwrite {TEST_FILE}={TEST_CONTENT}'
        plugin.publish(TOPIC_MAILBOX_WRITE, write_payload)
        # The response will be printed to the console, which we are subscribed to.
        time.sleep(2)

        # --- Test File Read ---
        print(f"Sending 'fread' command to mailbox to read from {TEST_FILE}...")
        read_payload = f'fread {TEST_FILE}'
        plugin.publish(TOPIC_MAILBOX_WRITE, read_payload)
        # The file content will be printed to the console, which we are subscribed to.
        print("Waiting 3s for read response from console...")
        time.sleep(3)

    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        if plugin:
            plugin.disconnect()
        print("Done.")

def on_connect(client, userdata, flags, rc, properties=None):
    print("Connected with result code " + str(rc))
    # Subscribe to a topic or perform other actions here

    # Example of subscribing to a topic
    client.subscribe("some/topic")

    # Additional logic can be added here

    # This is just a placeholder for the function's body

    pass  # Replace with actual logic as needed
#!/usr/bin/env python3
"""
Example: Test file I/O via MQTT
Sends WRITEFILE and READFILE commands to the yun/command topic
"""
import time
import paho.mqtt.client as mqtt
try:
	from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
	CallbackAPIVersion = None

BROKER = 'localhost'
PORT = 1883
TOPIC_CMD = 'yun/command'

if CallbackAPIVersion is not None:
	client = mqtt.Client(CallbackAPIVersion.VERSION2)
else:
	client = mqtt.Client()
client.connect(BROKER, PORT, 60)
client.loop_start()

TEST_FILE = '/tmp/bridge_test.txt'

print("Writing file via MQTT...")
client.publish(TOPIC_CMD, f'WRITEFILE {TEST_FILE} hello_bridge')
time.sleep(1)
print("Reading file via MQTT...")
client.publish(TOPIC_CMD, f'READFILE {TEST_FILE}')
time.sleep(1)
client.loop_stop()
client.disconnect()
print("Done.")

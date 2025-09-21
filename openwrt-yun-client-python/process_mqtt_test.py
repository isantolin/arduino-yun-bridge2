
#!/usr/bin/env python3
"""
Example: Test process execution via MQTT
Sends RUN command to the yun/command topic
"""
import time
import paho.mqtt.client as mqtt
try:
	from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
	CallbackAPIVersion = None

def on_connect(client, userdata, flags, rc, properties=None):
	print("Connected with result code " + str(rc))

def main():
	BROKER = 'localhost'
	PORT = 1883
	TOPIC_CMD = 'yun/command'

	if CallbackAPIVersion is not None:
		client = mqtt.Client(CallbackAPIVersion.VERSION2)
	else:
		client = mqtt.Client()
	client.on_connect = on_connect
	client.connect(BROKER, PORT, 60)
	client.loop_start()

	print("Running process via MQTT...")
	client.publish(TOPIC_CMD, 'RUN echo hello_from_yun')
	time.sleep(1)
	client.loop_stop()
	client.disconnect()
	print("Done.")

if __name__ == '__main__':
	main()

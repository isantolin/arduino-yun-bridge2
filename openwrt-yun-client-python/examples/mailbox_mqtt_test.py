
#!/usr/bin/env python3
"""
Example: Test mailbox via MQTT (nuevo flujo)
Publica mensajes arbitrarios en yun/mailbox/send y escucha respuestas en yun/mailbox/recv
"""
import time
import paho.mqtt.client as mqtt
try:
	from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
	CallbackAPIVersion = None

BROKER = 'localhost'
PORT = 1883
TOPIC_SEND = 'yun/mailbox/send'
TOPIC_RECV = 'yun/mailbox/recv'

def on_connect(client, userdata, flags, rc, properties=None):
	print("Connected with result code " + str(rc))
	client.subscribe(TOPIC_RECV, qos=2)

def on_message(client, userdata, msg):
	print(f"[MQTT] Received on {msg.topic}: {msg.payload.decode(errors='replace')}")

if CallbackAPIVersion is not None:
	client = mqtt.Client(CallbackAPIVersion.VERSION2)
else:
	client = mqtt.Client()
client.on_connect = on_connect
client.on_message = on_message
client.connect(BROKER, PORT, 60)
client.loop_start()

print("Enviando mensaje a mailbox via MQTT...")
client.publish(TOPIC_SEND, 'hola_desde_mqtt', qos=2)
time.sleep(2)
print("Listo. Esperando posibles respuestas en yun/mailbox/recv...")
time.sleep(3)
client.loop_stop()
client.disconnect()
print("Fin.")

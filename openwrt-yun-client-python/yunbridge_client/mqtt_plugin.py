"""
MQTT Messaging Plugin for YunBridge Client
"""
import paho.mqtt.client as mqtt
from .plugin_base import MessagingPluginBase

class MQTTPlugin(MessagingPluginBase):
    def __init__(self, host, port=1883, username=None, password=None, tls=False, cafile=None):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.tls = tls
        self.cafile = cafile
        self.client = mqtt.Client()

    def connect(self):
        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)
        if self.tls:
            self.client.tls_set(ca_certs=self.cafile)
        self.client.connect(self.host, self.port)
        self.client.loop_start()

    def publish(self, topic, message):
        self.client.publish(topic, message)

    def subscribe(self, topic, callback):
        def on_message(client, userdata, msg):
            callback(msg.topic, msg.payload.decode())
        self.client.subscribe(topic)
        self.client.on_message = on_message

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()

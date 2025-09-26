"""
MQTT Messaging Plugin for YunBridge Client
Improvements: rotating logging, configuration validation, robust error handling.
"""
import paho.mqtt.client as mqtt
from .plugin_base import MessagingPluginBase
import logging
from logging.handlers import RotatingFileHandler

 # Global rotating logging configuration for the plugin
LOG_PATH = '/tmp/yunbridge_mqtt_plugin.log'
handler = RotatingFileHandler(LOG_PATH, maxBytes=1000000, backupCount=3)
formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
handler.setFormatter(formatter)
logger = logging.getLogger("yunbridge.mqtt_plugin")
logger.setLevel(logging.DEBUG)  # Always log debug info for troubleshooting
if not logger.hasHandlers():
    logger.addHandler(handler)

class MQTTPlugin(MessagingPluginBase):
    def __init__(self, host, port=1883, username=None, password=None, tls=False, cafile=None):
        if not host:
            raise ValueError("MQTT host is required")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.tls = tls
        self.cafile = cafile
        self.client = mqtt.Client()

    def connect(self):
        try:
            if self.username and self.password:
                self.client.username_pw_set(self.username, self.password)
            if self.tls:
                if not self.cafile:
                    raise ValueError("CA file required for TLS connection")
                self.client.tls_set(ca_certs=self.cafile)
            self.client.connect(self.host, self.port)
            self.client.loop_start()
            logger.info(f"Connected to MQTT broker {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"MQTT connect error: {e}")
            raise

    def publish(self, topic, message):
        if not topic or message is None:
            logger.error("MQTT publish: topic and message are required")
            raise ValueError("MQTT publish: topic and message are required")
        try:
            self.client.publish(topic, message)
            logger.debug(f"Published to {topic}: {message}")
        except Exception as e:
            logger.error(f"MQTT publish error: {e}")
            raise

    def subscribe(self, topic, callback):
        if not topic or not callable(callback):
            logger.error("MQTT subscribe: valid topic and callback required")
            raise ValueError("MQTT subscribe: valid topic and callback required")
        def on_message(client, userdata, msg):
            try:
                callback(msg.topic, msg.payload.decode())
            except Exception as e:
                logger.error(f"MQTT callback error: {e}")
        try:
            self.client.subscribe(topic)
            self.client.on_message = on_message
            logger.info(f"Subscribed to {topic}")
        except Exception as e:
            logger.error(f"MQTT subscribe error: {e}")
            raise

    def disconnect(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
            logger.info("MQTT disconnected")
        except Exception as e:
            logger.warning(f"MQTT disconnect error: {e}")

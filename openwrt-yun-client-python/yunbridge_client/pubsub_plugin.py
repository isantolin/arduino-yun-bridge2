"""
Google Pub/Sub Messaging Plugin for YunBridge Client
Mejoras: logging rotativo, validación de configuración, manejo robusto de errores.
"""
from .plugin_base import MessagingPluginBase
import logging
import threading
import time
from logging.handlers import RotatingFileHandler

LOG_PATH = '/tmp/yunbridge_pubsub_plugin.log'
handler = RotatingFileHandler(LOG_PATH, maxBytes=1000000, backupCount=3)
formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
handler.setFormatter(formatter)
logger = logging.getLogger("yunbridge.pubsub_plugin")
logger.setLevel(logging.INFO)  # Cambia a DEBUG para más detalle
if not logger.hasHandlers():
    logger.addHandler(handler)

class PubSubPlugin(MessagingPluginBase):
    def __init__(self, project_id, topic_name, subscription_name, credentials_path):
        if not (project_id and topic_name and subscription_name and credentials_path):
            raise ValueError("All Pub/Sub config params are required")
        self.project_id = project_id
        self.topic_name = topic_name
        self.subscription_name = subscription_name
        self.credentials_path = credentials_path
        self.publisher = None
        self.subscriber = None
        self._stop_event = threading.Event()
        self._thread = None

    def connect(self):
        try:
            import os
            from google.cloud import pubsub_v1
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self.credentials_path
            self.publisher = pubsub_v1.PublisherClient()
            self.subscriber = pubsub_v1.SubscriberClient()
            logger.info("Connected to Google Pub/Sub")
        except Exception as e:
            logger.error(f"PubSub connect error: {e}")
            raise

    def publish(self, topic, message):
        if not topic or message is None:
            logger.error("PubSub publish: topic and message are required")
            raise ValueError("PubSub publish: topic and message are required")
        if self.publisher is None:
            raise RuntimeError("Publisher client is not initialized. Call connect() before publish().")
        try:
            topic_path = self.publisher.topic_path(self.project_id, topic or self.topic_name)
            self.publisher.publish(topic_path, message.encode())
            logger.debug(f"Published to {topic_path}: {message}")
        except Exception as e:
            logger.error(f"PubSub publish error: {e}")
            raise

    def subscribe(self, topic, callback):
        if not topic or not callable(callback):
            logger.error("PubSub subscribe: topic y callback válidos requeridos")
            raise ValueError("PubSub subscribe: topic y callback válidos requeridos")
        if self.subscriber is None:
            raise RuntimeError("Subscriber client is not initialized. Call connect() before subscribe().")
        subscription_path = self.subscriber.subscription_path(self.project_id, topic or self.subscription_name)
        def _listen():
            if self.subscriber is None:
                raise RuntimeError("Subscriber client is not initialized. Call connect() before subscribe.")
            def _callback(message):
                try:
                    callback(topic, message.data.decode())
                except Exception as e:
                    logger.error(f"PubSub callback error: {e}")
                message.ack()
            try:
                streaming_pull_future = self.subscriber.subscribe(subscription_path, callback=_callback)
                while not self._stop_event.is_set():
                    time.sleep(0.5)
                streaming_pull_future.cancel()
            except Exception as e:
                logger.error(f"PubSub subscribe error: {e}")
        self._thread = threading.Thread(target=_listen, daemon=True)
        self._thread.start()

    def disconnect(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join()
        logger.info("PubSub disconnected")

"""
Google Pub/Sub Messaging Plugin for YunBridge Client
"""
from .plugin_base import MessagingPluginBase
from google.cloud import pubsub_v1
import threading
import time

class PubSubPlugin(MessagingPluginBase):
    def __init__(self, project_id, topic_name, subscription_name, credentials_path):
        self.project_id = project_id
        self.topic_name = topic_name
        self.subscription_name = subscription_name
        self.credentials_path = credentials_path
        self.publisher = None
        self.subscriber = None
        self._stop_event = threading.Event()
        self._thread = None

    def connect(self):
        import os
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self.credentials_path
        self.publisher = pubsub_v1.PublisherClient()
        self.subscriber = pubsub_v1.SubscriberClient()

    def publish(self, topic, message):
        if self.publisher is None:
            raise RuntimeError("Publisher client is not initialized. Call connect() before publish().")
        topic_path = self.publisher.topic_path(self.project_id, topic or self.topic_name)
        self.publisher.publish(topic_path, message.encode())

    def subscribe(self, topic, callback):
        if self.subscriber is None:
            raise RuntimeError("Subscriber client is not initialized. Call connect() before subscribe().")
        subscription_path = self.subscriber.subscription_path(self.project_id, topic or self.subscription_name)
        def _listen():
            if self.subscriber is None:
                raise RuntimeError("Subscriber client is not initialized. Call connect() before subscribe().")
            def _callback(message):
                callback(topic, message.data.decode())
                message.ack()
            streaming_pull_future = self.subscriber.subscribe(subscription_path, callback=_callback)
            while not self._stop_event.is_set():
                time.sleep(0.5)
            streaming_pull_future.cancel()
        self._thread = threading.Thread(target=_listen, daemon=True)
        self._thread.start()

    def disconnect(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join()

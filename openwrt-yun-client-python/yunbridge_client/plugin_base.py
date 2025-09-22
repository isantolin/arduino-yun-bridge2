"""
YunBridge Client Messaging Plugin Interface

All messaging system plugins must implement the following interface:
- connect()
- publish(topic, message)
- subscribe(topic, callback)
- disconnect()

Plugins should be placed in this directory and follow the naming pattern: <system>_plugin.py
"""

class MessagingPluginBase:
    def connect(self):
        raise NotImplementedError

    def publish(self, topic, message):
        raise NotImplementedError

    def subscribe(self, topic, callback):
        raise NotImplementedError

    def disconnect(self):
        raise NotImplementedError

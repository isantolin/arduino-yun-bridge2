"""
YunBridge Client Plugin Loader

Dynamically loads messaging plugins from the yunbridge_client directory.
"""
import importlib
import os
import sys

PLUGIN_DIR = os.path.dirname(__file__)

class PluginLoader:
    @staticmethod
    def load_plugin(plugin_name):
        sys.path.insert(0, PLUGIN_DIR)
        module = importlib.import_module(f"yunbridge_client.{plugin_name}")
        # Special case for mqtt_plugin: class is named MQTTPlugin
        if plugin_name == 'mqtt_plugin':
            class_name = 'MQTTPlugin'
        else:
            class_name = ''.join([part.capitalize() for part in plugin_name.split('_')]) + 'Plugin'
        return getattr(module, class_name)

"""
This file is part of Arduino Yun Ecosystem v2.

Copyright (C) 2025 Ignacio Santolin and contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
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

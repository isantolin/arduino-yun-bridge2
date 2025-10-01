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

Loads the specified messaging plugin.
"""
from .mqtt_plugin import MqttPlugin

class PluginLoader:
    """
    A simplified plugin loader that provides a specific, known plugin.
    This maintains the plugin architecture for future expansion while removing
    unnecessary dynamic loading complexity for a single-plugin scenario.
    """
    @staticmethod
    def load_plugin(plugin_name):
        """
        Loads and returns the specified plugin class.
        
        Args:
            plugin_name (str): The name of the plugin to load. Currently only
                             'mqtt' is supported.
        
        Returns:
            The plugin class if found, otherwise raises ValueError.
        """
        if plugin_name == 'mqtt':
            return MqttPlugin
        else:
            raise ValueError(f"Unsupported plugin: {plugin_name}")

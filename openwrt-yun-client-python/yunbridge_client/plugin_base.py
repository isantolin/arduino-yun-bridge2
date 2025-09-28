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

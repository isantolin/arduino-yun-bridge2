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
from setuptools import setup

setup(
    name='openwrt-yun-client-python',
    version='1.0.0',
    description='Python client for Arduino Yun v2 Bridge',
    author='isantolin',
    author_email='',
    packages=['yunbridge_client'],
    py_modules=['client'],
    install_requires=[
        'paho-mqtt',
    ],
    entry_points={
        'console_scripts': [
            'yunbridge-client=client:main',
        ],
    },
    classifiers=[
        'Programming Language :: Python :: 3',
        'Operating System :: POSIX :: Linux',
    ],
)

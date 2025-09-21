from setuptools import setup

setup(
    name='openwrt-yun-bridge',
    version='1.0.0',
    description='MQTT <-> Serial bridge daemon for Arduino Yun v2 (OpenWRT)',
    author='isantolin',
    author_email='',
    packages=[],
    py_modules=['src.bridge_daemon'],
    install_requires=[
        'pyserial',
        'paho-mqtt',
        'python3-uci',
    ],
    entry_points={
        'console_scripts': [
            'yunbridge=src.bridge_daemon:main',
        ],
    },
    classifiers=[
        'Programming Language :: Python :: 3',
        'Operating System :: POSIX :: Linux',
    ],
)

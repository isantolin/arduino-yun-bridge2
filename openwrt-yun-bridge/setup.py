from setuptools import setup

setup(
    name='openwrt-yun-bridge',
    version='1.0.0',
    description='MQTT <-> Serial bridge daemon for Arduino Yun v2 (OpenWRT)',
    author='isantolin',
    author_email='',
    packages=[],
    py_modules=['bridge_daemon'],
    install_requires=[
        'pyserial',
        'paho-mqtt',
    ],
    entry_points={
        'console_scripts': [
            'yunbridge=bridge_daemon:main',
        ],
    },
    classifiers=[
        'Programming Language :: Python :: 3',
        'Operating System :: POSIX :: Linux',
    ],
)

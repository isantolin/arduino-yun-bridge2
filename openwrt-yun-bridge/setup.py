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
        'boto3',
    # Pub/Sub (google-cloud-pubsub) and grpcio are not supported on OpenWRT Yun. All related code has been removed.
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

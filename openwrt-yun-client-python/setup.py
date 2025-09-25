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
        'boto3',
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

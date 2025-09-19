#!/bin/bash
# YunBridge v2 install script
# Installs Python3 daemon and sets up systemd service

set -e

# Install dependencies
opkg update
opkg install python3 python3-pip

# Always copy the latest daemon to /usr/bin
if [ -f src/bridge_daemon.py ]; then
    echo "Copying YunBridge daemon..."
    cp src/bridge_daemon.py /usr/bin/yunbridge
    chmod +x /usr/bin/yunbridge
else
    echo "ERROR: src/bridge_daemon.py not found."
fi

echo "YunBridge v2 installed. Use /etc/init.d/bridge-v2 to start the service on OpenWRT."
#!/bin/bash
# YunBridge v2 install script
# Installs Python3 daemon and sets up systemd service

set -e

# Install dependencies
opkg update
opkg install python3 python3-pip

# Copy daemon to /usr/bin
echo "Copying YunBridge daemon..."
cp src/bridge_daemon.py /usr/bin/yunbridge
chmod +x /usr/bin/yunbridge

# Install systemd service
cat <<EOF > /etc/systemd/system/yunbridge.service
[Unit]
Description=YunBridge v2 Daemon
if [ -f src/bridge_daemon.py ]; then
	echo "Copying YunBridge daemon..."
	cp src/bridge_daemon.py /usr/bin/yunbridge
	chmod +x /usr/bin/yunbridge
else
	echo "ERROR: src/bridge_daemon.py not found."
fi

echo "YunBridge v2 installed. Use /etc/init.d/bridge-v2 to start the service on OpenWRT."


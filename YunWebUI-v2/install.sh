#!/bin/bash
# YunWebUI v2 install script
# Deploys web UI to OpenWRT web server

set -e

echo "YunWebUI v2 deployed to $WWW_DST and web server restarted."
WWW_DST="/www/arduino-webui-v2"
if [ ! -d www ]; then
	echo "ERROR: www directory not found."
	exit 1
fi
mkdir -p "$WWW_DST"
cp -r www/* "$WWW_DST/"


if [ -f /etc/init.d/uhttpd ]; then
	/etc/init.d/uhttpd restart
	echo "YunWebUI v2 deployed to $WWW_DST and web server restarted."
else
	echo "YunWebUI v2 deployed to $WWW_DST. Web server not restarted (uhttpd not found)."
fi

echo "Access the YunWebUI at: http://<your-yun-ip>/arduino-webui-v2/"

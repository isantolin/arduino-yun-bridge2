# Install CGI script for LED 13 control
if [ -f scripts/led13_rest_cgi.py ]; then
	mkdir -p /www/cgi-bin
	cp scripts/led13_rest_cgi.py /www/cgi-bin/led13
	chmod +x /www/cgi-bin/led13
	echo "CGI script for LED 13 installed at /www/cgi-bin/led13"
else
	echo "WARNING: scripts/led13_rest_cgi.py not found. CGI script not installed."
fi
#!/bin/bash
# openwrt-yun-v2 install script
# Applies patches and installs integration scripts

set -e

# Patch serial port config for /dev/ttyATH0 @ 115200
# (Example: update /etc/inittab or /etc/config/system as needed)



# Ensure /etc/yunbridge exists
if [ ! -d /etc/yunbridge ]; then
	mkdir -p /etc/yunbridge || { echo "ERROR: Could not create /etc/yunbridge"; exit 1; }
fi



# Copy config and package files
for f in package/99-bridge-ttyath0.conf package/README.md package/bridge-v2.files; do
	if [ -f "$f" ]; then
		cp "$f" /etc/yunbridge/
	else
		echo "WARNING: $f not found."
	fi
done


# Install init script to /etc/init.d
if [ -f package/bridge-v2.init ]; then
	cp package/bridge-v2.init /etc/init.d/bridge-v2
	chmod +x /etc/init.d/bridge-v2
else
	echo "ERROR: package/bridge-v2.init not found."
fi



# Copy scripts
if [ -d scripts ]; then
	for f in scripts/*; do
		if [ -f "$f" ]; then
			cp "$f" /usr/bin/
		fi
	done
else
	echo "WARNING: scripts directory not found."
fi


# Check for yunbridge binary
if [ ! -f /usr/bin/yunbridge ]; then
  echo "ERROR: /usr/bin/yunbridge not found. Please copy YunBridge-v2/src/bridge_daemon.py to /usr/bin/yunbridge and make it executable."
fi


# Web UI integration
if [ -d ../YunWebUI-v2/www ]; then
  mkdir -p /www/arduino-webui-v2
  cp -r ../YunWebUI-v2/www/* /www/arduino-webui-v2/
else
  echo "WARNING: ../YunWebUI-v2/www not found. Skipping web UI copy."
fi


# Restart services (OpenWRT uses /etc/init.d/bridge-v2)

# Restart services (OpenWRT uses /etc/init.d/bridge-v2)
if [ -f /etc/init.d/bridge-v2 ]; then
	/etc/init.d/bridge-v2 enable
	/etc/init.d/bridge-v2 restart
else
	echo "ERROR: /etc/init.d/bridge-v2 not found. Service not started."
fi

# Web UI integration
cp -r ../YunWebUI-v2/www/* /www/arduino-webui-v2/


if [ -f /etc/init.d/uhttpd ]; then
	/etc/init.d/uhttpd restart
else
	echo "WARNING: /etc/init.d/uhttpd not found. Web server not restarted."
fi

echo "openwrt-yun-v2 integration complete."

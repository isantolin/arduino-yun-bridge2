#!/bin/bash
# openwrt-yun-v2 install script
# Aplica parches, instala scripts de integración y actualiza todos los paquetes

# Actualizar todos los paquetes del sistema
echo "[INFO] Actualizando lista de paquetes y actualizando todos los paquetes instalados..."
opkg update
opkg list-upgradable | cut -f 1 -d ' ' | xargs -r opkg upgrade


# Deshabilitar login shell en consola serie (eliminar ::askconsole:/usr/libexec/login.sh de /etc/inittab)
if grep -q '::askconsole:/usr/libexec/login.sh' /etc/inittab; then
	echo "[INFO] Eliminando login shell de consola serie en /etc/inittab..."
	sed -i '/::askconsole:\/usr\/libexec\/login.sh/d' /etc/inittab
fi

# Install CGI script for LED 13 control
if [ -f scripts/led13_rest_cgi.py ]; then
	mkdir -p /www/cgi-bin
	cp scripts/pin_rest_cgi.py /www/cgi-bin/pin
	chmod +x /www/cgi-bin/pin
	echo "Installing REST CGI script (generic pin, requires pin parameter)..."
else
	echo "WARNING: scripts/led13_rest_cgi.py not found. CGI script not installed."
	echo "WARNING: scripts/pin_rest_cgi.py not found. CGI script not installed."
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



# Stop any running yunbridge daemons before starting a new one
echo "Stopping YunBridge v2 daemon..."
PIDS=$(ps | grep '[y]unbridge' | awk '{print $1}')
if [ -n "$PIDS" ]; then
	echo "$PIDS" | xargs kill -9
	echo "All running yunbridge processes killed."
else
	echo "No yunbridge processes found."
fi


# Instalar daemon Python yunbridge
echo "Instalando YunBridge v2 daemon en /usr/bin/yunbridge..."
cp ../YunBridge-v2/src/bridge_daemon.py /usr/bin/yunbridge
chmod +x /usr/bin/yunbridge
echo "YunBridge daemon instalado y marcado como ejecutable."



# Restart services (OpenWRT uses /etc/init.d/bridge-v2)
if [ -f /etc/init.d/bridge-v2 ]; then
		/etc/init.d/bridge-v2 enable
		/etc/init.d/bridge-v2 restart
else
		echo "ERROR: /etc/init.d/bridge-v2 not found. Service not started."
fi


if [ -f /etc/init.d/uhttpd ]; then
	/etc/init.d/uhttpd restart
else
	echo "WARNING: /etc/init.d/uhttpd not found. Web server not restarted."
fi

echo "openwrt-yun-v2 integration complete."

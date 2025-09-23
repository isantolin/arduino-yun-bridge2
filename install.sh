# --- Opcional: Mover /tmp a la SD (bind mount) ---
echo "[CHECKPOINT] Configurando /tmp para usar la SD (bind mount)..."
SD_TMP="$SD_MOUNT/tmp"
if [ ! -d "$SD_TMP" ]; then
    mkdir -p "$SD_TMP"
    chmod 1777 "$SD_TMP"
    echo "[INFO] Directorio $SD_TMP creado."
fi
# Copiar archivos actuales de /tmp si existen (opcional, seguro)
if [ "$(ls -A /tmp 2>/dev/null | wc -l)" -gt 0 ]; then
    cp -a /tmp/* "$SD_TMP/" 2>/dev/null || true
fi
# Realizar bind mount
mount --bind "$SD_TMP" /tmp
echo "[INFO] /tmp ahora apunta a $SD_TMP en la SD."
# Hacer persistente en /etc/rc.local
if ! grep -q 'mount --bind.*/tmp' /etc/rc.local; then
    echo "[INFO] Agregando bind mount de /tmp a /etc/rc.local para persistencia tras reinicio."
    sed -i '/^exit 0/i \
mkdir -p '$SD_TMP'\nchmod 1777 '$SD_TMP'\nmount --bind '$SD_TMP' /tmp\n' /etc/rc.local
fi
df -h /tmp
#!/bin/bash
# Unified install script for Arduino Yun v2 ecosystem
# Installs all dependencies, daemon, scripts, configs, Arduino library, and Python client plugin system
#
# Para probar el rollback, puedes forzar un error agregando: false
# después de cualquier checkpoint.
# Ejemplo: después de 'echo "[CHECKPOINT] Copying config and package files..."', agrega una línea: false
# El script debe limpiar los archivos creados y mostrar el mensaje de rollback.


set -e


# 1. Update and upgrade system
echo "[CHECKPOINT] Updating package lists..."
opkg update
echo "[CHECKPOINT] Upgrading upgradable packages..."
opkg list-upgradable | cut -f 1 -d ' ' | xargs -r opkg upgrade

# Centralizar instalación de paquetes opkg requeridos
echo "[CHECKPOINT] Installing required opkg packages..."
opkg install python3-uci python3 python3-pyserial mosquitto python3-pip || true

# --- SD/Extroot Python venv setup ---
SD_MOUNT="/mnt/sda1"  # Cambia esto si tu SD está montada en otro lugar
VENV_PATH="$SD_MOUNT/pyenv"
PYTHON="$VENV_PATH/bin/python3"
PIP="$VENV_PATH/bin/pip"

echo "[CHECKPOINT] Verificando entorno virtual Python en la SD..."
if [ ! -d "$VENV_PATH" ]; then
    echo "[INFO] Creando entorno virtual Python en $VENV_PATH"
    python3 -m venv "$VENV_PATH" || { echo "ERROR: No se pudo crear el entorno virtual en la SD"; exit 1; }
fi

echo "[INFO] Activando entorno virtual y actualizando pip..."
source "$VENV_PATH/bin/activate"
$PIP install --upgrade pip

echo "[INFO] Instalando dependencias Python en la SD..."
$PIP install --upgrade paho-mqtt google-cloud-pubsub boto3

LOGFILE="/tmp/yunbridge_install.log"
exec > >(tee -a "$LOGFILE") 2>&1

function rollback {
    echo "[ROLLBACK] Rolling back partial installation..."
    # Remove files/directories that may have been created
    [ -d /etc/yunbridge ] && rm -rf /etc/yunbridge
    [ -f /etc/config/yunbridge-ttyath0 ] && rm -f /etc/config/yunbridge-ttyath0
    [ -f /etc/yunbridge/yunbridge.files ] && rm -f /etc/yunbridge/yunbridge.files
    [ -f /etc/init.d/yunbridge ] && rm -f /etc/init.d/yunbridge
    [ -d /www/cgi-bin ] && rm -rf /www/cgi-bin
    [ -d /www/yunbridge ] && rm -rf /www/yunbridge
    [ -d "$HOME/Arduino/libraries/openwrt-library-arduino" ] && rm -rf "$HOME/Arduino/libraries/openwrt-library-arduino"
    echo "[ROLLBACK] Done."
}

trap 'echo "[ERROR] Installation failed at line $LINENO. See $LOGFILE for details."; rollback; exit 1' ERR

# 1. Update and upgrade system
echo "[CHECKPOINT] Updating package lists..."
opkg update
echo "[CHECKPOINT] Upgrading upgradable packages..."
opkg list-upgradable | cut -f 1 -d ' ' | xargs -r opkg upgrade

opkg install python3-uci python3 python3-pyserial mosquitto python3-pip || true
echo "[INFO] Core installation complete."
echo "[INFO] To install the Web UI (LuCI), follow instructions in /luci-app-yunbridge/README.md."

# 3. Remove serial console login if present
if grep -q '::askconsole:/usr/libexec/login.sh' /etc/inittab; then
    echo "[INFO] Removing serial console login from /etc/inittab..."
    sed -i '/::askconsole:\/usr\/libexec\/login.sh/d' /etc/inittab
fi

# 4. Install CGI REST script
echo "[CHECKPOINT] Installing CGI REST script..."
if [ -f openwrt-yun-core/scripts/pin_rest_cgi.py ]; then
    mkdir -p /www/cgi-bin
    cp -f openwrt-yun-core/scripts/pin_rest_cgi.py /www/cgi-bin/pin
    chmod +x /www/cgi-bin/pin
    echo "Installing REST CGI script (generic pin, requires pin parameter)..."
else
    echo "WARNING: openwrt-yun-core/scripts/pin_rest_cgi.py not found. CGI script not installed."
fi

# 5. Ensure /etc/yunbridge exists
echo "[CHECKPOINT] Ensuring /etc/yunbridge exists..."
if [ ! -d /etc/yunbridge ]; then
    mkdir -p /etc/yunbridge || { echo "ERROR: Could not create /etc/yunbridge"; exit 1; }
fi

# 3. Install LuCI Web UI if present
echo "[CHECKPOINT] Installing LuCI Web UI if present..."
LUCI_IPK=$(ls luci-app-yunbridge/bin/packages/*/luci/luci-app-yunbridge_*.ipk 2>/dev/null | head -n1)
if [ -n "$LUCI_IPK" ]; then
    echo "[INFO] Installing Web UI (luci-app-yunbridge) from .ipk package..."
    opkg install "$LUCI_IPK"
    echo "[INFO] Web UI (LuCI) installed from .ipk. Access via LuCI > Services > YunBridge."
else
    if [ -d luci-app-yunbridge/luasrc ]; then
        echo "[INFO] Installing Web UI (luci-app-yunbridge) manually..."
        mkdir -p /usr/lib/lua/luci/controller
        mkdir -p /usr/lib/lua/luci/model/cbi
        mkdir -p /usr/lib/lua/luci/view
        cp -f luci-app-yunbridge/luasrc/controller/yunbridge.lua /usr/lib/lua/luci/controller/ 2>/dev/null || true
        cp -f luci-app-yunbridge/luasrc/model/cbi/yunbridge.lua /usr/lib/lua/luci/model/cbi/ 2>/dev/null || true
        cp -rf luci-app-yunbridge/luasrc/view/yunbridge /usr/lib/lua/luci/view/ 2>/dev/null || true
        # Config UCI
        if [ -f luci-app-yunbridge/root/etc/config/yunbridge ]; then
            cp -f luci-app-yunbridge/root/etc/config/yunbridge /etc/config/yunbridge
        fi
        # WebUI
        mkdir -p /www/yunbridge
        if [ -f luci-app-yunbridge/root/www/yunbridge/index.html ]; then
            cp -f luci-app-yunbridge/root/www/yunbridge/index.html /www/yunbridge/index.html
        fi
        # Restart LuCI services
        if [ -f /etc/init.d/uhttpd ]; then
            /etc/init.d/uhttpd restart
        fi
        if [ -f /etc/init.d/rpcd ]; then
            /etc/init.d/rpcd restart
        fi
        echo "[INFO] Web UI (LuCI) installed manually. Access via LuCI > Services > YunBridge."
    else
        echo "[INFO] Web UI (luci-app-yunbridge) not found, only core installed."
    fi
fi

# 6. Copy config and package files
echo "[CHECKPOINT] Copying config and package files..."
if [ -f openwrt-yun-core/package/99-yunbridge-ttyath0.conf ]; then
    cp -f openwrt-yun-core/package/99-yunbridge-ttyath0.conf /etc/config/yunbridge-ttyath0
else
    echo "WARNING: openwrt-yun-core/package/99-yunbridge-ttyath0.conf not found."
fi
if [ -f openwrt-yun-core/package/yunbridge.files ]; then
    cp -f openwrt-yun-core/package/yunbridge.files /etc/yunbridge/yunbridge.files
else
    echo "WARNING: openwrt-yun-core/package/yunbridge.files not found."
fi

# 7. Install init script
echo "[CHECKPOINT] Installing init script..."
if [ -f openwrt-yun-core/package/yunbridge.init ]; then
    cp -f openwrt-yun-core/package/yunbridge.init /etc/init.d/yunbridge
    chmod +x /etc/init.d/yunbridge
else
    echo "ERROR: openwrt-yun-core/package/yunbridge.init not found."
fi

# 8. Copy scripts to /usr/bin
echo "[CHECKPOINT] Copying scripts to /usr/bin..."
if [ -d openwrt-yun-core/scripts ]; then
    for f in openwrt-yun-core/scripts/*; do
        if [ -f "$f" ]; then
            cp -f "$f" /usr/bin/
        fi
    done
else
    echo "WARNING: openwrt-yun-core/scripts directory not found."
fi

# 9. Install YunBridge daemon (Python package)
echo "[CHECKPOINT] Installing YunBridge daemon..."
if [ -f openwrt-yun-bridge/setup.py ]; then
    echo "[INFO] Installing Python daemon openwrt-yun-bridge via setup.py en entorno virtual de la SD..."
    cd openwrt-yun-bridge
    $PIP install --force-reinstall --upgrade .
    cd ..
    echo "[INFO] Daemon yunbridge installed as Python package in SD venv. Run with: $PYTHON -m yunbridge or activate venv."
else
    echo "ERROR: openwrt-yun-bridge/setup.py not found."
fi

# 10. Stop any running yunbridge daemons before starting a new one
echo "[CHECKPOINT] Stopping any running yunbridge daemons..."
PIDS=$(ps | grep '[y]unbridge' | awk '{print $1}')
if [ -n "$PIDS" ]; then
    echo "Stopping YunBridge v2 daemon..."
    kill $PIDS
fi

# 11. Start YunBridge daemon
echo "[CHECKPOINT] Starting YunBridge daemon..."
if [ -x "$PYTHON" ]; then
    echo "[DEBUG] Launching YunBridge daemon desde entorno virtual en la SD y mostrando log en tiempo real..."
    $PYTHON /usr/bin/yunbridge > /tmp/yunbridge_debug.log 2>&1 &
    sleep 1
    tail -f /tmp/yunbridge_debug.log &
    echo "YunBridge daemon started from SD venv. Log is shown above. You can close the tail with Ctrl+C."
else
    echo "ERROR: $PYTHON not found. Daemon not started."
fi

# 12. Install Arduino library (openwrt-library-arduino)
echo "[CHECKPOINT] Installing Arduino library..."
if [ -d openwrt-library-arduino/src ]; then
    LIB_DST="$HOME/Arduino/libraries/openwrt-library-arduino"
    mkdir -p "$LIB_DST"
    cp -rf openwrt-library-arduino/src/* "$LIB_DST/"
    echo "openwrt-library-arduino installed to $LIB_DST."
else
    echo "WARNING: openwrt-library-arduino/src directory not found. Arduino library not installed."
fi

echo "\n[INFO] Arduino Yun v2 ecosystem installation complete."
echo "- Upload the example sketch from openwrt-library-arduino to your Yun using the Arduino IDE."
echo "- Reboot the Yun if needed."
echo "- Test MQTT, LuCI WebUI, and integration."
echo "- For Google Pub/Sub support, ensure you have a valid service account .json file and configure Pub/Sub options in LuCI."
echo "- For Amazon SNS support, ensure you have your AWS credentials and SNS topic ARN, and configure SNS options in LuCI."

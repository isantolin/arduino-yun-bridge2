
echo "[INFO] Instalando dependencias de compilación necesarias para OpenWRT SDK (solo en PC de desarrollo)"
if [ "$(uname -s)" = "Linux" ]; then
    if [ -f /etc/debian_version ]; then
        echo "[INFO] Instalando paquetes para Ubuntu/Debian..."
        sudo apt-get update
        sudo apt-get install -y build-essential python3 python3-pip python3-venv python3-setuptools python3-wheel python3-build git unzip tar gzip bzip2 xz-utils coreutils libncurses5-dev libncursesw5-dev zstd wget
    elif [ -f /etc/fedora-release ]; then
        echo "[INFO] Instalando paquetes para Fedora..."
        sudo dnf install -y @development-tools python3 python3-pip python3-virtualenv python3-setuptools python3-wheel python3-build git unzip tar gzip bzip2 xz coreutils ncurses-devel zstd wget
    else
        echo "[WARN] Distro Linux no reconocida. Instala manualmente: build-essential, ncurses-dev, zstd, wget, etc."
    fi
else
    echo "[WARN] Sistema operativo no soportado para instalación automática de dependencias."
fi

echo "\n[INFO] Arduino Yun v2 ecosystem installation complete."
echo "- Upload the example sketch from openwrt-library-arduino to your Yun using the Arduino IDE."
echo "- Reboot the Yun if needed."
echo "- Test MQTT, LuCI WebUI, and integration."
echo "- For Google Pub/Sub support, ensure you have a valid service account .json file and configure Pub/Sub options in LuCI."
echo "- For Amazon SNS support, ensure you have your AWS credentials and SNS topic ARN, and configure SNS options in LuCI."


#!/bin/bash
# Simplified install script for Arduino Yun v2 ecosystem
# Installs only pre-built packages (.ipk, .whl) and performs minimal configuration

set -e

echo "[CHECKPOINT] Updating package lists..."
opkg update
echo "[CHECKPOINT] Upgrading upgradable packages..."
opkg list-upgradable | cut -f 1 -d ' ' | xargs -r opkg upgrade
echo "[CHECKPOINT] Installing required opkg packages..."
# Solo dependencias mínimas de sistema, el resto se declara en los Makefile/setup.py de cada paquete
opkg install python3 python3-pip || true
echo "[INFO] Activando entorno virtual y actualizando pip..."

# --- Instalar paquetes precompilados ---
echo "[CHECKPOINT] Instalando paquetes .ipk precompilados..."
for ipk in bin/*.ipk; do
    if [ -f "$ipk" ]; then
        echo "[INFO] Instalando $ipk ..."
        opkg install --force-reinstall "$ipk"
    fi
done

echo "[CHECKPOINT] Instalando paquetes Python .whl precompilados en venv de la SD..."
for whl in bin/*.whl; do
    if [ -f "$whl" ]; then
        echo "[INFO] Instalando $whl ..."
        $PIP install --force-reinstall "$whl"
    fi
done

echo "[INFO] Instalación de paquetes precompilados completa."
echo "- Reboot the Yun if needed."
echo "- Test MQTT, LuCI WebUI, and integration."
echo "- For Google Pub/Sub support, ensure you have a valid service account .json file and configure Pub/Sub options in LuCI."
echo "- For Amazon SNS support, ensure you have your AWS credentials and SNS topic ARN, and configure SNS options in LuCI."

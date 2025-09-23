#!/bin/bash
# Simplified install script for Arduino Yun v2 ecosystem
# Installs only pre-built packages (.ipk, .whl) and performs minimal configuration

set -e

echo "[CHECKPOINT] Updating package lists..."
opkg update
echo "[CHECKPOINT] Upgrading upgradable packages..."
opkg list-upgradable | cut -f 1 -d ' ' | xargs -r opkg upgrade
echo "[CHECKPOINT] Installing required opkg packages..."
# Only minimal system dependencies; all others are declared in each package's Makefile/setup.py
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

#!/bin/sh
# Script local para instalar luci-app-yunbridge en OpenWrt/Yun
# Uso: ejecuta este script directamente en el router

# Instalar dependencias necesarias
opkg update
opkg install luci luci-base lua luci-mod-admin-full luci-lib-nixio luci-lib-ipkg luci-compat python3-uci

set -e


# Crear directorios destino si no existen
mkdir -p /usr/lib/lua/luci/controller
mkdir -p /usr/lib/lua/luci/model/cbi
mkdir -p /usr/lib/lua/luci/view

# Copiar archivos LuCI
cp -f luasrc/controller/yunbridge.lua /usr/lib/lua/luci/controller/
cp -f luasrc/model/cbi/yunbridge.lua /usr/lib/lua/luci/model/cbi/
cp -rf luasrc/view/yunbridge /usr/lib/lua/luci/view/

# Copiar config UCI
cp -f root/etc/config/yunbridge /etc/config/yunbridge

# Copiar WebUI
mkdir -p /www/yunbridge
cp -f root/www/yunbridge/index.html /www/yunbridge/index.html

# Reiniciar servicios LuCI
/etc/init.d/uhttpd restart
/etc/init.d/rpcd restart

echo "\n¡Listo! Accede a LuCI > Servicios > YunBridge en tu router."
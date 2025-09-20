# Dependencias necesarias

Antes de instalar el panel, asegúrate de tener instalados los siguientes paquetes en OpenWrt:


```sh
opkg update
opkg install luci luci-base lua luci-mod-admin-full luci-lib-nixio luci-lib-ipkg luci-compat python3-uci
```

# luci-app-yunbridge

LuCI Web panel for configuring and monitoring YunBridge on OpenWrt.

## Features
- Edit YunBridge configuration (MQTT, serial, debug, etc.)
- Integrated Web UI for real-time control/monitoring (from YunWebUI)
- UCI config: `/etc/config/yunbridge`



## Notas sobre configuración
El daemon YunBridge lee la configuración desde UCI (`/etc/config/yunbridge`) usando `python3-uci`. Si alguna opción no existe, se usará el valor estándar.

## Instalación manual (sin buildroot)

Puedes instalar el panel directamente copiando los archivos al router vía SSH/SCP:

1. Ejecuta el script de instalación automática:
	```sh
	./install.sh <IP_ROUTER> [usuario]
	# Ejemplo:
	./install.sh 192.168.1.1 root
	```
	Esto copiará todos los archivos necesarios y reiniciará LuCI.

2. Alternativamente, copia manualmente:
	- `luasrc/controller/yunbridge.lua` a `/usr/lib/lua/luci/controller/`
	- `luasrc/model/cbi/yunbridge.lua` a `/usr/lib/lua/luci/model/cbi/`
	- `luasrc/view/yunbridge/` a `/usr/lib/lua/luci/view/`
	- `root/etc/config/yunbridge` a `/etc/config/yunbridge`
	- `root/www/yunbridge/index.html` a `/www/yunbridge/index.html`
	- Reinicia LuCI: `/etc/init.d/uhttpd restart; /etc/init.d/rpcd restart`

Luego accede a LuCI: Servicios > YunBridge

## Usage
- Instala el paquete o copia los archivos manualmente
- Accede vía LuCI: Services > YunBridge
- Configura parámetros y usa la pestaña Web UI

## Files
- `luasrc/controller/yunbridge.lua`: LuCI controller
- `luasrc/model/cbi/yunbridge.lua`: Config form
- `luasrc/view/yunbridge/webui.htm`: Embedded Web UI
- `root/etc/config/yunbridge`: UCI config defaults
- `root/www/yunbridge/index.html`: YunWebUI frontend (copy from YunWebUI-v2)

## MQTT Broker IP Automation
Para que el WebUI use automáticamente la IP del router como broker MQTT, reemplaza la línea:
```js
const brokerUrl = 'ws://192.168.15.17:9001';
```
por:
```js
const brokerUrl = 'ws://' + window.location.hostname + ':9001';
```
Así el WebUI siempre conecta al broker MQTT del propio dispositivo.

## Roadmap
- [ ] Mejorar validación de parámetros
- [ ] Soporte para más opciones avanzadas
- [ ] Integración con logs y estado del daemon

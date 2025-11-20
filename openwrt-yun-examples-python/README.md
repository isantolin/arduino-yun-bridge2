# Arquitectura del Cliente del Puente de Yun v2

Este componente (`openwrt-yun-client-python`) proporciona las herramientas para que las aplicaciones que se ejecutan en el lado Linux del Arduino Yun interactúen con el microcontrolador a través del `bridge_daemon.py`. Las utilidades de este paquete se apoyan ahora en **aiomqtt 2.4** (que incluye `paho-mqtt` 2.1) y hablan MQTT v5 de forma predeterminada, reutilizando el mismo shim asíncrono que usa el daemon para conservar compatibilidad con la antigua API de `asyncio-mqtt`.

## API de Comunicación: MQTT

El ecosistema utiliza MQTT como el mecanismo principal de comunicación para interactuar con el `bridge_daemon.py`.

-   **Propósito:** Para scripts y aplicaciones que se ejecutan **tanto en el procesador Linux del Yun como externamente**.
-   **Mecanismo:** El `bridge_daemon.py` expone la funcionalidad del microcontrolador a través de un broker MQTT. Los clientes (como los ejemplos en este directorio) se conectan a este broker para enviar comandos y recibir datos.
-   **Caso de uso:** Un script de Python en el Yun que monitoriza el uso de CPU y quiere mostrar el resultado en una pantalla LCD conectada al microcontrolador, o un panel de control web (Dashboard) que se ejecuta en un servidor en la nube y muestra la temperatura leída por un sensor en el Arduino, y permite encender un LED desde el navegador.

## El Sistema de Plugins (Nota Histórica)

Versiones anteriores del diseño consideraban un sistema de plugins para extender la funcionalidad del cliente. Sin embargo, la arquitectura actual centraliza la lógica de puente en `bridge_daemon.py` y utiliza MQTT como la interfaz unificada. Esto simplifica el diseño y mejora la interoperabilidad.

En resumen, la comunicación se realiza exclusivamente a través de MQTT, lo que proporciona una solución robusta, modular y adaptable a casi cualquier caso de uso de IoT.

## Dependencias empaquetadas

Los scripts reutilizan las mismas dependencias instaladas en la Yún vía `opkg`. `python3-aiomqtt` (que ya incluye `paho-mqtt` ≥ 2.1) y `python3-pyserial` provienen de los feeds oficiales, mientras que `python3-pyserial-asyncio` y `python3-cobs` se construyen desde PyPI y se distribuyen como `.ipk` junto con el resto de artefactos. No necesitas instalar `asyncio-mqtt`: el módulo `yunbridge_client._mqtt_asyncio` implementa la API equivalente sobre aiomqtt, habilitando propiedades MQTT v5 como `session_expiry_interval` y códigos de motivo enriquecidos sin cambiar el código de los ejemplos.

Si ejecutas los ejemplos directamente desde el repositorio (sin instalar los paquetes IPK), instala las dependencias mínimas en tu entorno de desarrollo:

```sh
pip install "aiomqtt>=2.4,<3" "paho-mqtt>=2.1,<3"
```

Antes de modificar los ejemplos, ejecuta `pyright` en la raíz del proyecto para asegurarte de que el tipado estático siga consistente con el daemon.

> **Nota:** Si trabajas en un entorno virtual fuera de OpenWrt, instala también `python3-tenacity` o añade `tenacity>=9.1` a tu entorno para que los ejemplos puedan aprovechar los mismos helpers de reconexión que usa el daemon.

### Puesta en marcha del broker MQTT

Los ejemplos asumen que existe un broker accesible en la IP y puerto configurados (por defecto `127.0.0.1:1883`). En una Yún real, ese broker lo expone el `bridge_daemon.py` cuando está en ejecución. Las conexiones se negocian con MQTT v5 (`clean_start=FIRST_ONLY`, `session_expiry_interval=0`) y publican los motivos de desconexión, por lo que conviene revisar el log del daemon si observas `ConnectionCloseForcedError` o códigos de error adicionales.

```sh
# En el dispositivo o en tu máquina de desarrollo
python3 openwrt-yun-bridge/bridge_daemon.py
```

Si prefieres realizar pruebas aisladas sin el daemon, puedes lanzar un mosquitto local:

```sh
mosquitto -v -p 1883
```

Cuando no hay ningún broker escuchando, los ejemplos fallarán con un timeout al conectarse.

### Variables de entorno útiles

Exporta estas variables antes de ejecutar los scripts o instaladores para ajustar el comportamiento sin editar los archivos UCI:

```sh
# Activa el modo de depuración en el daemon y los ejemplos
export YUNBRIDGE_DEBUG=1

# Fuerza la instalación sin confirmaciones interactivas en 3_install.sh
export YUNBRIDGE_AUTO_UPGRADE=1

# Elimina automáticamente paquetes PPP/odhcp que bloquean ttyATH0 durante 3_install.sh
export YUNBRIDGE_REMOVE_PPP=1

# Omite el prompt de confirmación al preparar el extroot en 2_expand.sh
export EXTROOT_FORCE=1

# Ajusta la cola MQTT (vía UCI) antes de reiniciar el daemon
uci set yunbridge.general.mqtt_queue_limit=256
uci set yunbridge.general.serial_retry_timeout='0.75'
uci set yunbridge.general.serial_retry_attempts='3'
uci commit yunbridge
/etc/init.d/yunbridge restart
```

### Variables de entorno del cliente

Los módulos de `yunbridge_client` detectan automáticamente estas variables al conectarse al broker MQTT:

```sh
export YUN_BROKER_IP='192.168.1.50'
export YUN_BROKER_PORT='1883'
export YUN_BROKER_USER='mi_usuario'
export YUN_BROKER_PASS='mi_password'
# Ajusta la resiliencia de los clientes REST/LuCI frente a brokers lentos
export YUNBRIDGE_MQTT_RETRIES='5'
export YUNBRIDGE_MQTT_TIMEOUT='6.0'
export YUNBRIDGE_MQTT_BACKOFF='0.75'
```

Puedes definirlas en tu shell de desarrollo o añadirlas a `/etc/profile.d/` si quieres que queden persistentes en la Yún. El prefijo MQTT (`br` por defecto) puede modificarse al instanciar `Bridge(topic_prefix="otro_prefijo")`.

### Nuevos ejemplos y flujos

- `process_test.py` ahora ilustra cómo consumir el `stdout`/`stderr` de procesos largos mediante polls consecutivos, verificando los flags de truncamiento publicados en MQTT.
- Los scripts de datastore y mailbox reflejan los prefijos de longitud y códigos de estado actualizados expuestos por la librería del MCU.
- Recuerda subscribirte a `br/system/status` para recibir estados de error del MCU, por ejemplo cuando la cola de `CMD_PROCESS_POLL` alcanza el máximo.
- Las peticiones de lectura al datastore se envían a `br/datastore/get/<clave>/request`; las respuestas continúan publicándose en `br/datastore/get/<clave>`, evitando así consumir nuestro propio mensaje de petición.

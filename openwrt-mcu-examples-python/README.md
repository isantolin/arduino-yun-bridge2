# Arquitectura del Cliente del Puente de MCU v2

Este componente (`openwrt-mcu-client-python`) proporciona las herramientas para que las aplicaciones que se ejecutan en el lado Linux del Arduino MCU interactúen con el microcontrolador a través de `mcubridge/daemon.py`. Las utilidades de este paquete se apoyan ahora en **aiomqtt 2.5** (que incluye `paho-mqtt` 2.1) y hablan MQTT v5 de forma predeterminada, consumiendo directamente `aiomqtt.client.Message` junto con los DTOs serializables (`QueuedPublish`) y los helpers (`topic_name`, `correlation_data`) que expone el daemon sin recurrir a ningún shim intermedio.

## API de Comunicación: MQTT

El ecosistema utiliza MQTT como el mecanismo principal de comunicación para interactuar con `mcubridge/daemon.py`.

-   **Propósito:** Para scripts y aplicaciones que se ejecutan **tanto en el procesador Linux del MCU como externamente**.
-   **Mecanismo:** `mcubridge/daemon.py` expone la funcionalidad del microcontrolador a través de un broker MQTT. Los clientes (como los ejemplos en este directorio) se conectan a este broker para enviar comandos y recibir datos.
-   **Caso de uso:** Un script de Python en el MCU que monitoriza el uso de CPU y quiere mostrar el resultado en una pantalla LCD conectada al microcontrolador, o un panel de control web (Dashboard) que se ejecuta en un servidor en la nube y muestra la temperatura leída por un sensor en el Arduino, y permite encender un LED desde el navegador.

### Flujo request/response con MQTT v5

Las estructuras compartidas de `mcubridge.mqtt` (ahora limitadas a DTOs serializables) aprovechan las capacidades de **MQTT v5** para correlacionar peticiones y respuestas sin depender de nombres de tópicos rígidos:

- Al invocar métodos como `Bridge._publish_and_wait(...)`, el cliente genera un `correlation_data` aleatorio y fija su propio `response_topic` privado (`br/client/<uuid>/reply`). El daemon reutiliza ambos campos en cada respuesta para que la correlación sea inequívoca incluso frente a múltiples consumidores.
- Cuando una petición incluye un tópico de respuesta explícito (por compatibilidad hacia atrás), el daemon se suscribe temporalmente y publica allí la respuesta; si no, envía el payload al `response_topic` privado.
- Cada servicio añade metadatos en `user_properties` para que puedas inspeccionar rápidamente el contexto original sin parsear el payload:

	| Propiedad                    | Servicio(s)                                      | Descripción                                                          |
	| --------------------------- | ------------------------------------------------ | -------------------------------------------------------------------- |
	| `bridge-request-topic`      | Todos                                            | Tópico original que originó la petición MQTT.                        |
	| `bridge-pin`                | GPIO digital/analógico                           | Identificador del pin asociado a la lectura/respuesta.               |
	| `bridge-datastore-key`      | Datastore                                        | Clave afectada por la operación `put/get`.                           |
	| `bridge-file-path`          | Sistema de archivos                              | Ruta absoluta (normalizada) del fichero leído desde Linux.           |
	| `bridge-process-pid`        | Procesos (poll/pipeline)                         | PID interno utilizado por el daemon para rastrear la ejecución.      |
	| `bridge-status` (+ mensaje) | `system/status`                                  | Código de estado publicado junto con la descripción humana legible.  |

- Los servicios asignan `message_expiry_interval` acordes a la semántica (p. ej. lecturas de pines = 5 s, memoria libre = 10 s, versión MCU = 60 s) para evitar respuestas obsoletas en brokers congestivos.
- El cliente expone directamente las propiedades de usuario y la correlación en los `DeliveredMessage`, por lo que puedes construir flujos “fire & forget” o pipelines reactivos en tiempo real sin desincronizarte.

> **Consejo:** Si tu aplicación necesita remontar la respuesta a la petición originaria (por ejemplo, en un dashboard web multiusuario), guarda el `correlation_data` que devuelve `Bridge._publish_and_wait` y compara el binario devuelto por la respuesta antes de aplicar cambios en UI.

## El Sistema de Plugins (Nota Histórica)

Versiones anteriores del diseño consideraban un sistema de plugins para extender la funcionalidad del cliente. Sin embargo, la arquitectura actual centraliza la lógica de puente en `mcubridge/daemon.py` y utiliza MQTT como la interfaz unificada. Esto simplifica el diseño y mejora la interoperabilidad.

En resumen, la comunicación se realiza exclusivamente a través de MQTT, lo que proporciona una solución robusta, modular y adaptable a casi cualquier caso de uso de IoT.

## Dependencias empaquetadas

Los scripts reutilizan las mismas dependencias instaladas en la MCU por `3_install.sh`. `aiomqtt`, `paho-mqtt`, `cobs`, `prometheus-client` y `psutil` se compilan localmente como APKs en `feeds/` y se instalan desde `bin/`. La comunicación serial del daemon usa una implementación pura basada en `termios` (módulo built-in de Python), eliminando la dependencia de `pyserial`. No necesitas instalar `asyncio-mqtt`: trabajamos directamente contra `aiomqtt.Client`, reutilizamos `QueuedPublish` para los spoolers y apoyamos la observabilidad en los mismos helpers que usa el daemon para exponer `session_expiry_interval` y códigos de motivo enriquecidos sin cambiar el código de los ejemplos.

Si ejecutas los ejemplos directamente desde el repositorio (sin instalar los paquetes APK), instala las dependencias mínimas en tu entorno de desarrollo:

```sh
pip install \
	"aiomqtt>=2.5,<3" \
	"paho-mqtt>=2.1,<3" \
	"prometheus-client>=0.20,<1" \
	"tenacity>=9.0,<10" \
	"cobs>=1.2,<2"
```

Antes de modificar los ejemplos, ejecuta `pyright` en la raíz del proyecto para asegurarte de que el tipado estático siga consistente con el daemon.

### Puesta en marcha del broker MQTT

Los ejemplos asumen que existe un broker accesible en la IP y puerto configurados (por defecto `127.0.0.1:8883` con TLS habilitado). En una MCU real, ese broker lo expone `mcubridge/daemon.py` cuando está en ejecución. Las conexiones se negocian con MQTT v5 (`clean_start=FIRST_ONLY`, `session_expiry_interval=0`) y publican los motivos de desconexión, por lo que conviene revisar el log del daemon si observas `ConnectionCloseForcedError` o códigos de error adicionales.

```sh
# En el dispositivo o en tu máquina de desarrollo
python3 openwrt-mcu-bridge/mcubridge/daemon.py
```

Si prefieres realizar pruebas aisladas sin el daemon, puedes lanzar un mosquitto local:

```sh
mosquitto -v -p 8883 \
	--cafile /path/to/ca.crt \
	--cert /path/to/server.crt \
	--key /path/to/server.key

> Ajusta las rutas a tus propios certificados TLS emitidos por la CA que configuraste en el daemon.
```

Cuando no hay ningún broker escuchando, los ejemplos fallarán con un timeout al conectarse.

### Configuración del daemon (solo UCI)

El daemon **no** consume variables de entorno para su configuración: todo se gestiona vía UCI/LuCI.

```sh
# Activa depuración en el daemon
uci set mcubridge.general.debug='1'
uci commit mcubridge
/etc/init.d/mcubridge restart

# Ejemplos de tuning (vía UCI)
uci set mcubridge.general.mqtt_queue_limit='256'
uci set mcubridge.general.serial_retry_timeout='0.75'
uci set mcubridge.general.serial_retry_attempts='3'
uci commit mcubridge
/etc/init.d/mcubridge restart
```

### Configuración (solo UCI)

Este repo evita knobs por variables de entorno: usa UCI para el daemon y también para los valores del broker que consumen estos ejemplos.

### Configuración del broker para los ejemplos

Los módulos de `mcubridge_client` leen la configuración desde `mcubridge.general.*`:

```sh
uci set mcubridge.general.mqtt_host='192.168.1.50'
uci set mcubridge.general.mqtt_port='8883'
uci set mcubridge.general.mqtt_user='mi_usuario'
uci set mcubridge.general.mqtt_pass='mi_password'
uci set mcubridge.general.mqtt_tls='1'
uci commit mcubridge
/etc/init.d/mcubridge restart
```

El prefijo MQTT (`br` por defecto) se controla con `mcubridge.general.mqtt_topic` o al instanciar `Bridge(topic_prefix="otro_prefijo")`.

### Nuevos ejemplos y flujos

- `process_test.py` ahora ilustra cómo consumir el `stdout`/`stderr` de procesos largos mediante polls consecutivos, verificando los flags de truncamiento publicados en MQTT.
- Los scripts de datastore y mailbox reflejan los prefijos de longitud y códigos de estado actualizados expuestos por la librería del MCU.
- Recuerda subscribirte a `br/system/status` para recibir estados de error del MCU, por ejemplo cuando la cola de `CMD_PROCESS_POLL` alcanza el máximo.
- Las peticiones de lectura al datastore se envían a `br/datastore/get/<clave>/request`; las respuestas continúan publicándose en `br/datastore/get/<clave>`, evitando así consumir nuestro propio mensaje de petición.

# Arquitectura del Cliente del Puente de MCU v2

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MQTT](https://img.shields.io/badge/MQTT-v5-660066?logo=mqtt)](https://mqtt.org/)
[![aiomqtt](https://img.shields.io/badge/aiomqtt-2.5+-blue)](https://sbtinstruments.github.io/aiomqtt/)
[![OpenWrt](https://img.shields.io/badge/OpenWrt-25.12.0-00B5E2?logo=openwrt)](https://openwrt.org/)

Este componente (`openwrt-mcu-client-python`) proporciona las herramientas para que las aplicaciones que se ejecutan en el lado Linux del Arduino MCU interactúen con el microcontrolador a través de `mcubridge/daemon.py`. Las utilidades de este paquete se apoyan en **aiomqtt 2.5** y hablan MQTT v5 de forma predeterminada, consumiendo directamente `aiomqtt.client.Message` junto con los DTOs serializables (`QueuedPublish`) que expone el daemon.

## API de Comunicación: MQTT

El ecosistema utiliza MQTT como el mecanismo principal de comunicación.

-   **Propósito:** Para scripts y aplicaciones que se ejecutan **tanto en el procesador Linux del MCU como externamente**.
-   **Mecanismo:** `mcubridge/daemon.py` expone la funcionalidad del microcontrolador a través de un broker MQTT.
-   **Caso de uso:** Un script de Python en el MCU que monitoriza el uso de CPU y quiere mostrar el resultado en una pantalla LCD, o un panel de control web que muestra la temperatura leída por un sensor.

### Flujo request/response con MQTT v5

Aprovecha las capacidades de **MQTT v5** para correlacionar peticiones y respuestas:

- Al invocar métodos como `Bridge._publish_and_wait(...)`, el cliente genera un `correlation_data` aleatorio y fija su propio `response_topic` privado (`br/client/<uuid>/reply`).
- Cada servicio añade metadatos en `user_properties` para inspeccionar el contexto original sin parsear el payload:

	| Propiedad                    | Servicio(s)                                      | Descripción                                                          |
	| --------------------------- | ------------------------------------------------ | -------------------------------------------------------------------- |
	| `bridge-request-topic`      | Todos                                            | Tópico original que originó la petición MQTT.                        |
	| `bridge-pin`                | GPIO digital/analógico                           | Identificador del pin asociado a la lectura/respuesta.               |
	| `bridge-datastore-key`      | Datastore                                        | Clave afectada por la operación `put/get`.                           |
	| `bridge-file-path`          | Sistema de archivos                              | Ruta absoluta (normalizada) del fichero leído.                       |
	| `bridge-process-pid`        | Procesos (poll/pipeline)                         | PID interno rastreado por el daemon.                                 |
	| `bridge-status`             | `system/status`                                  | Código de estado publicado.                                          |

- Los servicios asignan `message_expiry_interval` acordes a la semántica (p. ej. pines = 5 s).

## Dependencias empaquetadas

Los scripts reutilizan las mismas dependencias instaladas en la MCU por `3_install.sh`: `aiomqtt`, `paho-mqtt`, `cobs`, `prometheus-client` y `psutil`.

Si ejecutas los ejemplos directamente desde el repositorio, instala las dependencias:

```sh
pip install \
	"aiomqtt>=2.5,<3" \
	"paho-mqtt>=2.1,<3" \
	"prometheus-client>=0.20,<1" \
	"tenacity>=9.0,<10" \
	"cobs>=1.2,<2"
```

### Puesta en marcha del broker MQTT

Los ejemplos asumen que existe un broker accesible (por defecto `127.0.0.1:1883` para emulación).

```sh
# En el dispositivo o en tu máquina de desarrollo
python3 -m mcubridge.daemon --debug
```

### Configuración (solo UCI)

El daemon y la librería cliente leen la configuración desde OpenWrt UCI (`mcubridge.general.*`).

```sh
# Activa depuración
uci set mcubridge.general.debug='1'
uci commit mcubridge && /etc/init.d/mcubridge restart
```

### Nuevos ejemplos y flujos

- `process_test.py`: ilustra cómo consumir el `stdout`/`stderr` de procesos largos mediante polls consecutivos.
- `br/system/status`: subscríbete para recibir estados de error globales.
- `br/datastore/get/<clave>/request`: peticiones de lectura al datastore (las respuestas se publican en `br/datastore/get/<clave>`).

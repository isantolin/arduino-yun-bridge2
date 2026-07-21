# Arquitectura del Cliente del Puente de MCU v2

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Socket](https://img.shields.io/badge/IPC-UNIX_Socket-blue?logo=linux)](https://en.wikipedia.org/wiki/Unix_domain_socket)
[![Protobuf](https://img.shields.io/badge/Serialization-Protobuf-green?logo=protobuf)](https://protobuf.dev/)
[![OpenWrt](https://img.shields.io/badge/OpenWrt-25.12.5-00B5E2?logo=openwrt)](https://openwrt.org/)

Este componente (`openwrt-mcu-client-python`) proporciona las herramientas para que las aplicaciones que se ejecutan en el lado Linux del Arduino MCU interactúen con el microcontrolador a través del socket UNIX expuesto por el daemon. El cliente de Python utiliza conexiones asíncronas persistentes sobre `/var/run/mcubridge.sock` y serialización mediante tramas Protobuf (`CloudQueuedPublish`).

## API de Comunicación: Socket UNIX local

El ecosistema utiliza Sockets UNIX como el mecanismo principal de IPC local.

- **Propósito:** Para scripts y aplicaciones que se ejecutan en el procesador Linux del MPU (como CGI scripts y CLI de administración).
- **Mecanismo:** El daemon expone un socket UNIX en `/var/run/mcubridge.sock` (configurable mediante la variable de entorno `MCUBRIDGE_SOCKET_PATH`).
- **Caso de uso:** Un script de Python local que lee pines analógicos/digitales, manipula el sistema de archivos del microcontrolador, o ejecuta subprocesos asíncronos y monitorea su progreso.

### Formato de tramas y serialización

Toda la comunicación en el socket UNIX se realiza mediante tramas binarias prefijadas con su longitud:

- **Estructura de la Trama:** `[Longitud (4 bytes big-endian)] [Payload Protobuf (CloudQueuedPublish)]`
- **Mensaje de Consola MCU:** El daemon transmite automáticamente las salidas de la consola del MCU (`/console/out`) a todos los clientes conectados al socket.

## Dependencias

Los scripts y herramientas CLI utilizan únicamente `protobuf`, `cobs`, y `prometheus-client`. Ya no se requiere configurar ni instalar brokers locales de CLOUD ni TLS en las dependencias del cliente.

Si ejecutas los ejemplos directamente desde el repositorio, instala las dependencias:

```sh
pip install \
	"protobuf==7.36.0rc1" \
	"prometheus-client>=0.20,<1" \
	"tenacity>=9.0,<10" \
	"cobs>=1.2,<2"
```

### Puesta en marcha del Daemon

Los ejemplos asumen que existe una instancia del daemon corriendo y escuchando en el socket UNIX.

```sh
# Arrancar el daemon en modo depuración (creará el socket UNIX)
python3 -m mcubridge.daemon --debug
```

### Configuración (solo UCI)

El daemon lee la configuración general desde OpenWrt UCI (`mcubridge.general.*`).

```sh
# Activa depuración
uci set mcubridge.general.debug='1'
uci commit mcubridge && /etc/init.d/mcubridge restart
```

### Ejemplos incluidos

- `process_test.py`: ilustra cómo lanzar y monitorizar subprocesos asíncronos en el MPU a través del socket.
- `mailbox_read_test.py`: demuestra la lectura y escritura sobre el mailbox del microcontrolador.
- `sensor_reader_test.py`: lectura periódica del pin digital `d13` o analógicos de la placa.
- `led13_test.py`: control de encendido y apagado del LED integrado en la placa.
- `spi_test.py`: lectura y escritura a través de buses periféricos SPI.


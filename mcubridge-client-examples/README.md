# Arquitectura de Clientes MCU Bridge v2 (Pigweed Cloud Gateway)

[![Python](https://img.shields.io/badge/Python-3.13+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Pigweed](https://img.shields.io/badge/Pigweed-RPC-orange)](https://pigweed.dev/)
[![Protobuf](https://img.shields.io/badge/Serialization-Protobuf_v3-green?logo=protobuf)](https://protobuf.dev/)
[![OpenWrt](https://img.shields.io/badge/OpenWrt-25.12.6-00B5E2?logo=openwrt)](https://openwrt.org/)

Este componente proporciona el SDK y la suite de herramientas CLI para que aplicaciones, dashboards y servicios de automatización interactúen con microcontroladores Arduino a través del **Pigweed Cloud Gateway** C++ (`mcubridge-gateway-cpp`).

## Arquitectura de Comunicación Centralizada

Todos los clientes se comunican **exclusivamente con el Cloud Gateway** mediante llamadas RPC fuertemente tipadas sobre Pigweed RPC (`GatewayClient`). Se ha erradicado por completo el uso de sockets UNIX locales en los clientes, consolidando un plano de control distribuido y homogéneo:

```
┌──────────────────────────────────────┐
│  Client Applications / Scripts / CLI │
│    (mcubridge-client-examples)       │
└──────────────────┬───────────────────┘
                   │ TLS 1.3 mTLS (SPIFFE ID, HDLC, pw_rpc)
                   ▼
┌─────────────────────────────────────────────────────────────┐
│       Pigweed Cloud Gateway (mcubridge-gateway-cpp)         │
│  - GatewayControl (Health, Dispatch con target_device_id)   │
│  - pw_transfer (Streaming de archivos y EEPROM acotado)     │
│  - Identidad SPIFFE (spiffe://mcubridge/{client,device}/...) │
│  - Multiplexación N:1 de dispositivos de borde              │
└──────────────────┬──────────────────────────────────────────┘
                   │ TLS 1.3 mTLS (EdgeControl, DeviceTelemetry)
                   │ rpc.pb.<Service>/<Method>
                   ▼
┌─────────────────────────────────────────────────────────────┐
│             McuBridge Daemon (Edge Linux / MPU)             │
│  - Conexión saliente autenticada (Pure Push)                │
│  - Despacho delegado RPC (EdgeControl.Dispatch)             │
│  - IPC local vía OpenWrt UBUS (ubus call mcubridge)         │
└──────────────────┬──────────────────────────────────────────┘
                   │ Serial HDLC + pw_rpc (pw_hdlc + CRC32 FCS)
                   ▼
┌─────────────────────────────────────────────────────────────┐
│                 MCU Firmware (Arduino AVR)                  │
└─────────────────────────────────────────────────────────────┘
```

---

## Resolución Explícita de Dispositivos (`target_device_id`)

El Cloud Gateway Pigweed opera en una topología **Hub-and-Spoke (N:1)** donde múltiples placas Arduino se conectan simultáneamente. Para garantizar determinismo e integridad SIL-2, **la resolución del dispositivo de destino es siempre explícita**:

1. **Campo Obligatorio:** Toda invocación a `GatewayControl.Dispatch` incluye el campo `target_device_id` con el identificador exacto del dispositivo de destino (p.ej. `yun-mcu-01`).
2. **Rechazo Inmediato sin Adopción Implícita:**
   - Si un cliente omite `target_device_id`, el Gateway devuelve `INVALID_ARGUMENT`.
   - Si el dispositivo solicitado no mantiene una sesión Pigweed activa con el Gateway, se devuelve inmediatamente `NOT_FOUND` / `UNAVAILABLE`.
3. **Cero Tolerancia a Fallbacks Legacy:** No existe selección por omisión, auto-descubrimiento heurístico ni compatibilidad hacia sockets locales en los clientes.

---

## Topología de Tópicos Canónicos Cloud

Para clientes que utilizan el método genérico `Publish` o procesan flujos de telemetría y eventos en tiempo real, el direccionamiento se rige estrictamente por la **regla de cero backward**:

- **Tópico de Flota (enrutamiento en Gateway):** `devices/{device_id}/{direction}/{method}`
- **Tópico de Borde (enrutamiento en Daemon):** `{prefix}/{direction}/{method}`

Donde:
- `direction`: Obligatoriamente uno de tres tokens en minúscula:
  - `req`: Comandos / solicitudes dirigidas al microcontrolador o daemon.
  - `res`: Respuestas directas a solicitudes previas.
  - `evt`: Eventos asíncronos y telemetría espontánea (`status_online`, `pin_event`, etc.).
- `method`: Nombre canónico del servicio/método u operación (`rpc.pb.GpioService/DigitalWrite`, `rpc.pb.DataStoreService/Put`, etc.).

Todo tópico que no respete esta estructura exacta de 3 segmentos es rechazado de inmediato.

---

## Requisitos e Instalación

El cliente requiere **Python 3.13+** y dependencias basadas en stubs oficiales de Pigweed y Protobuf:

```bash
pip install \
    "protobuf==5.28.3" \
    "pigweed==0.0.24" \
    "tenacity>=9.0,<10" \
    "structlog>=24.4.0" \
    "typer>=0.12.5"
```

> **Nota:** No se requieren librerías de enlace serie (`pyserial`) ni utilidades de bajo nivel en el cliente, ya que el empaquetado binario y el transporte físico son gestionados íntegramente por el daemon de borde.

---

## Parámetros de Conexión CLI

Todos los ejemplos incluidos en `examples/` aceptan los siguientes argumentos unificados:

| Parámetro | Variable de Entorno | Descripción | Valor por Defecto |
|---|---|---|---|
| `--host` | `MCUBRIDGE_GATEWAY_HOST` | Dirección del servidor Cloud Gateway | `127.0.0.1` |
| `--port` | `MCUBRIDGE_GATEWAY_PORT` | Puerto HDLC mTLS del Cloud Gateway | `8443` |
| `--device-id` | `MCUBRIDGE_DEVICE_ID` | **Obligatorio**. Identificador del dispositivo destino | *(Ninguno)* |

Si se omite `--device-id` y no se ha definido `MCUBRIDGE_DEVICE_ID`, el script fallará inmediatamente con un error explícito.

---

## Catálogo de Ejemplos (`examples/`)

| Script | Descripción Funcional | Métodos RPC Pigweed Utilizados |
|---|---|---|
| `all_features_test.py` | Suite integral de validación de las capacidades expuestas por `GatewayControl` (GPIO, Datastore, FileIO mediante pw_transfer/borrado). | `DigitalWrite`, `AnalogWrite`, `DatastorePut`, `DatastoreGet`, `push_bytes_to_device`, `fetch_bytes_from_device`, `FileRemove` |
| `bootloader_test.py` | Reinicio controlado del MCU hacia el bootloader de programación. | `EnterBootloader` |
| `console_test.py` | Escritura hacia la consola del MCU (`rpc.pb.ConsoleService/Write`). La salida MCU → Linux se publica como telemetría (`evt/rpc.pb.ConsoleService/Write`). | `ConsoleWrite` |
| `datastore_test.py` | Lectura, escritura y verificación de pares clave-valor en RAM. | `DatastorePut`, `DatastoreGet` |
| `fileio_test.py` | Transferencia de archivos del MPU mediante `pw_transfer` staging y `FileTransferService`, y borrado con `FileRemove`. El recurso EEPROM del MCU se transfiere mediante `pw_transfer` (recurso `0x4D454741`). | `transfer_write`, `transfer_read`, `push_bytes_to_device`, `fetch_bytes_from_device`, `FileRemove` |
| `led13_test.py` | Alternancia periódica de encendido y apagado del LED Pin 13. | `DigitalWrite` |
| `mailbox_read_test.py` | Lectura no destructiva y consumo FIFO de la cola de mensajes. | `MailboxRead` |
| `pin_subscribe_test.py` | Alta y baja de una suscripción de pin en el MCU (`rpc.pb.GpioService/SubscribePins`). | `PinSubscribe` |
| `process_test.py` | Ejecución, sondeo y terminación de subprocesos en el MPU. | `ProcessRunAsync`, `ProcessPoll`, `ProcessKill` |
| `sensor_reader_test.py` | Muestreo continuo de lecturas analógicas y digitales. | `AnalogRead`, `DigitalRead` |
| `spi_test.py` | Configuración de bus SPI y transferencias de bytes full-duplex. | `SpiConfigure`, `SpiTransfer` |

---

## Guía Rápida de Ejecución

### 1. Iniciar el Gateway Pigweed C++ Central
```bash
./mcubridge-gateway-cpp/build/mcubridge_gateway --port 8443 --cert certs/gateway.crt --key certs/gateway.key
```

### 2. Ejecutar un Ejemplo CLI
Configurando el ID de dispositivo mediante variable de entorno:
```bash
export MCUBRIDGE_DEVICE_ID="yun-mcu-01"
python3 mcubridge-client-examples/examples/led13_test.py
```

O especificando el dispositivo explícitamente en la línea de comandos:
```bash
python3 mcubridge-client-examples/examples/sensor_reader_test.py \
    --host 127.0.0.1 \
    --port 8443 \
    --device-id yun-mcu-01
```

### 3. Ejemplo de Integración en Código Python

```python
import asyncio
from mcubridge_client import GatewayClient, GatewayConnection, mcu_wire_pb2


async def main():
    device_id = "yun-mcu-01"
    async with GatewayConnection.connect("127.0.0.1", 8443) as conn:
        client = GatewayClient(conn, target_device_id=device_id)

        # Invocación directa a través de Pigweed RPC GatewayControl
        response = await client.DigitalWrite(
            mcu_wire_pb2.DigitalWrite(pin=13, value=1)
        )
        print("Respuesta:", response)


asyncio.run(main())
```

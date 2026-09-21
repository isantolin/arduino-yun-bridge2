# Arquitectura de Clientes MCU Bridge v2 (gRPC Cloud Gateway)

[![Python](https://img.shields.io/badge/Python-3.13+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![gRPC](https://img.shields.io/badge/gRPC-HTTP%2F2-244c5a?logo=grpc)](https://grpc.io/)
[![Protobuf](https://img.shields.io/badge/Serialization-Protobuf_v3-green?logo=protobuf)](https://protobuf.dev/)
[![OpenWrt](https://img.shields.io/badge/OpenWrt-25.12.5-00B5E2?logo=openwrt)](https://openwrt.org/)

Este componente proporciona el SDK y la suite de herramientas CLI para que aplicaciones, dashboards y servicios de automatización interactúen con microcontroladores Arduino Yún a través del **Protobuf Cloud Gateway** centralizado (`mcubridge-gateway`).

## Arquitectura de Comunicación Centralizada

Todos los clientes se comunican **exclusivamente con el Cloud Gateway** mediante llamadas RPC fuertemente tipadas sobre gRPC (`LocalBridgeStub`). Se ha erradicado por completo el uso de sockets UNIX locales en los clientes, consolidando un plano de control distribuido y homogéneo:

```
┌──────────────────────────────────────┐
│  Client Applications / Scripts / CLI │
│    (mcubridge-client-examples)       │
└──────────────────┬───────────────────┘
                   │ gRPC (LocalBridge RPCs, x-device-id)
                   ▼
┌─────────────────────────────────────────────────────────────┐
│       Protobuf Cloud Gateway (Dedicated Central Server)     │
│  - GatewayLocalBridgeService (22 RPCs + SubscribeConsole)   │
│  - Enrutamiento estricto por metadato 'x-device-id'         │
│  - Multiplexación N:1 de dispositivos de borde              │
└──────────────────┬──────────────────────────────────────────┘
                   │ gRPC Bidirectional Session Stream
                   │ CommandRequest(command_path="rpc/<method>")
                   ▼
┌─────────────────────────────────────────────────────────────┐
│             McuBridge Daemon (Edge Linux / MPU)             │
│  - Outbound-only connection (Pure Push)                     │
│  - Delegated RPC execution (local_bridge_service)           │
│  - Local IPC via OpenWrt UBUS (ubus call mcubridge)         │
└──────────────────┬──────────────────────────────────────────┘
                   │ Serial RPC (COBS/R + CRC32)
                   ▼
┌─────────────────────────────────────────────────────────────┐
│                 MCU Firmware (Arduino AVR)                  │
└─────────────────────────────────────────────────────────────┘
```

---

## Resolución Explícita de Dispositivos (`x-device-id`)

El Cloud Gateway opera en una topología **Hub-and-Spoke (N:1)** donde múltiples placas Arduino Yún se conectan simultáneamente. Para garantizar determinismo e integridad SIL-2, **la resolución del dispositivo de destino es siempre explícita**:

1. **Metadato Obligatorio:** Toda invocación a `LocalBridge` debe incluir el metadato gRPC `x-device-id` con el identificador exacto del dispositivo de destino (p.ej. `yun-mcu-01`).
2. **Rechazo Inmediato sin Adopción Implícita:**
   - Si un cliente no envía el metadato `x-device-id`, el Gateway rechaza la llamada con error `GRPCError(Status.INVALID_ARGUMENT, "Missing x-device-id in request metadata")`.
   - Si el dispositivo solicitado no mantiene una sesión gRPC activa con el Gateway, se devuelve inmediatamente `GRPCError(Status.UNAVAILABLE, "Device '<id>' is not connected to gateway")`.
3. **Cero Tolerancia a Fallbacks Legacy:** No existe selección por omisión, auto-descubrimiento heurístico ni compatibilidad hacia sockets locales en los clientes.

---

## Requisitos e Instalación

El cliente requiere **Python 3.13+** y dependencias basadas en stubs oficiales gRPC y Protobuf:

```bash
pip install \
    "grpclib>=0.4.7" \
    "protobuf==7.36.0" \
    "tenacity>=9.0,<10" \
    "structlog>=24.4.0" \
    "typer>=0.12.5"
```

> **Nota:** No se requieren librerías de enlace serie (`pyserial`/`serialx`) ni utilidades de bajo nivel (`cobs`) en el cliente, ya que el empaquetado binario y el transporte físico son gestionados íntegramente por el daemon de borde.

---

## Parámetros de Conexión CLI

Todos los ejemplos incluidos en `examples/` aceptan los siguientes argumentos unificados:

| Parámetro | Variable de Entorno | Descripción | Valor por Defecto |
|---|---|---|---|
| `--host` | `MCUBRIDGE_GATEWAY_HOST` | Dirección del servidor Cloud Gateway | `127.0.0.1` |
| `--port` | `MCUBRIDGE_GATEWAY_PORT` | Puerto gRPC del Cloud Gateway | `8443` |
| `--device-id` | `MCUBRIDGE_DEVICE_ID` | **Obligatorio**. Identificador del dispositivo destino | *(Ninguno)* |

Si se omite `--device-id` y no se ha definido `MCUBRIDGE_DEVICE_ID`, el script fallará inmediatamente con un error explícito.

---

## Catálogo de Ejemplos (`examples/`)

| Script | Descripción Funcional | Métodos gRPC Utilizados |
|---|---|---|
| `all_features_test.py` | Suite integral de validación de todas las capacidades del Bridge. | GPIO, Datastore, Mailbox, Process, FileIO, SPI |
| `bootloader_test.py` | Reinicio controlado del MCU hacia el bootloader de programación. | `Publish` (`SYS_RESET`) |
| `console_test.py` | Streaming bidireccional en tiempo real de la consola del MCU. | `SubscribeConsole` |
| `datastore_test.py` | Lectura, escritura y verificación de pares clave-valor en RAM. | `DatastorePut`, `DatastoreGet` |
| `fileio_test.py` | Operaciones de sistema de archivos (escritura, lectura, borrado). | `FileWrite`, `FileRead`, `FileRemove` |
| `led13_test.py` | Alternancia periódica de encendido y apagado del LED Pin 13. | `DigitalWrite` |
| `mailbox_read_test.py` | Lectura no destructiva y consumo FIFO de la cola de mensajes. | `MailboxRead` |
| `pin_subscribe_test.py` | Suscripción asíncrona a cambios de estado de pines digitales. | `PinSubscribe` |
| `process_test.py` | Ejecución, sondeo y terminación de subprocesos en el MPU. | `ProcessRunAsync`, `ProcessPoll`, `ProcessKill` |
| `sensor_reader_test.py` | Muestreo continuo de lecturas analógicas y digitales. | `AnalogRead`, `DigitalRead` |
| `spi_test.py` | Configuración de bus SPI y transferencias de bytes full-duplex. | `SpiConfigure`, `SpiTransfer` |

---

## Guía Rápida de Ejecución

### 1. Iniciar el Gateway Central
```bash
python3 mcubridge-gateway/gateway.py --no-tls --port 8443 --metrics-port 9100
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
from grpclib.client import Channel
from mcubridge_client import pb, pb_grpc

async def main():
    device_id = "yun-mcu-01"
    async with Channel("127.0.0.1", 8443) as channel:
        stub = pb_grpc.LocalBridgeStub(channel)
        
        # Inyectar x-device-id en los metadatos de la llamada
        response = await stub.DigitalWrite(
            pb.DigitalWrite(pin=13, value=1),
            metadata={"x-device-id": device_id}
        )
        print("Respuesta:", response.status)

asyncio.run(main())
```

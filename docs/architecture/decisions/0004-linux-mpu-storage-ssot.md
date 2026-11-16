# ADR-0004: Filesystem de Linux MPU como SSOT de Almacenamiento Común

- **Estado:** Accepted (aprobado); modificado por [ADR-0007](0007-cloud-eeprom-access-and-persistent-storage.md)
- **Fecha:** 2026-11-15
- **Responsables de la decisión:** mantenedor del repositorio
- **Contexto:** PR #85 / Arquitectura McuBridge 2
- **Supersede:** Cualquier diseño que asuma emulación de filesystem distribuido en la memoria interna de los microcontroladores.

---

## 1. Contexto y Problema

El ecosistema McuBridge conecta microcontroladores heterogéneos con un microprocesador Linux MPU (OpenWrt):
* **AVR (ATmega2560):** Cuenta con 4.096 bytes de EEPROM interna física en el silicio, con alta resistencia (100.000 ciclos de escritura), accesible byte a byte.
* **ARM Cortex-M0+ (SAMD21G18A / MKR WiFi 1010):** No dispone de EEPROM física. Su almacenamiento interno es Flash NOR (256 KB) con borrado por filas de 256 bytes y resistencia acotada (del orden de 10⁴ ciclos por fila; confirmar contra la hoja de datos).
* **Xtensa LX7 (ESP32-S3 / Nano ESP32):** No dispone de EEPROM física. Usa Flash SPI externa con particiones NVS / LittleFS.

Intentar unificar el almacenamiento persistente forzando emulaciones de filesystem en la memoria interna de cada MCU genera:
1. **Riesgo crítico de desgaste de Flash (*flash wear-out*):** Escrituras periódicas de registros, eventos o estados agotan rápidamente los ciclos de borrado de la Flash interna del MCU, inutilizando permanentemente el chip.
2. **Bloqueo de CPU y tiempos no deterministas:** El borrado de sectores de Flash bloquea el microcontrolador entre 20 ms y 100 ms, interfiriendo con el bucle de control de tiempo real y el manejo de tramas HDLC del UART.
3. **Fragmentación de contratos:** La capacidad varía drásticamente (desde 1 KB emulado hasta varios megabytes), haciendo inviable un contrato común predecible.

---

## 2. Decisión Arquitectónica

Se adopta formalmente la **Opción 1**:

1. **Linux MPU Filesystem como SSOT de Almacenamiento:**
   * El sistema de archivos del procesador Linux OpenWrt (`file_system_root`; por defecto `/srv/mcubridge/files`, persistente en el overlay, o una tarjeta SD montada; ver ADR-0007) es la **única fuente de verdad (*Single Source of Truth*) para el almacenamiento común persistente**.
   * Todo almacenamiento jerárquico de archivos, telemetría histórica, binarios, configuraciones extensas y datos de aplicación reside en el MPU.

2. **Rol del Microcontrolador (MCU):**
   * El MCU no asume propiedad de filesystem.
   * Procesa sus datos en memoria volátil de alta velocidad y acceso determinista (SRAM mediante el servicio `DataStore`).
   * Delega las operaciones de persistencia hacia el MPU Linux.

3. **Canal Unificado de Transferencia hacia la Nube:**
   * Toda sincronización remota de archivos entre clientes, Gateway central y dispositivos edge opera **exclusivamente mediante streaming `pw_transfer` contra `FileTransferService`** (`PushToLinux` / `FetchFromLinux`).
   * Se prohíben y extirpan los métodos unarios legados que transportaban archivos completos en payloads Protobuf (`FileWrite`, `FileRead`).

4. **Delimitación de la EEPROM de Hardware:**
   * La EEPROM interna de 4 KiB del ATmega2560 (recurso fijo `0x4D454741`) permanece accesible vía `pw_transfer` como un recurso de hardware nativo de bajo nivel para compatibilidad de proyectos AVR (y, según ADR-0007, también desde la nube con permisos `eeprom_read`/`eeprom_write`), pero **no** constituye el modelo general de almacenamiento de la plataforma. El recurso expone los primeros 4.080 bytes; los últimos 16 bytes guardan el epoch de arranque del handshake ([ADR-0005](0005-mcu-contributed-handshake-nonce.md)).
   * Excepción acotada: el epoch de arranque de ADR-0005 es el único estado persistente propio del MCU (AVR: 16 bytes de EEPROM; SAMD21: dos filas NVM vía `FlashStorage`, una escritura por arranque). No es almacenamiento de aplicación y no contradice esta decisión.

---

## 3. Alternativas consideradas

* **Filesystem emulado en la memoria interna de cada MCU** (EEPROM emulada, LittleFS/NVS): descartada por desgaste de Flash, bloqueos de 20–100 ms en borrados y capacidades incompatibles entre arquitecturas.
* **Almacenamiento externo en el MCU (tarjeta SD por SPI):** depende del hardware de cada placa y duplica la validación de rutas y cuotas que ya hace el MPU.
* **Quitar el acceso a la EEPROM AVR:** descartada para mantener compatibilidad con proyectos AVR existentes; queda como recurso de hardware, no como contrato de almacenamiento.

---

## 4. Consecuencias

### Positivas
* **Protección del Silicio:** Cero desgaste de Flash en microcontroladores SAMD21 y ESP32; la vida útil de los nodos se preserva indefinidamente.
* **Seguridad y Control de Recursos:** El MPU Linux aplica centralizadamente validación estricta de rutas (prevención de *path traversal*), cuotas de almacenamiento configurables (`file_storage_quota_bytes`) y límites por archivo (`file_write_max_bytes`).
* **Contrato Homogéneo para la Nube:** La interfaz de transferencia de archivos para clientes y Gateway es idéntica independientemente del MCU conectado.

### Negativas / Mitigaciones
* **Dependencia del MPU para persistencia:** Si el MPU Linux está apagado o en arranque, el MCU solo puede retener datos temporalmente en su SRAM (o en su EEPROM física si es AVR).
* *Mitigación:* Para el caso de uso del Bridge, el MCU y el MPU están acoplados en la misma placa; el enlace HDLC sincroniza el estado (`LinkSync`) antes de habilitar el intercambio de datos.

---

## 5. Relacionados

* [ADR-0002](0002-pigweed-hdlc-rpc-migration.md): migración a Pigweed HDLC/RPC (`pw_transfer`).
* [ADR-0007](0007-cloud-eeprom-access-and-persistent-storage.md): acceso desde la nube a la EEPROM y almacenamiento persistente.
* [ADR-0005](0005-mcu-contributed-handshake-nonce.md): reserva de 16 bytes de EEPROM AVR y epoch SAMD21 para el nonce del MCU.
* [`docs/architecture/pw-transfer-integration.md`](../pw-transfer-integration.md): recurso EEPROM `0x4D454741` y frontera de transferencia de archivos.

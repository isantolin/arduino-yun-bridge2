# Arduino MCU Bridge 2

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![OpenWrt](https://img.shields.io/badge/OpenWrt-25.12.3-00B5E2?logo=openwrt)](https://openwrt.org/releases/25.12.3)
[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/release/python-3130/)
[![C++ Standard](https://img.shields.io/badge/C%2B%2B-17-00599C?logo=cplusplus)](https://isocpp.org/)
[![ETL](https://img.shields.io/badge/ETL-SIL--2%20Compliant-green)](https://www.etlcpp.com/)
[![FIPS 140-3](https://img.shields.io/badge/Security-FIPS%20140--3-critical)](https://csrc.nist.gov/publications/detail/fips/140/3/final)

**MCU Bridge 2 es un reemplazo moderno, robusto y agnóstico de hardware para el sistema Bridge original.**

Este proyecto re-imagina la comunicación entre el microcontrolador (MCU) y el procesador Linux (MPU) en dispositivos OpenWrt, utilizando un protocolo RPC binario basado en **Protobuf Enveloping** y un daemon asíncrono de alto rendimiento.

## Características Principales

- **Protobuf Enveloping (v3):** Todo el transporte está unificado bajo un esquema Protobuf, garantizando tipado fuerte y validación automática de rangos en el borde del sistema.
- **Zero-Boilerplate (C++):** Despacho de comandos automatizado mediante Variadic Templates, reduciendo el código manual y eliminando riesgos de seguridad.
- **Seguridad Funcional (SIL-2):** Librería MCU escrita en C++17 sin STL y sin alocación dinámica, utilizando contenedores ETL y Nanopb con límites estáticos.
- **MIL-SPEC Compliance (FIPS 140-3):** Implementación de **HKDF-SHA256** para derivación de claves y **ChaCha20-Poly1305** para cifrado AEAD con nonces monótonos.
- **Optimización de Tráfico:** Eliminación de compresión RLE manual en favor de la eficiencia nativa de Protobuf (Varints) y campos empaquetados.

### Novedades (Mayo 2026 - La Gran Purga)

- **Erradicación de RLE:** Se ha eliminado el codec legacy de compresión RLE. El sistema ahora es más simple, rápido y predecible.
- **Validación en Esquema:** Los rangos de pines y valores GPIO se validan ahora directamente en el paso de decodificación de Protobuf, eliminando condicionales manuales en el código de aplicación.
- **Aplanamiento de Transporte:** Refactorización total de la capa de transporte en Python para eliminar capas de abstracción innecesarias (Pasamanos) y mejorar la resiliencia ante fallos de link.

---

(Mantener resto de secciones de instalación y despliegue...)

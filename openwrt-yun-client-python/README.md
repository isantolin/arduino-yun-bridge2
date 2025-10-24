# Arquitectura del Cliente del Puente de Yun v2

Este componente (`openwrt-yun-client-python`) proporciona las herramientas para que las aplicaciones que se ejecutan en el lado Linux del Arduino Yun interactúen con el microcontrolador a través del `bridge_daemon.py`.

## API Dual: Local y Remota

El ecosistema ofrece dos modos de comunicación distintos, cada uno con un propósito claro:

1.  **Comunicación Local (API de Socket):**
    -   **Propósito:** Para scripts y aplicaciones que se ejecutan **directamente en el procesador Linux del Yun**.
    -   **Mecanismo:** La librería cliente se comunica con el `bridge_daemon.py` a través de un socket local (UDS).
    -   **Caso de uso:** Un script de Python en el Yun que monitoriza el uso de CPU y quiere mostrar el resultado en una pantalla LCD conectada al microcontrolador.

2.  **Comunicación Remota (API de Red vía MQTT):**
    -   **Propósito:** Para que aplicaciones, servicios o dispositivos **externos** (en la misma red o en internet) interactúen con el Yun.
    -   **Mecanismo:** El `mqtt_plugin.py` (cargado por el cliente) actúa como un puente entre el daemon local y un broker MQTT. Publica los datos del Arduino en tópicos MQTT y escucha en otros tópicos para enviar comandos al Arduino.
    -   **Caso de uso:** Un panel de control web (Dashboard) que se ejecuta en un servidor en la nube y muestra la temperatura leída por un sensor en el Arduino, y permite encender un LED desde el navegador.

Esta separación es una fortaleza clave del diseño, no una limitación.

## El Sistema de Plugins

La aparente "abstracción adicional" del sistema de plugins es intencionada y es lo que hace a este cliente tan potente y flexible.

-   **Fortalezas:**
    -   **Modularidad:** La lógica para interactuar con servicios externos (como MQTT) está contenida en su propio plugin. El núcleo del cliente no sabe nada sobre MQTT.
    -   **Extensibilidad:** Para conectar el bridge con otro servicio (una base de datos InfluxDB, una API REST, un bot de Telegram), solo se necesita crear un nuevo plugin. No es necesario modificar el código del daemon ni del cliente principal.

En resumen, aunque puede parecer complejo al principio, el sistema de plugins y la dualidad de la comunicación (local/remota) son decisiones de diseño deliberadas que hacen que el ecosistema sea robusto, modular y adaptable a casi cualquier caso de uso de IoT.

# Arquitectura del Cliente del Puente de Yun v2

Este componente (`openwrt-yun-client-python`) proporciona las herramientas para que las aplicaciones que se ejecutan en el lado Linux del Arduino Yun interactúen con el microcontrolador a través del `bridge_daemon.py`.

## API de Comunicación: MQTT

El ecosistema utiliza MQTT como el mecanismo principal de comunicación para interactuar con el `bridge_daemon.py`.

-   **Propósito:** Para scripts y aplicaciones que se ejecutan **tanto en el procesador Linux del Yun como externamente**.
-   **Mecanismo:** El `bridge_daemon.py` expone la funcionalidad del microcontrolador a través de un broker MQTT. Los clientes (como los ejemplos en este directorio) se conectan a este broker para enviar comandos y recibir datos.
-   **Caso de uso:** Un script de Python en el Yun que monitoriza el uso de CPU y quiere mostrar el resultado en una pantalla LCD conectada al microcontrolador, o un panel de control web (Dashboard) que se ejecuta en un servidor en la nube y muestra la temperatura leída por un sensor en el Arduino, y permite encender un LED desde el navegador.

## El Sistema de Plugins (Nota Histórica)

Versiones anteriores del diseño consideraban un sistema de plugins para extender la funcionalidad del cliente. Sin embargo, la arquitectura actual centraliza la lógica de puente en `bridge_daemon.py` y utiliza MQTT como la interfaz unificada. Esto simplifica el diseño y mejora la interoperabilidad.

En resumen, la comunicación se realiza exclusivamente a través de MQTT, lo que proporciona una solución robusta, modular y adaptable a casi cualquier caso de uso de IoT.

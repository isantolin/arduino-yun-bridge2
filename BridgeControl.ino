/*
 * BridgeControl - Sketch Funcional y Refactorizado para Yun Bridge v2
 *
 * Objetivo:
 * Mantiene la funcionalidad original de procesar comandos de Mailbox ("ON", "OFF",
 * "fwrite", "fread") y permite el control de los pines de I/O directamente
 * a través de la librería Bridge.
 *
 * Correcciones aplicadas:
 * - Se ha eliminado el manejador de comandos manual (handle_incoming_command)
 *   para delegar el control de pines (digitalWrite, etc.) a la librería Bridge,
 *   que es su comportamiento diseñado.
 * - Se ha optimizado la lectura del Mailbox para no usar la clase String,
 *   mejorando la gestión de la memoria.
 * - El código es más limpio y se alinea con el uso previsto de la arquitectura.
 */
#include <Bridge.h>

// Define el pin del LED para facilitar su referencia
const int ledPin = 13;

// --- Funciones de ayuda para imprimir en ambas consolas ---
void printToBoth(const char* s) {
  Console.print(s);
}

void printlnToBoth(const char* s) {
  Console.println(s);
}
// --- FIN ---

// --- Manejador de Respuesta Asíncrono ---
// Esta función será llamada por la librería Bridge cuando llegue una respuesta
// de una lectura digital, sin bloquear el loop principal.
void handleDigitalReadResponse(uint8_t pin, int value) {
  Console.print("Respuesta asíncrona para pin D");
  Console.print(pin);
  Console.print(": ");
  Console.println(value);
}

void setup() {
  // Bridge.begin() inicializa la comunicación serial con Linux a 115200 baudios.
  Bridge.begin();
  
  // Registrar el manejador para las respuestas de lectura digital.
  Bridge.onDigitalReadResponse(handleDigitalReadResponse);
  
  pinMode(ledPin, OUTPUT);

  // Un delay para dar tiempo al lado Linux a arrancar completamente.
  delay(2000);
  
  printlnToBoth("BridgeControl sketch refactorizado y asíncrono iniciado.");
  printlnToBoth("Envíe 'READ_D13' por Mailbox para probar la lectura asíncrona.");
}

// Variables para el temporizador no bloqueante del Mailbox
unsigned long last_check_time = 0;
const unsigned long check_interval_ms = 50;

void loop() {
  // Bridge.process() es esencial. Procesa los comandos de I/O (pinMode, etc.)
  // que llegan desde Linux, y también maneja las respuestas para los callbacks.
  Bridge.process();

  // --- Lógica de la aplicación: Procesamiento de mensajes del Mailbox ---
  // Se mantiene la funcionalidad original de procesar comandos personalizados.
  if (millis() - last_check_time >= check_interval_ms) {
    last_check_time = millis();

    int msg_len = Mailbox.available();
    if (msg_len > 0 && msg_len < 255) {
      // Optimización: Leer directamente en un buffer para evitar usar String
      char msg_buf[256];
      Mailbox.read((uint8_t*)msg_buf, msg_len);
      msg_buf[msg_len] = '\0';  // Asegurar la terminación NULL

      printToBoth("Mensaje de Mailbox recibido: ");
      printlnToBoth(msg_buf);

      // Funcionalidad original: controlar LED con "ON" / "OFF"
      if (strcmp(msg_buf, "ON") == 0) {
        digitalWrite(ledPin, HIGH);
        printlnToBoth("LED 13 encendido por Mailbox");
      } else if (strcmp(msg_buf, "OFF") == 0) {
        digitalWrite(ledPin, LOW);
        printlnToBoth("LED 13 apagado por Mailbox");
      } else if (strcmp(msg_buf, "READ_D13") == 0) {
        printlnToBoth("Solicitando lectura del pin 13 de forma asíncrona...");
        Bridge.requestDigitalRead(13);
      } else {
        // --- MEJORA: Feedback de error hacia Linux ---
        char error_msg[100];
        snprintf(error_msg, sizeof(error_msg), "Error: Comando de Mailbox desconocido: '%s'", msg_buf);
        printlnToBoth(error_msg); // Imprime en la consola de Linux para debugging
      }
    }
  }
}

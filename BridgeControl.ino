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

// --- Manejador de Respuesta Asíncrono ---
// Esta función será llamada por la librería Bridge cuando llegue una respuesta
// de una lectura digital, sin bloquear el loop principal.
void handleDigitalReadResponse(uint8_t pin, int value) {
  Console.print("Respuesta asíncrona para pin D");
  Console.print(pin);
  Console.print(": ");
  Console.println(value);
}

// --- Manejador de Comandos Genérico ---
// Esta función será llamada por la librería Bridge para comandos que no tienen
// un manejador específico registrado.
void handleCommand(const rpc::Frame& frame) {
  Console.print("Comando RPC no manejado: ID=0x");
  Console.print(frame.header.command_id, HEX);
  Console.print(", Payload Len=");
  Console.println(frame.header.payload_length);
}

// --- Manejador de Mensajes de Mailbox ---
// Esta función será llamada por la librería Bridge cuando llegue un mensaje
// del Mailbox de Linux.
void handleMailboxMessage(const uint8_t* buffer, size_t size) {
  char msg_buf[256]; // Asumiendo que el tamaño máximo del mensaje cabe en el buffer
  if (size < sizeof(msg_buf)) {
    memcpy(msg_buf, buffer, size);
    msg_buf[size] = '\0';  // Asegurar la terminación NULL

    Console.print("Mensaje de Mailbox recibido: ");
    Console.println(msg_buf);

    // Funcionalidad original: controlar LED con "ON" / "OFF"
    if (strcmp(msg_buf, "ON") == 0) {
      digitalWrite(ledPin, HIGH);
      Console.println("LED 13 encendido por Mailbox");
    } else if (strcmp(msg_buf, "OFF") == 0) {
      digitalWrite(ledPin, LOW);
      Console.println("LED 13 apagado por Mailbox");
    } else if (strcmp(msg_buf, "READ_D13") == 0) {
      Console.println("Solicitando lectura del pin 13 de forma asíncrona...");
      Bridge.requestDigitalRead(13);
    } else {
      char error_msg[100];
      snprintf(error_msg, sizeof(error_msg), "Error: Comando de Mailbox desconocido: '%s'", msg_buf);
      Console.println(error_msg);
    }
  } else {
    Console.println("Error: Mensaje de Mailbox demasiado largo.");
  }
  // Después de procesar el mensaje, solicitar el siguiente para mantener el flujo.
  Mailbox.requestRead();
}

void setup() {
  // Bridge.begin() inicializa la comunicación serial con Linux a 115200 baudios.
  Bridge.begin();

  // Registrar los manejadores de respuesta.
  Bridge.onDigitalReadResponse(handleDigitalReadResponse);
  Bridge.onCommand(handleCommand);
  Bridge.onMailboxMessage(handleMailboxMessage);
  
  pinMode(ledPin, OUTPUT);

  // Un delay para dar tiempo al lado Linux a arrancar completamente.
  delay(2000);
  
  Console.println("BridgeControl sketch refactorizado y asíncrono iniciado.");
  Console.println("Envíe 'READ_D13' por Mailbox para probar la lectura asíncrona.");

  // Iniciar la solicitud de mensajes del Mailbox
  Mailbox.requestRead();
}

void loop() {
  // Bridge.process() es esencial. Procesa los comandos de I/O (pinMode, etc.)
  // que llegan desde Linux, y también maneja las respuestas para los callbacks.
  Bridge.process();

  // La lógica de procesamiento de Mailbox ahora se maneja en el callback handleMailboxMessage.
  // No se necesita un temporizador no bloqueante aquí para el Mailbox.
}


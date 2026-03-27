/*
 * BridgeControl - Sketch Funcional con Password en Runtime
 *
 * [CONFIGURACIÓN]
 * Para ahorrar memoria (Flash/RAM) desactivando servicios (Process, FileSystem, etc.), 
 * DEBES editar el archivo de la librería:
 * -> mcubridge-library-arduino/src/config/bridge_config.h
 *
 * Cambiar los #define BRIDGE_ENABLE_... de 1 a 0 según sea necesario.
 */

// CONFIGURACIÓN DEL SECRETO
// Este password debe coincidir con el configurado en el lado de Linux
// (/etc/config/mcubridge o similar)
#ifndef BRIDGE_SECRET
#define BRIDGE_SECRET "DEBUG_INSECURE"
// SECURITY WARNING: Using default BRIDGE_SECRET. Change this before production use!
#endif

#include <Bridge.h>
#include <string.h>

void setup() {
  // [SIL-2] PROHIBIDO usar Serial.print() si comparte puerto con el Bridge.
  // En emulación, Serial (UART0) es el canal del protocolo. Cualquier texto
  // enviado aquí corromperá el stream COBS y bloqueará la sincronización.

  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SECRET);

  Bridge.onDigitalReadResponse(BridgeClass::DigitalReadHandler::create([](uint8_t value) {
    Console.print(F("Respuesta asíncrona de lectura digital: "));
    Console.println(value);
  }));

  Bridge.onCommand(BridgeClass::CommandHandler::create([](const rpc::Frame& frame) {
    Console.print(F("Comando RPC no manejado: ID=0x"));
    Console.println(frame.header.command_id, HEX);
  }));

  Mailbox.onMailboxMessage(MailboxClass::MailboxHandler::create([](etl::span<const uint8_t> buffer) {
    char msg_buf[80];
    if (buffer.size() < sizeof(msg_buf)) {
      memcpy(msg_buf, buffer.data(), buffer.size());
      msg_buf[buffer.size()] = '\0';

      Console.print(F("Mensaje de Mailbox recibido: "));
      Console.println(msg_buf);

      if (strcmp(msg_buf, "ON") == 0) {
        digitalWrite(13, HIGH);
        Console.println(F("LED 13 encendido por Mailbox"));
      } else if (strcmp(msg_buf, "OFF") == 0) {
        digitalWrite(13, LOW);
        Console.println(F("LED 13 apagado por Mailbox"));
      } else {
        Console.print(F("Comando desconocido: "));
        Console.println(msg_buf);
      }
    }
  }));

  Bridge.onStatus(BridgeClass::StatusHandler::create([](rpc::StatusCode status_code, etl::span<const uint8_t> payload) {
    (void)payload;
    // Solo imprimir errores graves para evitar saturación
    if (status_code != rpc::StatusCode::STATUS_OK) {
      Console.print(F("Error de Estado: 0x"));
      Console.println(static_cast<uint8_t>(status_code), HEX);
    }
  }));

  // Bloqueo controlado hasta sincronizar SIN LOGS a Serial.
  while (!Bridge.isSynchronized()) {
    Bridge.process();
    // Podemos usar el LED para feedback visual si fuera hardware real
  }

  // Una vez sincronizado, Console es seguro porque viaja dentro de marcos RPC.
  Console.begin();
  Console.println(F("Bridge sincronizado y operando."));
}

void loop() {
  Bridge.process();

  static unsigned long lastMailboxCheck = 0;
  if (millis() - lastMailboxCheck > 500) {
    lastMailboxCheck = millis();
    Mailbox.requestRead();
  }
}

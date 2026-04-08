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

#include <Bridge.h>
#include <services/Console.h>
#include <services/Mailbox.h>
#include <services/FileSystem.h>
#include <services/Process.h>
#include <string.h>

// [MIL-SPEC] Shared secret must match the daemon configuration.
#ifndef BRIDGE_SERIAL_SHARED_SECRET
#define BRIDGE_SERIAL_SHARED_SECRET "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"
#endif

void setup() {
  // [SIL-2] PROHIBIDO usar Serial.print() si comparte puerto con el Bridge.
  // En emulación, Serial (UART0) es el canal del protocolo. Cualquier texto
  // enviado aquí corromperá el stream COBS y bloqueará la sincronización.

  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SERIAL_SHARED_SECRET);

  Bridge.onCommand(BridgeClass::CommandHandler::create([](const rpc::Frame& frame) {
    Console.print(F("Comando RPC no manejado: ID=0x"));
    Console.println(frame.header.command_id, HEX);
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
#if BRIDGE_ENABLE_MAILBOX
    Mailbox.requestRead();
#endif
  }
}

/*
 * BridgeControl - Sketch Funcional con Password en Runtime
 *
 * [CONFIGURACIÓN]
 * Para ahorrar memoria (Flash/RAM) desactivando servicios (Process, FileSystem,
 * etc.), DEBES editar el archivo de la librería:
 * -> mcubridge-library-arduino/src/config/bridge_config.h
 *
 * Cambiar los #define BRIDGE_ENABLE_... de 1 a 0 según sea necesario.
 */

#include <Bridge.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <services/Console.h>
#include <services/FileSystem.h>
#include <services/Mailbox.h>
#include <services/Process.h>
#include <wolfssl.h>
#include <wolfssl/wolfcrypt/settings.h>

// [MIL-SPEC] Shared secret must match the daemon configuration.
#ifndef BRIDGE_SERIAL_SHARED_SECRET
#define BRIDGE_SERIAL_SHARED_SECRET \
  "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"
#endif

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  // [SIL-2] PROHIBIDO usar Serial.print() si comparte puerto con el Bridge.
  // En emulación, Serial (UART0) es el canal del protocolo. Cualquier texto
  // enviado aquí corromperá el stream COBS y bloqueará la sincronización.

  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SERIAL_SHARED_SECRET);

  Bridge.onCommand(BridgeClass::CommandHandler::create(
      [](const rpc_pb_RpcEnvelope& envelope) {
        Console.print(F("Comando RPC no manejado: ID=0x"));
        Console.println(envelope.command_id, HEX);
      }));

  Bridge.onStatus(BridgeClass::StatusHandler::create(
      [](rpc::StatusCode status_code, etl::span<const uint8_t> payload) {
        (void)payload;
        // Solo imprimir errores graves para evitar saturación
        if (status_code != rpc::StatusCode::STATUS_OK) {
          Console.print(F("Error de Estado: 0x"));
          Console.println(static_cast<uint8_t>(status_code), HEX);
        }
      }));

  // [SIL-2] Bounded synchronization: abort to safe state if Linux unreachable.
  {
    constexpr size_t OUTER_STEPS = 300;
    constexpr size_t INNER_STEPS = 50;
    etl::array<uint8_t, OUTER_STEPS> outer_ticks{};
    etl::array<uint8_t, INNER_STEPS> inner_polls{};
    const uint32_t sync_deadline = millis() + bridge::config::SYNC_TIMEOUT_MS;
    auto sync_it = etl::find_if(outer_ticks.begin(), outer_ticks.end(), [&](uint8_t) {
      if (Bridge.isSynchronized()) return true;
      if (static_cast<int32_t>(millis() - sync_deadline) > 0) return true;
      auto inner_it = etl::find_if(inner_polls.begin(), inner_polls.end(), [&](uint8_t) {
        if (Bridge.isSynchronized()) return true;
        Bridge.process();
        delay(2);
        return false;
      });
      (void)inner_it;
      return Bridge.isSynchronized();
    });
    (void)sync_it;
    if (!Bridge.isSynchronized()) {
      Bridge.enterSafeState();
    }
  }

  // Una vez sincronizado, Console es seguro porque viaja dentro de marcos RPC.
  if (Bridge.isSynchronized()) {
    Console.begin();
    Console.println(F("Bridge sincronizado y operando."));
  }
}

void loop() {
  Bridge.process();

  static unsigned long lastMailboxCheck = 0;
  if (millis() - lastMailboxCheck > 500) {
    lastMailboxCheck = millis();
#if BRIDGE_ENABLE_MAILBOX
    if (Bridge.isSynchronized()) {
      Mailbox.requestRead();
    }
#endif
  }
}

/*
 * BridgeCloud - Reference Example for Cloud-Connected Edge Nodes (N:1 Gateway)
 *
 * Demonstrates SIL-2 Zero-Heap RPC bridge connected through OpenWrt edge daemon
 * to the centralized mcubridge-gateway server.
 *
 * Capabilities demonstrated:
 * 1. Bounded cryptographic handshake (ChaCha20-Poly1305 AEAD).
 * 2. Asynchronous pin/sensor telemetry streaming via Bridge.sendPinEvent().
 * 3. Remote cloud actuation commands (LED control via incoming RPC).
 * 4. Resilient fault detection and status notification via Bridge.onStatus().
 */

#include <Arduino.h>
#include <Bridge.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <services/Console.h>
#include <wolfssl.h>
#include <wolfssl/wolfcrypt/settings.h>

// [MIL-SPEC] Shared secret must match the daemon configuration.
#ifndef BRIDGE_SERIAL_SHARED_SECRET
#define BRIDGE_SERIAL_SHARED_SECRET \
  "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"
#endif

// Telemetry reporting interval in milliseconds
constexpr uint32_t TELEMETRY_INTERVAL_MS = 2000;
constexpr uint8_t SENSOR_PIN = A0;

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  // [SIL-2] PROHIBIDO usar Serial.print() si comparte puerto con el Bridge.
  // La comunicación serie se reserva exclusivamente para los marcos binarios
  // COBS.
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SERIAL_SHARED_SECRET);

  // Registrar callback para comandos remotos despachados desde el Gateway
  // Central
  Bridge.onCommand(BridgeClass::CommandHandler::create(
      [](const rpc_pb_RpcEnvelope& envelope) {
        // En caso de recibir comandos específicos o eventos de control
        if (envelope.command_id ==
            rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE)) {
          // El daemon en Linux ya procesa la escritura de pin directamente,
          // pero el MCU puede interceptar comandos personalizados si se desea.
        }
      }));

  // Registrar observador de estado para registrar anomalías
  Bridge.onStatus(BridgeClass::StatusHandler::create(
      [](rpc::StatusCode status_code, etl::span<const uint8_t> payload) {
        (void)payload;
        if (status_code != rpc::StatusCode::STATUS_OK) {
          // Si ocurre un error, parpadear rápido para diagnóstico físico
          digitalWrite(LED_BUILTIN, HIGH);
        }
      }));

  // [SIL-2] Bounded synchronization: abortar a safe state si Linux no responde
  {
    const uint32_t sync_deadline = millis() + bridge::config::SYNC_TIMEOUT_MS;
    while (!Bridge.isSynchronized()) {
      if (static_cast<int32_t>(millis() - sync_deadline) > 0) {
        Bridge.enterSafeState();
        break;
      }
      Bridge.process();
    }
  }

  // Confirmación de sincronización con encendido de LED
  if (Bridge.isSynchronized()) {
    digitalWrite(LED_BUILTIN, HIGH);
#if BRIDGE_ENABLE_CONSOLE
    Console.begin();
    Console.println(F("BridgeCloud: Conectado a OpenWrt y sincronizado."));
#endif
  }
}

void loop() {
  // Procesar eventos de enlace y tramas RPC entrantes
  Bridge.process();

  // Publicación periódica de telemetría de sensor hacia el Cloud Gateway
  static unsigned long last_telemetry_ms = 0;
  const unsigned long current_ms = millis();

  if (Bridge.isSynchronized() &&
      (current_ms - last_telemetry_ms >= TELEMETRY_INTERVAL_MS)) {
    last_telemetry_ms = current_ms;

    // Lectura analógica de sensor (temperatura, luz, tensión, etc.)
    const uint16_t sensor_reading =
        static_cast<uint16_t>(analogRead(SENSOR_PIN));

    // [SIL-2 / Zero-Heap] Enviar evento de telemetría directamente al Gateway
    Bridge.sendPinEvent(SENSOR_PIN, sensor_reading);
  }
}

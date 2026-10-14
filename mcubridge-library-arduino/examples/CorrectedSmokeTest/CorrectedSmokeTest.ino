/*
 * CorrectedSmokeTest.ino - Full-stack E2E Verification
 */
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

void setup() {
  // [SIL-2] Force safe state for actuators before enabling interrupts or
  // protocol
  pinMode(13, OUTPUT);
  digitalWrite(13, LOW);

  // Initialize Bridge
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SERIAL_SHARED_SECRET);

  // Initialize Console
  Console.begin();

  // CRITICAL: Wait for Bridge to be synchronized (Linux handshake complete)
  // This prevents "Rejecting MCU frame before link synchronisation" errors.
  // Non-blocking LED blink continues while waiting for handshake.
  {
    constexpr size_t OUTER_STEPS = 300;
    constexpr size_t INNER_STEPS = 50;
    etl::array<uint8_t, OUTER_STEPS> outer_ticks{};
    etl::array<uint8_t, INNER_STEPS> inner_polls{};
    const uint32_t sync_deadline = millis() + bridge::config::SYNC_TIMEOUT_MS;
    bool ledState = false;
    auto sync_it = etl::find_if(outer_ticks.begin(), outer_ticks.end(), [&](uint8_t) {
      if (Bridge.isSynchronized()) return true;
      if (static_cast<int32_t>(millis() - sync_deadline) > 0) return true;
      ledState = !ledState;
      digitalWrite(13, ledState ? HIGH : LOW);
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

  if (Bridge.isSynchronized()) {
    digitalWrite(13, HIGH);  // Indicate handshake complete
  }
}

void loop() {
  // Main processing loop
  Bridge.process();

  // Example: Echo serial console input back to verify bi-directional link
  etl::array<uint8_t, 32> echo_buf{};
  const size_t avail = etl::min(static_cast<size_t>(Console.available()), echo_buf.size());
  etl::for_each(echo_buf.begin(), echo_buf.begin() + avail, [](uint8_t) {
    int c = Console.read();
    if (c >= 0) {
      Console.write(static_cast<uint8_t>(c));
    }
  });

  /*
  static long lastPrint = 0;
  if (millis() - lastPrint > 1000) {
    lastPrint = millis();
    Console.println("Estado: 0x05 (TIMEOUT)");
  }
  */
}

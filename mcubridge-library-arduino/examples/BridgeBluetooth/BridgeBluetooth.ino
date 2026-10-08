/*
 * BridgeBluetooth.ino - Reference Example for Arduino MCU Bridge 2 over
 * Bluetooth SPP
 *
 * Demonstrates SIL-2 Zero-Heap RPC bridge over a Bluetooth serial stream.
 * Uses Classic SPP on ESP32 targets that provide it and Serial1 with external
 * Bluetooth UART modules on BLE-only or non-ESP32 targets.
 */

#include <Arduino.h>
#if defined(ESP32) && defined(CONFIG_BT_SPP_ENABLED)
#include <BluetoothSerial.h>
BluetoothSerial SerialBT;
#define BT_STREAM SerialBT
#elif defined(HAVE_HWSERIAL1) || defined(ARDUINO_ARCH_SAMD) || \
    defined(ARDUINO_ARCH_SAM) || defined(ESP32)
#define BT_STREAM Serial1
// For single-UART boards (e.g. Arduino Uno / Nano) connected to Bluetooth
// module on primary Serial
#else
#define BT_STREAM Serial
#endif
#include <Bridge.h>
#include <wolfssl.h>
#include <wolfssl/wolfcrypt/settings.h>

#ifndef BRIDGE_SERIAL_SHARED_SECRET
#define BRIDGE_SERIAL_SHARED_SECRET \
  "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"
#endif

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

#if defined(ESP32) && defined(CONFIG_BT_SPP_ENABLED)
  SerialBT.begin("McuBridge_Device");
#elif defined(HAVE_HWSERIAL1) || defined(ARDUINO_ARCH_SAMD) || \
    defined(ARDUINO_ARCH_SAM) || defined(ESP32)
  Serial1.begin(115200);
#else
  Serial.begin(115200);
#endif

  // Initialize bridge directly on the Bluetooth Stream with authentication
  Bridge.setStream(BT_STREAM);
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SERIAL_SHARED_SECRET);

  // [SIL-2] Bounded synchronization: wait for daemon handshake
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

  digitalWrite(LED_BUILTIN, HIGH);
}

void loop() { Bridge.process(); }

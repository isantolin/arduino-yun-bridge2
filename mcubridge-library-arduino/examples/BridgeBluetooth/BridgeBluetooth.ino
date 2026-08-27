/*
 * BridgeBluetooth.ino - Reference Example for Arduino MCU Bridge 2 over
 * Bluetooth SPP
 *
 * Demonstrates SIL-2 Zero-Heap RPC bridge over Bluetooth serial stream.
 * Compatible with ESP32 (BluetoothSerial) and transparent BLE/SPP UART modules.
 */

#include <Arduino.h>
#if defined(ESP32)
#include <BluetoothSerial.h>
BluetoothSerial SerialBT;
#define BT_STREAM SerialBT
#elif defined(HAVE_HWSERIAL1) || defined(ARDUINO_AVR_MEGA2560) || defined(ARDUINO_AVR_MEGA) || defined(ARDUINO_AVR_YUN) || defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM)
// For boards with dedicated secondary hardware serial
#define BT_STREAM Serial1
#else
// For single-UART boards (e.g. Arduino Uno / Nano) connected to Bluetooth module on primary Serial
#define BT_STREAM Serial
#endif
#include <Bridge.h>
#include <wolfssl.h>
#include <wolfssl/wolfcrypt/settings.h>

BridgeClass BridgeBT(BT_STREAM);

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

#if defined(ESP32)
  SerialBT.begin("McuBridge_Device");
#elif defined(HAVE_HWSERIAL1) || defined(ARDUINO_AVR_MEGA2560) || defined(ARDUINO_AVR_MEGA) || defined(ARDUINO_AVR_YUN) || defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM)
  Serial1.begin(115200);
#else
  Serial.begin(115200);
#endif

  // Initialize bridge directly on the Bluetooth Stream
  BridgeBT.begin();
  digitalWrite(LED_BUILTIN, HIGH);
}

void loop() { BridgeBT.process(); }

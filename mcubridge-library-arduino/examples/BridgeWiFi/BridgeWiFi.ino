/*
 * BridgeWiFi.ino - Reference Example for Arduino MCU Bridge 2 over WiFi TCP
 *
 * Demonstrates SIL-2 Zero-Heap RPC bridge over wireless TCP stream
 * (WiFiClient). Framing (COBS/R + CRC32) and AEAD ChaCha20-Poly1305 encryption
 * operate transparently across wireless sockets.
 */

#include <Arduino.h>

#if defined(__has_include)
#if __has_include(<WiFi.h>)
#include <WiFi.h>
#elif __has_include(<ESP8266WiFi.h>)
#include <ESP8266WiFi.h>
#elif __has_include(<WiFiNINA.h>)
#include <WiFiNINA.h>
#else
// Transparent UART Coprocessor Stream for AVR architectures / hardware
// emulation
#if defined(HAVE_HWSERIAL1) || defined(ARDUINO_AVR_MEGA2560) || \
    defined(ARDUINO_AVR_MEGA) || defined(ARDUINO_AVR_YUN) ||    \
    defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM)
#define WIFI_UART_STREAM Serial1
#else
#define WIFI_UART_STREAM Serial
#endif

class WiFiClient : public Stream {
 public:
  int connect(const char*, uint16_t) {
    WIFI_UART_STREAM.begin(115200);
    _connected = 1;
    return 1;
  }
  uint8_t connected() { return _connected; }
  int available() override { return WIFI_UART_STREAM.available(); }
  int read() override { return WIFI_UART_STREAM.read(); }
  int peek() override { return WIFI_UART_STREAM.peek(); }
  void flush() override { WIFI_UART_STREAM.flush(); }
  size_t write(uint8_t b) override { return WIFI_UART_STREAM.write(b); }
  size_t write(const uint8_t* buffer, size_t size) override {
    return WIFI_UART_STREAM.write(buffer, size);
  }

 private:
  uint8_t _connected{0};
};
enum wl_status_t { WL_CONNECTED = 3 };
enum WiFiMode { WIFI_STA = 1 };
struct WiFiClass {
  void mode(WiFiMode) {}
  void begin(const char*, const char*) {}
  wl_status_t status() { return WL_CONNECTED; }
};
inline WiFiClass WiFi;
#endif
#else
#include <WiFi.h>
#endif

#include <Bridge.h>
#include <wolfssl.h>
#include <wolfssl/wolfcrypt/settings.h>

// Configuration
constexpr const char* WIFI_SSID = "McuBridge_Network";
constexpr const char* WIFI_PASS = "BridgeSafePass";
constexpr const char* BRIDGE_HOST = "192.168.1.1";
constexpr uint16_t BRIDGE_PORT = 9000;

#ifndef BRIDGE_SERIAL_SHARED_SECRET
#define BRIDGE_SERIAL_SHARED_SECRET \
  "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"
#endif

WiFiClient client;
unsigned long last_reconnect_attempt = 0;
constexpr unsigned long RECONNECT_INTERVAL_MS = 3000;

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
  }

  // Connect to Python MPU Bridge daemon over TCP with authentication
  if (client.connect(BRIDGE_HOST, BRIDGE_PORT)) {
    Bridge.setStream(client);
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
}

void loop() {
  if (client.connected()) {
    Bridge.process();
  } else {
    digitalWrite(LED_BUILTIN, LOW);
    unsigned long now = millis();
    if (now - last_reconnect_attempt >= RECONNECT_INTERVAL_MS) {
      last_reconnect_attempt = now;
      if (WiFi.status() == WL_CONNECTED &&
          client.connect(BRIDGE_HOST, BRIDGE_PORT)) {
        Bridge.setStream(client);
        Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, BRIDGE_SERIAL_SHARED_SECRET);
        digitalWrite(LED_BUILTIN, HIGH);
      }
    }
  }
}

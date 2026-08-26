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
// Fallback definitions for host static analysis & IDE indexers
class WiFiClient : public Stream {
 public:
  int connect(const char*, uint16_t) { return 1; }
  uint8_t connected() { return 1; }
  int available() override { return 0; }
  int read() override { return -1; }
  int peek() override { return -1; }
  void flush() override {}
  size_t write(uint8_t) override { return 1; }
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

WiFiClient client;
BridgeClass BridgeWiFiInstance(client);
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

  // Connect to Python MPU Bridge daemon over TCP
  if (client.connect(BRIDGE_HOST, BRIDGE_PORT)) {
    BridgeWiFiInstance.begin();
    digitalWrite(LED_BUILTIN, HIGH);
  }
}

void loop() {
  if (client.connected()) {
    BridgeWiFiInstance.process();
  } else {
    digitalWrite(LED_BUILTIN, LOW);
    unsigned long now = millis();
    if (now - last_reconnect_attempt >= RECONNECT_INTERVAL_MS) {
      last_reconnect_attempt = now;
      if (WiFi.status() == WL_CONNECTED &&
          client.connect(BRIDGE_HOST, BRIDGE_PORT)) {
        BridgeWiFiInstance.begin();
        digitalWrite(LED_BUILTIN, HIGH);
      }
    }
  }
}

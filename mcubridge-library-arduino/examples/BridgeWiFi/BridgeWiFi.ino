/*
 * BridgeWiFi.ino - Reference Example for Arduino MCU Bridge 2 over WiFi TCP
 *
 * Demonstrates SIL-2 Zero-Heap RPC bridge over wireless TCP stream
 * (WiFiClient). Framing (COBS/R + CRC32) and AEAD ChaCha20-Poly1305 encryption
 * operate transparently across wireless sockets.
 */

#include <Arduino.h>
#if defined(ESP8266)
#include <ESP8266WiFi.h>
#elif defined(ESP32)
#include <WiFi.h>
#else
#include <WiFiNINA.h>
#endif
#include <Bridge.h>

// Configuration
constexpr const char* WIFI_SSID = "McuBridge_Network";
constexpr const char* WIFI_PASS = "BridgeSafePass";
constexpr const char* BRIDGE_HOST = "192.168.1.1";
constexpr uint16_t BRIDGE_PORT = 9000;

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

  // Connect to Python MPU Bridge daemon over TCP
  if (client.connect(BRIDGE_HOST, BRIDGE_PORT)) {
    Bridge.begin(client);
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
        Bridge.begin(client);
        digitalWrite(LED_BUILTIN, HIGH);
      }
    }
  }
}

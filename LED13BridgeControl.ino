// LEDBridgeControl.ino
// Generic sketch for controlling any pin via Bridge (Serial1)
// Use this as a base for integration with YunBridge Python and WebUI

#include <Bridge.h>


// Detect board type and set MAX_PINS accordingly
#if defined(ARDUINO_AVR_YUN) || defined(ARDUINO_AVR_UNO)
  #define MAX_PINS 20   // Yun/Uno: 0-19
#elif defined(ARDUINO_AVR_MEGA2560)
  #define MAX_PINS 54   // Mega: 0-53
#elif defined(ARDUINO_AVR_LEONARDO)
  #define MAX_PINS 20   // Leonardo: 0-19
#else
  #define MAX_PINS 20   // Default/fallback
#endif

int pinStates[MAX_PINS];

void setPin(int pin, bool state) {
  if (pin < 0 || pin >= MAX_PINS) return;
  pinMode(pin, OUTPUT);
  digitalWrite(pin, state ? HIGH : LOW);
  pinStates[pin] = state ? HIGH : LOW;
}

void reportPinState(int pin) {
  if (pin < 0 || pin >= MAX_PINS) return;
  String msg = "PIN" + String(pin) + " STATE ";
  msg += (digitalRead(pin) == HIGH) ? "ON" : "OFF";
  Serial1.print(msg); Serial1.print('\n');
  delay(2); // Pequeño delay para evitar pegado de mensajes
}

void setup() {
  Bridge.begin();
  Serial.begin(115200);
  for (int i = 0; i < MAX_PINS; i++) pinStates[i] = LOW;
  pinMode(13, OUTPUT); // Default/test pin
  pinStates[13] = LOW;
}

void loop() {
  if (Serial1.available()) {
    String raw = "";
    Serial.print("[DEBUG] Serial1 buffer: ");
    while (Serial1.available()) {
      char c = Serial1.read();
      Serial.print("["); Serial.print((int)c); Serial.print(":"); Serial.print(c); Serial.print("] ");
      raw += c;
      if (c == '\n') break;
    }
    Serial.println();
    Serial.print("[DEBUG] Full raw buffer before trim: ");
    Serial.println(raw);
    raw.trim();
    Serial.print("[DEBUG] Command received (raw): ");
    Serial.println(raw);

    // Match commands: PIN<N> ON, PIN<N> OFF, PIN<N>:ON, PIN<N>:OFF, MAILBOX <msg>
    int pin = 13; // Default
    bool matched = false;
    if (raw.startsWith("PIN")) {
      int idx = 3;
      String pinStr = "";
      while (idx < raw.length() && isDigit(raw[idx])) {
        pinStr += raw[idx];
        idx++;
      }
      if (pinStr.length() > 0) {
        pin = pinStr.toInt();
      }
      String rest = raw.substring(idx);
      rest.trim();
      if (rest == "ON" || rest == ":ON") {
        setPin(pin, true);
        Serial.print("Pin "); Serial.print(pin); Serial.println(" ON");
        Serial1.print("PIN" + String(pin) + " STATE ON\n");
        delay(2);
        matched = true;
      } else if (rest == "OFF" || rest == ":OFF") {
        setPin(pin, false);
        Serial.print("Pin "); Serial.print(pin); Serial.println(" OFF");
        Serial1.print("PIN" + String(pin) + " STATE OFF\n");
        delay(2);
        matched = true;
      } else if (rest.startsWith("STATE")) {
        reportPinState(pin);
        matched = true;
      }
    } else if (raw.startsWith("MAILBOX ")) {
      String msg = raw.substring(8);
      Serial.print("[MAILBOX] Mensaje recibido: ");
      Serial.println(msg);
      matched = true;
    }
    if (!matched) {
      Serial.print("[DEBUG] Unrecognized command: ");
      Serial.println(raw);
    }
  }
  // Debug: indicate that the loop is still running
  static unsigned long lastPrint = 0;
  if (millis() - lastPrint > 2000) {
    Serial.println("[DEBUG] Loop active");
    lastPrint = millis();
  }
}

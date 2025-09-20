
// LEDBridgeControl.ino
// Generic sketch for controlling any pin via Bridge (Serial1)
// Use this as a base for integration with YunBridge Python and WebUI

#include <Bridge.h>

// Map to keep track of pin states (for reporting)
#include <map>
std::map<int, int> pinStates;

void setPin(int pin, bool state) {
  pinMode(pin, OUTPUT);
  digitalWrite(pin, state ? HIGH : LOW);
  pinStates[pin] = state ? HIGH : LOW;
}

void reportPinState(int pin) {
  String msg = "PIN" + String(pin) + " STATE ";
  msg += (digitalRead(pin) == HIGH) ? "ON" : "OFF";
  Serial1.println(msg);
}

void setup() {
  Bridge.begin();
  Serial.begin(115200);
  pinMode(13, OUTPUT); // Default/test pin
  pinStates[13] = LOW;
}

void loop() {
  if (Serial1.available()) {
    String raw = "";
    while (Serial1.available()) {
      char c = Serial1.read();
      raw += c;
      if (c == '\n') break;
    }
    raw.trim();
    Serial.print("[DEBUG] Command received (raw): ");
    Serial.println(raw);

    // Match commands: PIN<N> ON, PIN<N> OFF, PIN<N>:ON, PIN<N>:OFF
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
      String rest = raw.substring(idx).trim();
      if (rest == "ON" || rest == ":ON") {
        setPin(pin, true);
        Serial.print("Pin "); Serial.print(pin); Serial.println(" ON");
        Serial1.println("PIN" + String(pin) + " STATE ON");
        matched = true;
      } else if (rest == "OFF" || rest == ":OFF") {
        setPin(pin, false);
        Serial.print("Pin "); Serial.print(pin); Serial.println(" OFF");
        Serial1.println("PIN" + String(pin) + " STATE OFF");
        matched = true;
      } else if (rest.startsWith("STATE")) {
        reportPinState(pin);
        matched = true;
      }
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

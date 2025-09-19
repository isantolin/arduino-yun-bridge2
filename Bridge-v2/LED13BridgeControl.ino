// LED13BridgeControl.ino
// Generic sketch for controlling LED 13 via Bridge (Serial1)
// Use this as a base for integration with YunBridge Python and WebUI

#include <Bridge.h>

void setup() {
  Bridge.begin();
  Serial.begin(9600);
  pinMode(13, OUTPUT);
}

void loop() {
  if (Serial1.available()) {
    String cmd = Serial1.readStringUntil('\n');
    cmd.trim();
    if (cmd == "LED13 ON" || cmd == "LED13:ON") {
      digitalWrite(13, HIGH);
      Serial.println("LED 13 ON");
    } else if (cmd == "LED13 OFF" || cmd == "LED13:OFF") {
      digitalWrite(13, LOW);
      Serial.println("LED 13 OFF");
    }
  }
}

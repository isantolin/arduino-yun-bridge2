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
    Serial.println("[DEBUG] Serial1 available!");
    // Leer y mostrar cada byte recibido para debug
    String raw = "";
    while (Serial1.available()) {
      char c = Serial1.read();
      raw += c;
      Serial.print("[DEBUG] Byte recibido: ");
      Serial.print(c);
      Serial.print(" (ASCII: ");
      Serial.print((int)c);
      Serial.println(")");
      // Salir si se detecta salto de línea
      if (c == '\n') break;
    }
    raw.trim();
    Serial.print("[DEBUG] Comando recibido (raw): ");
    Serial.println(raw);
    if (raw == "LED13 ON" || raw == "LED13:ON") {
      digitalWrite(13, HIGH);
      Serial.println("LED 13 ON");
      Serial1.println("LED13 STATE ON");
    } else if (raw == "LED13 OFF" || raw == "LED13:OFF") {
      digitalWrite(13, LOW);
      Serial.println("LED 13 OFF");
      Serial1.println("LED13 STATE OFF");
    } else {
      Serial.print("[DEBUG] Comando no reconocido: ");
      Serial.println(raw);
    }
  }
  // Debug: indicar que el loop sigue corriendo
  static unsigned long lastPrint = 0;
  if (millis() - lastPrint > 2000) {
    Serial.println("[DEBUG] Loop activo");
    lastPrint = millis();
  }
}

// YunBridge Example: Console Test
#include <Bridge.h>

void setup() {
  Bridge.begin();
  Serial.begin(115200);
  delay(1000);
  Serial1.println("CONSOLE hello_console");
  delay(200);
  while (Serial1.available()) Serial.write(Serial1.read());
}

void loop() {}

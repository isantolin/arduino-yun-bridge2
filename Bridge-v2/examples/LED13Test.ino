// Bridge-v2 Example: LED 13 Test
#include <Bridge.h>

void setup() {
  Bridge.begin();
  pinMode(13, OUTPUT);
}

void loop() {
  Bridge.led13On();
  digitalWrite(13, HIGH);
  delay(1000);
  Bridge.led13Off();
  digitalWrite(13, LOW);
  delay(1000);
}

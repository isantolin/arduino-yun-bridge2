// Bridge-v2 Example: Key-Value Store Test
#include <Bridge.h>

void setup() {
  Bridge.begin();
  Serial.begin(115200);
  delay(1000);
  Serial1.println("SET foo bar");
  delay(200);
  while (Serial1.available()) Serial.write(Serial1.read());
  Serial1.println("GET foo");
  delay(200);
  while (Serial1.available()) Serial.write(Serial1.read());
}

void loop() {}

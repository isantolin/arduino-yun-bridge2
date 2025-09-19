// Bridge-v2 Example: File I/O Test
#include <Bridge.h>

void setup() {
  Bridge.begin();
  Serial.begin(9600);
  delay(1000);
  Serial1.println("WRITEFILE /tmp/bridge_test.txt hello_bridge");
  delay(200);
  while (Serial1.available()) Serial.write(Serial1.read());
  Serial1.println("READFILE /tmp/bridge_test.txt");
  delay(200);
  while (Serial1.available()) Serial.write(Serial1.read());
}

void loop() {}

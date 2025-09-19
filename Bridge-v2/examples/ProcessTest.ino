// Bridge-v2 Example: Process Execution Test
#include <Bridge.h>

void setup() {
  Bridge.begin();
  Serial.begin(9600);
  delay(1000);
  Serial1.println("RUN echo hello_from_yun");
  delay(200);
  while (Serial1.available()) Serial.write(Serial1.read());
}

void loop() {}

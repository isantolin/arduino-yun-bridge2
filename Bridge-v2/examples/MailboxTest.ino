// Bridge-v2 Example: Mailbox Test
#include <Bridge.h>

void setup() {
  Bridge.begin();
  Serial.begin(115200);
  delay(1000);
  Serial1.println("MAILBOX SEND hello_mailbox");
  delay(200);
  while (Serial1.available()) Serial.write(Serial1.read());
  Serial1.println("MAILBOX RECV");
  delay(200);
  while (Serial1.available()) Serial.write(Serial1.read());
}

void loop() {}

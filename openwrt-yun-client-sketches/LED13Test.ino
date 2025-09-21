

// YunBridge Example: Generic Pin Test (default: 13)
#include <Bridge.h>

// Set testPin to any digital pin you want to test (default: 13)
const int testPin = 13; // Change this to test other pins

void setup() {
  Bridge.begin();
  pinMode(testPin, OUTPUT);
}

void loop() {
  // Turn the pin ON using YunBridge and digitalWrite
  Bridge.pinOn(testPin);
  digitalWrite(testPin, HIGH);
  delay(1000);
  // Turn the pin OFF using YunBridge and digitalWrite
  Bridge.pinOff(testPin);
  digitalWrite(testPin, LOW);
  delay(1000);
}

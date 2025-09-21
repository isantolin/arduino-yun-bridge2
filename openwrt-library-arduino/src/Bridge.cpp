// Bridge-v2: Arduino Yun Bridge Library (Implementation)
#include "Bridge.h"

void BridgeClass::begin() {
    Serial1.begin(115200); // Yun internal serial
}


void BridgeClass::led13On() {
    pinOn(13);
}

void BridgeClass::led13Off() {
    pinOff(13);
}

void BridgeClass::pinOn(int pin) {
    Serial1.print("PIN");
    Serial1.print(pin);
    Serial1.println(" ON");
}

void BridgeClass::pinOff(int pin) {
    Serial1.print("PIN");
    Serial1.print(pin);
    Serial1.println(" OFF");
}

BridgeClass Bridge;

// Bridge-v2: Arduino Yun Bridge Library (Implementation)
#include "Bridge.h"

void BridgeClass::begin() {
    Serial1.begin(115200); // Yun internal serial
}

void BridgeClass::led13On() {
    Serial1.println("LED13 ON");
}

void BridgeClass::led13Off() {
    Serial1.println("LED13 OFF");
}

BridgeClass Bridge;

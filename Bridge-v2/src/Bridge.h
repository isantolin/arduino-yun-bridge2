// Bridge-v2: Arduino Yun Bridge Library (Header)
// Compatible with legacy Bridge API, extended for v2
#ifndef BRIDGE_V2_H
#define BRIDGE_V2_H

#include <Arduino.h>

class BridgeClass {
public:
    void begin();
    void led13On();
    void led13Off();
    // TODO: Add more API methods for full compatibility
};

extern BridgeClass Bridge;

#endif // BRIDGE_V2_H

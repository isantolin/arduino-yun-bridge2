/*
 * This file is part of Arduino Yun Ecosystem v2.
 *
 * Copyright (C) 2025 Ignacio Santolin and contributors
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */
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

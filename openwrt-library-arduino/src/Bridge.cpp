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

// --- Pin Control ---
void BridgeClass::pinOn(int pin) {
    Serial1.print("PIN ");
    Serial1.print(pin);
    Serial1.println(" ON");
}

void BridgeClass::pinOff(int pin) {
    Serial1.print("PIN ");
    Serial1.print(pin);
    Serial1.println(" OFF");
}

void BridgeClass::pinState(int pin) {
    Serial1.print("PIN ");
    Serial1.print(pin);
    Serial1.println(" STATE");
}

// --- Process Execution ---
void BridgeClass::run(const char* command) {
    Serial1.print("RUN ");
    Serial1.println(command);
}

// --- Key-Value Store ---
void BridgeClass::get(const char* key) {
    Serial1.print("GET ");
    Serial1.println(key);
}

void BridgeClass::set(const char* key, const char* value) {
    Serial1.print("SET ");
    Serial1.print(key);
    Serial1.print(" ");
    Serial1.println(value);
}

// --- File I/O ---
void BridgeClass::writeFile(const char* path, const char* data) {
    Serial1.print("WRITEFILE ");
    Serial1.print(path);
    Serial1.print(" ");
    Serial1.println(data);
}

void BridgeClass::readFile(const char* path) {
    Serial1.print("READFILE ");
    Serial1.println(path);
}

// --- Console & Mailbox ---
void BridgeClass::console(const char* message) {
    Serial1.print("CONSOLE ");
    Serial1.println(message);
}

void BridgeClass::mailbox(const char* message) {
    Serial1.print("MAILBOX ");
    Serial1.println(message);
}

BridgeClass Bridge;

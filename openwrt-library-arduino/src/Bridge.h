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
// Bridge-v2: Arduino Yun Bridge Library (Header)
// Compatible with legacy Bridge API, extended for v2
#ifndef BRIDGE_V2_H
#define BRIDGE_V2_H

#include <Arduino.h>

class BridgeClass {
public:
    void begin();

    // --- Pin Control ---
    void pinOn(int pin);
    void pinOff(int pin);
    void pinState(int pin);

    // --- Process Execution ---
    void run(const char* command);

    // --- Key-Value Store ---
    void get(const char* key);
    void set(const char* key, const char* value);

    // --- File I/O ---
    void writeFile(const char* path, const char* data);
    void readFile(const char* path);

    // --- Console & Mailbox ---
    void console(const char* message);
    void mailbox(const char* message);
};

extern BridgeClass Bridge;

#endif // BRIDGE_V2_H

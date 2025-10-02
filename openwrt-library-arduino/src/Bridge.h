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
// Bridge-v2: Arduino Yun Bridge Library (Header) - RPC Implementation
#ifndef BRIDGE_V2_H
#define BRIDGE_V2_H

#include <Arduino.h>
#include "rpc_frame.h"
#include "Print.h"

#define CONSOLE_RX_BUFFER_SIZE 64

class ConsoleClass : public Print {
public:
    ConsoleClass();
    void begin();
    virtual size_t write(uint8_t);
    virtual size_t write(const uint8_t *buffer, size_t size);
    int available();
    int read();
    int peek();
    void flush();
    
    explicit operator bool() const {
      return _begun;
    }

    // Internal method for Bridge to push data into the console
    void _push(const uint8_t* buffer, size_t size);

private:
    bool _begun;
    uint8_t _rx_buffer[CONSOLE_RX_BUFFER_SIZE];
    volatile uint16_t _rx_buffer_head;
    volatile uint16_t _rx_buffer_tail;
};

class DataStoreClass {
public:
    DataStoreClass();
    void put(const String& key, const String& value);
    String get(const String& key);

private:
    friend class BridgeClass;
};

class MailboxClass {
private:
    uint8_t _buffer[256];
    size_t _length;

public:
    MailboxClass();
    void begin();

    // Send a message to the Linux side
    void send(const String& message);
    void send(const uint8_t* data, size_t length);

    // Check how many messages are waiting
    int available();

    // Read a message, returns bytes read or -1 if no message
    int read(uint8_t* buffer, size_t length);
    String readString();

    friend class BridgeClass;
};

class FileSystemClass {
public:
  void begin();
  
  // Write data to a file. This will overwrite the file.
  void write(const String& filePath, const String& data);
  void write(const String& filePath, const uint8_t* data, size_t length);

  // Read the entire content of a file
  String read(const String& filePath);

  // Remove a file
  void remove(const String& filePath);
};

class ProcessClass {
public:
    ProcessClass();

    // Synchronous run, returns stdout
    String run(const String& command);

    // Asynchronous run, returns a process ID (pid)
    int runAsynchronously(const String& command);

    // Poll an asynchronous process for output
    String poll(int pid);

    // Kill a running process
    void kill(int pid);
};

class BridgeClass {
public:
    BridgeClass(Stream& stream);

    void begin();
    void process(); // Must be called in loop() to process incoming RPC frames

    // --- Callback for command processing ---
    typedef void (*CommandHandler)(const rpc::Frame& frame);
    void onCommand(CommandHandler handler);

    // --- Core Arduino Functions ---
    void pinMode(uint8_t pin, uint8_t mode);
    void digitalWrite(uint8_t pin, uint8_t value);
    void analogWrite(uint8_t pin, int value);
    int digitalRead(uint8_t pin);
    int analogRead(uint8_t pin);

    // --- Internal ---
    void sendFrame(uint16_t command_id, const uint8_t* payload, uint16_t payload_len);
    String waitForResponse(uint16_t command, unsigned long timeout = 1000);
    String waitForResponse(uint16_t command, const uint8_t* payload, uint16_t payload_len, unsigned long timeout = 1000);
    int waitForResponseAsInt(uint16_t command, unsigned long timeout = 1000);
    int waitForResponseAsInt(uint16_t command, const uint8_t* payload, uint16_t payload_len, unsigned long timeout = 1000);

private:
    Stream& _stream;
    volatile bool _response_received;
    uint16_t _waiting_for_cmd;
    uint8_t _response_payload[256];
    uint16_t _response_len;

    // RPC Frame handling
    rpc::FrameParser _parser;
    rpc::FrameBuilder _builder;
    CommandHandler _command_handler;

    void dispatch(const rpc::Frame& frame);
};

extern BridgeClass Bridge;
extern ConsoleClass Console;
extern DataStoreClass DataStore;
extern MailboxClass Mailbox;
extern FileSystemClass FileSystem;
extern ProcessClass Process;

#endif // BRIDGE_V2_H
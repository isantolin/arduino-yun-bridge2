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
#include "Bridge.h"
#include "rpc_protocol.h"
#include <string.h>

using namespace rpc;

// =================================================================================
// Global Instances
// =================================================================================

BridgeClass Bridge(Serial1); // Use Serial1 for hardware serial communication with Linux
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

// =================================================================================
// ConsoleClass
// =================================================================================

ConsoleClass::ConsoleClass() : 
  _begun(false),
  _rx_buffer_head(0), 
  _rx_buffer_tail(0) {}

void ConsoleClass::begin() {
  _begun = true;
  _rx_buffer_head = 0;
  _rx_buffer_tail = 0;
}

size_t ConsoleClass::write(uint8_t c) {
  return write(&c, 1);
}

size_t ConsoleClass::write(const uint8_t *buffer, size_t size) {
  if (!_begun) return 0;
  Bridge.sendFrame(CMD_CONSOLE_WRITE, buffer, size);
  return size;
}

int ConsoleClass::available() {
  return (_rx_buffer_head - _rx_buffer_tail + CONSOLE_RX_BUFFER_SIZE) % CONSOLE_RX_BUFFER_SIZE;
}

int ConsoleClass::peek() {
  if (_rx_buffer_head == _rx_buffer_tail) return -1;
  return _rx_buffer[_rx_buffer_tail];
}

int ConsoleClass::read() {
  if (_rx_buffer_head == _rx_buffer_tail) return -1;
  uint8_t c = _rx_buffer[_rx_buffer_tail];
  _rx_buffer_tail = (_rx_buffer_tail + 1) % CONSOLE_RX_BUFFER_SIZE;
  return c;
}

void ConsoleClass::flush() {}

void ConsoleClass::_push(const uint8_t* buffer, size_t size) {
  for (size_t i = 0; i < size; i++) {
    uint16_t next_head = (_rx_buffer_head + 1) % CONSOLE_RX_BUFFER_SIZE;
    if (next_head != _rx_buffer_tail) {
      _rx_buffer[_rx_buffer_head] = buffer[i];
      _rx_buffer_head = next_head;
    } else {
      // Buffer is full, discard character
    }
  }
}

// =================================================================================
// DataStoreClass
// =================================================================================

DataStoreClass::DataStoreClass() {}

void DataStoreClass::put(const String& key, const String& value) {
    size_t key_len = key.length();
    size_t value_len = value.length();
    size_t payload_len = key_len + 1 + value_len; // key + null + value
    if (payload_len > MAX_PAYLOAD_SIZE) return;

    uint8_t payload[payload_len];
    memcpy(payload, key.c_str(), key_len);
    payload[key_len] = '\0';
    memcpy(payload + key_len + 1, value.c_str(), value_len);

    Bridge.sendFrame(CMD_DATASTORE_PUT, payload, payload_len);
}

String DataStoreClass::get(const String& key) {
    return Bridge.waitForResponse(CMD_DATASTORE_GET, (const uint8_t*)key.c_str(), key.length());
}

// =================================================================================
// MailboxClass
// =================================================================================

MailboxClass::MailboxClass() {}

void MailboxClass::begin() {}

int MailboxClass::available() {
  return Bridge.waitForResponseAsInt(CMD_MAILBOX_AVAILABLE);
}

String MailboxClass::readString() {
  return Bridge.waitForResponse(CMD_MAILBOX_READ);
}

// =================================================================================
// FileSystemClass
// =================================================================================

void FileSystemClass::begin() {}

void FileSystemClass::write(const String& filePath, const String& data) {
  write(filePath, (const uint8_t*)data.c_str(), data.length());
}

void FileSystemClass::write(const String& filePath, const uint8_t* data, size_t length) {
  if (filePath.length() == 0) return;

  size_t payload_len = filePath.length() + 1 + length;
  if (payload_len > MAX_PAYLOAD_SIZE) return;

  uint8_t* payload = (uint8_t*)malloc(payload_len);
  if (!payload) return;

  memcpy(payload, filePath.c_str(), filePath.length());
  payload[filePath.length()] = '\0';
  memcpy(payload + filePath.length() + 1, data, length);

  Bridge.sendFrame(CMD_FILE_WRITE, payload, payload_len);
  free(payload);
}

String FileSystemClass::read(const String& filePath) {
  return Bridge.waitForResponse(CMD_FILE_READ, (const uint8_t*)filePath.c_str(), filePath.length());
}

void FileSystemClass::remove(const String& filePath) {
  if (filePath.length() == 0) return;
  Bridge.sendFrame(CMD_FILE_REMOVE, (const uint8_t*)filePath.c_str(), filePath.length());
}

// =================================================================================
// ProcessClass
// =================================================================================

ProcessClass::ProcessClass() {}

String ProcessClass::run(const String& command) {
    return Bridge.waitForResponse(CMD_PROCESS_RUN, (const uint8_t*)command.c_str(), command.length());
}

int ProcessClass::runAsynchronously(const String& command) {
    return Bridge.waitForResponseAsInt(CMD_PROCESS_RUN_ASYNC, (const uint8_t*)command.c_str(), command.length());
}

String ProcessClass::poll(int pid) {
    char pid_str[12];
    itoa(pid, pid_str, 10);
    return Bridge.waitForResponse(CMD_PROCESS_POLL, (const uint8_t*)pid_str, strlen(pid_str));
}

void ProcessClass::kill(int pid) {
    char pid_str[12];
    itoa(pid, pid_str, 10);
    Bridge.sendFrame(CMD_PROCESS_KILL, (const uint8_t*)pid_str, strlen(pid_str));
}

// =================================================================================
// BridgeClass
// =================================================================================

BridgeClass::BridgeClass(Stream& stream)
    : _stream(stream), _parser(), _builder(), _command_handler(nullptr) {}

void BridgeClass::begin() {
  // Set the baud rate for communication with the Linux processor.
  // This must match the rate used by the daemon.
  static_cast<HardwareSerial*>(&_stream)->begin(115200);
  _parser.reset();
  Console.begin();
}

void BridgeClass::onCommand(CommandHandler handler) {
    _command_handler = handler;
}

void BridgeClass::process() {
    while (_stream.available()) {
        uint8_t byte = _stream.read();
        rpc::Frame frame;
        if (_parser.consume(byte, frame)) {
            dispatch(frame);
        }
    }
}

void BridgeClass::dispatch(const rpc::Frame& frame) {
    // Check if this is a response to a waiting command
    if (_waiting_for_cmd != 0 && frame.header.command_id == _waiting_for_cmd) {
        uint16_t copy_len = min((uint16_t)sizeof(_response_payload) - 1, frame.header.payload_length);
        memcpy(_response_payload, frame.payload, copy_len);
        _response_payload[copy_len] = '\0'; // Null-terminate
        _response_len = copy_len;
        _response_received = true;
        return;
    }

    // If it's a response command (ID >= 0x80) but we weren't waiting for it,
    // it's a late/unexpected response. Ignore it and don't bother the user sketch.
    if (frame.header.command_id >= 0x80) {
        return;
    }

    // If it's not a response, pass it to the registered command handler
    if (_command_handler) {
        _command_handler(frame);
    }
}

void BridgeClass::sendFrame(uint16_t command_id, const uint8_t* payload, uint16_t payload_len) {
    _builder.build(_stream, command_id, payload, payload_len);
}

String BridgeClass::waitForResponse(uint16_t command, const uint8_t* payload, uint16_t payload_len, unsigned long timeout) {
    sendFrame(command, payload, payload_len);

    // The `command + 1` convention is wrong. We need to map requests to their specific response IDs.
    uint16_t expected_response_cmd = 0;
    switch(command) {
        case CMD_DIGITAL_READ: expected_response_cmd = CMD_DIGITAL_READ_RESP; break;
        case CMD_ANALOG_READ: expected_response_cmd = CMD_ANALOG_READ_RESP; break;
        case CMD_DATASTORE_GET: expected_response_cmd = CMD_DATASTORE_GET_RESP; break;
        case CMD_MAILBOX_READ: expected_response_cmd = CMD_MAILBOX_READ_RESP; break;
        case CMD_MAILBOX_AVAILABLE: expected_response_cmd = CMD_MAILBOX_AVAILABLE_RESP; break;
        case CMD_FILE_READ: expected_response_cmd = CMD_FILE_READ_RESP; break;
        case CMD_PROCESS_RUN: expected_response_cmd = CMD_PROCESS_RUN_RESP; break;
        case CMD_PROCESS_RUN_ASYNC: expected_response_cmd = CMD_PROCESS_RUN_ASYNC_RESP; break;
        case CMD_PROCESS_POLL: expected_response_cmd = CMD_PROCESS_POLL_RESP; break;
        default: expected_response_cmd = 0; // Should not happen for a command that expects a response
    }
    _waiting_for_cmd = expected_response_cmd;

    _response_received = false;
    _response_len = 0;

    unsigned long start = millis();
    while (millis() - start < timeout) {
        process();
        if (_response_received) {
            _waiting_for_cmd = 0;
            return String((char*)_response_payload);
        }
    }
    _waiting_for_cmd = 0;
    return String(); // Timeout
}

String BridgeClass::waitForResponse(uint16_t command, unsigned long timeout) {
    return waitForResponse(command, NULL, 0, timeout);
}

int BridgeClass::waitForResponseAsInt(uint16_t command, const uint8_t* payload, uint16_t payload_len, unsigned long timeout) {
    String response = waitForResponse(command, payload, payload_len, timeout);
    if (response.length() > 0) {
        return response.toInt();
    }
    return 0; // Or some error indicator
}

int BridgeClass::waitForResponseAsInt(uint16_t command, unsigned long timeout) {
    return waitForResponseAsInt(command, NULL, 0, timeout);
}


// --- Public API Methods ---

void BridgeClass::pinMode(uint8_t pin, uint8_t mode) {
    uint8_t payload[2] = {pin, mode};
    sendFrame(CMD_SET_PIN_MODE, payload, 2);
}

void BridgeClass::digitalWrite(uint8_t pin, uint8_t value) {
    uint8_t payload[2] = {pin, value};
    sendFrame(CMD_DIGITAL_WRITE, payload, 2);
}

void BridgeClass::analogWrite(uint8_t pin, int value) {
    uint8_t payload[2] = {pin, (uint8_t)value};
    sendFrame(CMD_ANALOG_WRITE, payload, 2);
}

int BridgeClass::digitalRead(uint8_t pin) {
    uint8_t payload[1] = {pin};
    return waitForResponseAsInt(CMD_DIGITAL_READ, payload, 1);
}

int BridgeClass::analogRead(uint8_t pin) {
    uint8_t payload[1] = {pin};
    return waitForResponseAsInt(CMD_ANALOG_READ, payload, 1);
}

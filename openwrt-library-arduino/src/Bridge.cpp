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

#include <string.h>

#include "rpc_protocol.h"

// CRITICAL: This baud rate MUST match the baud rate configured for the
// bridge daemon on the Linux side (via LuCI or UCI config).
// Mismatching baud rates will result in communication failure.
#define BRIDGE_BAUDRATE 115200

using namespace rpc;

// =================================================================================
// Global Instances
// =================================================================================

BridgeClass Bridge(
    Serial1);  // Use Serial1 for hardware serial communication with Linux
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

// =================================================================================
// ConsoleClass
// =================================================================================

ConsoleClass::ConsoleClass()
    : _begun(false),
      _rx_buffer_head(0),
      _rx_buffer_tail(0),
      _xoff_sent(false) {}

void ConsoleClass::begin() {
  _begun = true;
  _rx_buffer_head = 0;
  _rx_buffer_tail = 0;
  _xoff_sent = false;
}

size_t ConsoleClass::write(uint8_t c) { return write(&c, 1); }

size_t ConsoleClass::write(const uint8_t* buffer, size_t size) {
  if (!_begun) return 0;
  Bridge.sendFrame(CMD_CONSOLE_WRITE, buffer, size);
  return size;
}

int ConsoleClass::available() {
  return (_rx_buffer_head - _rx_buffer_tail + CONSOLE_RX_BUFFER_SIZE) %
         CONSOLE_RX_BUFFER_SIZE;
}

int ConsoleClass::peek() {
  if (_rx_buffer_head == _rx_buffer_tail) return -1;
  return _rx_buffer[_rx_buffer_tail];
}

int ConsoleClass::read() {
  if (_rx_buffer_head == _rx_buffer_tail) return -1;
  uint8_t c = _rx_buffer[_rx_buffer_tail];
  _rx_buffer_tail = (_rx_buffer_tail + 1) % CONSOLE_RX_BUFFER_SIZE;

  // Flow Control: Check if the buffer has emptied enough to resume
  // transmission. If we had previously sent an XOFF, and the buffer is now
  // below the low water mark, send an XON to the Linux side to tell it it's
  // safe to send more data.
  if (_xoff_sent && available() < CONSOLE_BUFFER_LOW_WATER) {
    Bridge.sendFrame(CMD_XON, nullptr, 0);
    _xoff_sent = false;
  }

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
      // Buffer is full, discard character. This is a data loss scenario that
      // the XON/XOFF flow control mechanism is designed to prevent.
    }
  }

  // Flow Control: Check if the buffer is getting too full.
  // If we haven't sent an XOFF yet and the buffer is above the high water mark,
  // send an XOFF to the Linux side to tell it to pause sending data.
  if (!_xoff_sent && available() > CONSOLE_BUFFER_HIGH_WATER) {
    Bridge.sendFrame(CMD_XOFF, nullptr, 0);
    _xoff_sent = true;
  }
}

// =================================================================================
// DataStoreClass
// =================================================================================

DataStoreClass::DataStoreClass() {}

void DataStoreClass::put(const char* key, const char* value) {
  size_t key_len = strlen(key);
  size_t value_len = strlen(value);
  size_t payload_len = key_len + 1 + value_len;  // key + null + value
  if (payload_len > rpc::MAX_PAYLOAD_SIZE) return;

  uint8_t payload[rpc::MAX_PAYLOAD_SIZE];
  memcpy(payload, key, key_len);
  payload[key_len] = '\0';
  memcpy(payload + key_len + 1, value, value_len);

  Bridge.sendFrame(CMD_DATASTORE_PUT, payload, payload_len);
}

int DataStoreClass::get(const char* key, char* buffer, size_t length) {
  return Bridge.waitForResponse(CMD_DATASTORE_GET, (const uint8_t*)key,
                                strlen(key), buffer, length);
}

// =================================================================================
// MailboxClass
// =================================================================================

MailboxClass::MailboxClass() : _length(0) {
  memset(_buffer, 0, sizeof(_buffer));
}

void MailboxClass::begin() {}

int MailboxClass::available() {
  return Bridge.waitForResponseAsInt(CMD_MAILBOX_AVAILABLE);
}

String MailboxClass::readString() {
  return Bridge.waitForResponse(CMD_MAILBOX_READ);
}

void MailboxClass::send(const String& message) {
  Bridge.sendFrame(CMD_MAILBOX_PROCESSED, (const uint8_t*)message.c_str(),
                   message.length());
}

void MailboxClass::send(const uint8_t* data, size_t length) {
  Bridge.sendFrame(CMD_MAILBOX_PROCESSED, data, length);
}

int MailboxClass::read(uint8_t* buffer, size_t length) {
  return Bridge.waitForResponse(CMD_MAILBOX_READ, NULL, 0, (char*)buffer,
                                length);
}

// =================================================================================
// FileSystemClass
// =================================================================================

void FileSystemClass::begin() {}

void FileSystemClass::write(const String& filePath, const String& data) {
  write(filePath, (const uint8_t*)data.c_str(), data.length());
}

void FileSystemClass::write(const String& filePath, const uint8_t* data,
                            size_t length) {
  if (filePath.length() == 0) return;

  size_t payload_len = filePath.length() + 1 + length;
  if (payload_len > rpc::MAX_PAYLOAD_SIZE) return;

  uint8_t payload[rpc::MAX_PAYLOAD_SIZE];

  memcpy(payload, filePath.c_str(), filePath.length());
  payload[filePath.length()] = '\0';
  memcpy(payload + filePath.length() + 1, data, length);

  Bridge.sendFrame(CMD_FILE_WRITE, payload, payload_len);
}

String FileSystemClass::read(const String& filePath) {
  return Bridge.waitForResponse(CMD_FILE_READ, (const uint8_t*)filePath.c_str(),
                                filePath.length());
}

int FileSystemClass::read(const String& filePath, char* buffer, size_t length) {
  return Bridge.waitForResponse(CMD_FILE_READ, (const uint8_t*)filePath.c_str(),
                                filePath.length(), buffer, length);
}

void FileSystemClass::remove(const String& filePath) {
  if (filePath.length() == 0) return;
  Bridge.sendFrame(CMD_FILE_REMOVE, (const uint8_t*)filePath.c_str(),
                   filePath.length());
}

// =================================================================================
// ProcessClass
// =================================================================================

ProcessClass::ProcessClass() {}

String ProcessClass::run(const String& command) {
  return Bridge.waitForResponse(
      CMD_PROCESS_RUN, (const uint8_t*)command.c_str(), command.length());
}

int ProcessClass::runAsynchronously(const String& command) {
  return Bridge.waitForResponseAsInt(
      CMD_PROCESS_RUN_ASYNC, (const uint8_t*)command.c_str(), command.length());
}

String ProcessClass::poll(int pid) {
  char pid_str[12];
  itoa(pid, pid_str, 10);
  return Bridge.waitForResponse(CMD_PROCESS_POLL, (const uint8_t*)pid_str,
                                strlen(pid_str));
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
    : _stream(stream),
      _parser(),
      _builder(),
      _command_handler(nullptr),
      _response_received(false),
      _waiting_for_cmd(0),
      _response_len(0) {}

void BridgeClass::begin() {
  // Set the baud rate for communication with the Linux processor.
  // This must match the rate used by the daemon.
  static_cast<HardwareSerial*>(&_stream)->begin(BRIDGE_BAUDRATE);
  _parser.reset();
  Console.begin();
}

void BridgeClass::onCommand(CommandHandler handler) { _command_handler = handler; }

void BridgeClass::onDigitalReadResponse(DigitalReadHandler handler) {
  _digital_read_handler = handler;
}

void BridgeClass::onAnalogReadResponse(AnalogReadHandler handler) {
  _analog_read_handler = handler;
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

/**
 * @brief The core router for all incoming RPC frames from the Linux side.
 * This function is called by process() whenever a complete, valid frame is
 * received. Its job is to figure out what the frame is for and what to do with
 * it.
 *
 * @param frame The validated RPC frame received from the parser.
 */
void BridgeClass::dispatch(const rpc::Frame& frame) {
  // --- Flujo Asíncrono (Callbacks) ---
  // Primero, comprobar si hay un callback registrado para esta respuesta.
  switch (frame.header.command_id) {
    case CMD_DIGITAL_READ_RESP:
      if (_digital_read_handler) {
        if (frame.header.payload_length >= 3) { // pin (1B) + value (2B)
          uint8_t pin = frame.payload[0];
          int value = (frame.payload[2] << 8) | frame.payload[1];
          _digital_read_handler(pin, value);
          return; // Callback manejado, no seguir
        }
      }
      break; // Si no hay handler, puede ser una llamada síncrona

    case CMD_ANALOG_READ_RESP:
      if (_analog_read_handler) {
        if (frame.header.payload_length >= 3) { // pin (1B) + value (2B)
          uint8_t pin = frame.payload[0];
          int value = (frame.payload[2] << 8) | frame.payload[1];
          _analog_read_handler(pin, value);
          return; // Callback manejado, no seguir
        }
      }
      break; // Si no hay handler, puede ser una llamada síncrona
  }

  // --- Flujo Síncrono (waitForResponse) ---
  // Si no se manejó con un callback, comprobar si es una respuesta síncrona esperada.
  if (_waiting_for_cmd != 0 && frame.header.command_id == _waiting_for_cmd) {
    uint16_t copy_len = min((uint16_t)sizeof(_response_payload) - 1,
                            frame.header.payload_length);
    memcpy(_response_payload, frame.payload, copy_len);
    _response_payload[copy_len] = '\0';  // Always null-terminate for safety.
    _response_len = copy_len;
    _response_received = true;  // Signal to waitForResponse() that we are done.
    return;
  }

  // Descartar respuestas inesperadas o tardías
  if (frame.header.command_id >= 0x80) {
    return;
  }

  // --- Comandos Generales (ACKs y otros) ---
  switch (frame.header.command_id) {
    case CMD_SET_PIN_MODE:
    case CMD_DIGITAL_WRITE:
    case CMD_ANALOG_WRITE:
    case CMD_CONSOLE_WRITE:
    case CMD_DATASTORE_PUT:
    case CMD_FILE_WRITE:
    case CMD_FILE_REMOVE:
    case CMD_PROCESS_KILL:
      sendFrame(STATUS_ACK, nullptr, 0);
      break;
  }

  // --- Comando Personalizado de Usuario ---
  if (_command_handler) {
    _command_handler(frame);
  }
}

void BridgeClass::sendFrame(uint16_t command_id, const uint8_t* payload,
                            uint16_t payload_len) {
  // Buffer for the raw frame (header + payload + CRC)
  uint8_t raw_frame_buf[rpc::MAX_RAW_FRAME_SIZE];

  // Build the raw frame into the buffer
  size_t raw_len =
      _builder.build(raw_frame_buf, command_id, payload, payload_len);
  if (raw_len == 0) {
    return;  // Failed to build frame
  }

  // Buffer for the COBS-encoded frame
  uint8_t cobs_buf[rpc::COBS_BUFFER_SIZE];

  // Encode the raw frame
  size_t cobs_len = cobs::encode(raw_frame_buf, raw_len, cobs_buf);

  // Write the encoded data and the trailing zero byte
  _stream.write(cobs_buf, cobs_len);
  _stream.write((uint8_t)0x00);
}

/**
 * @brief Sends a command and waits in a blocking loop for a specific response
 * frame. This is the core of synchronous request-response communication.
 *
 * @param command The command ID to send.
 * @param payload The payload to send with the command.
 * @param payload_len The length of the payload.
 * @param timeout The maximum time to wait for a response in milliseconds.
 * @return String The payload of the response frame, or an empty string on
 * timeout.
 */
String BridgeClass::waitForResponse(uint16_t command, const uint8_t* payload,
                                    uint16_t payload_len,
                                    unsigned long timeout) {
  // First, send the request frame.
  sendFrame(command, payload, payload_len);

  // Determine the expected response ID. By convention, response IDs are often
  // related to the request ID, but a switch statement provides an explicit and
  // safe mapping.
  uint16_t expected_response_cmd = 0;
  switch (command) {
    case CMD_DIGITAL_READ:
      expected_response_cmd = CMD_DIGITAL_READ_RESP;
      break;
    case CMD_ANALOG_READ:
      expected_response_cmd = CMD_ANALOG_READ_RESP;
      break;
    case CMD_DATASTORE_GET:
      expected_response_cmd = CMD_DATASTORE_GET_RESP;
      break;
    case CMD_MAILBOX_READ:
      expected_response_cmd = CMD_MAILBOX_READ_RESP;
      break;
    case CMD_MAILBOX_AVAILABLE:
      expected_response_cmd = CMD_MAILBOX_AVAILABLE_RESP;
      break;
    case CMD_FILE_READ:
      expected_response_cmd = CMD_FILE_READ_RESP;
      break;
    case CMD_PROCESS_RUN:
      expected_response_cmd = CMD_PROCESS_RUN_RESP;
      break;
    case CMD_PROCESS_RUN_ASYNC:
      expected_response_cmd = CMD_PROCESS_RUN_ASYNC_RESP;
      break;
    case CMD_PROCESS_POLL:
      expected_response_cmd = CMD_PROCESS_POLL_RESP;
      break;
    default:
      expected_response_cmd =
          0;  // Should not happen for a command that expects a response
  }
  _waiting_for_cmd = expected_response_cmd;

  // Reset the response state machine variables.
  _response_received = false;
  _response_len = 0;

  // Enter the blocking wait loop.
  unsigned long start = millis();
  while (millis() - start < timeout) {
    // It is crucial to keep processing incoming frames even while waiting.
    // The response we need will be parsed inside this process() call.
    process();
    if (_response_received) {
      _waiting_for_cmd = 0;  // Clear the state
      return String((char*)_response_payload);
    }
  }

  // If the loop finishes without _response_received being true, it was a
  // timeout.
  _waiting_for_cmd = 0;
  return String();  // Timeout
}

String BridgeClass::waitForResponse(uint16_t command, unsigned long timeout) {
  return waitForResponse(command, NULL, 0, timeout);
}

int BridgeClass::waitForResponseAsInt(uint16_t command, const uint8_t* payload,
                                      uint16_t payload_len,
                                      unsigned long timeout) {
  String response = waitForResponse(command, payload, payload_len, timeout);
  if (response.length() > 0) {
    return response.toInt();
  }
  return -1;  // Or some error indicator
}

int BridgeClass::waitForResponseAsInt(uint16_t command, unsigned long timeout) {
  return waitForResponseAsInt(command, NULL, 0, timeout);
}

int BridgeClass::waitForResponse(uint16_t command, const uint8_t* payload,
                                 uint16_t payload_len, char* buffer,
                                 size_t buffer_len, unsigned long timeout) {
  // This is the implementation for the character buffer version
  sendFrame(command, payload, payload_len);

  uint16_t expected_response_cmd = 0;
  switch (command) {
    case CMD_FILE_READ:
      expected_response_cmd = CMD_FILE_READ_RESP;
      break;
    // Add other cases if this function is used for more commands
    default:
      expected_response_cmd = 0;
  }
  _waiting_for_cmd = expected_response_cmd;

  _response_received = false;
  _response_len = 0;

  unsigned long start = millis();
  while (millis() - start < timeout) {
    process();
    if (_response_received) {
      _waiting_for_cmd = 0;  // Clear state
      size_t len_to_copy = min((size_t)_response_len, buffer_len - 1);
      memcpy(buffer, _response_payload, len_to_copy);
      buffer[len_to_copy] = '\0';  // Ensure null termination
      return _response_len;
    }
  }

  _waiting_for_cmd = 0;
  if (buffer_len > 0) {
    buffer[0] = '\0';  // Return empty buffer on timeout
  }
  return -1;  // Indicate timeout
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

// Non-blocking request methods
void BridgeClass::requestDigitalRead(uint8_t pin) {
  uint8_t payload[1] = {pin};
  sendFrame(CMD_DIGITAL_READ, payload, 1);
}

void BridgeClass::requestAnalogRead(uint8_t pin) {
  uint8_t payload[1] = {pin};
  sendFrame(CMD_ANALOG_READ, payload, 1);
}

// Deprecated blocking methods
int BridgeClass::digitalRead(uint8_t pin) {
  uint8_t payload[1] = {pin};
  return waitForResponseAsInt(CMD_DIGITAL_READ, payload, 1);
}

int BridgeClass::analogRead(uint8_t pin) {
  uint8_t payload[1] = {pin};
  return waitForResponseAsInt(CMD_ANALOG_READ, payload, 1);
}


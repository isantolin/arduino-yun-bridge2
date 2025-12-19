/*
 * This file is part of Arduino Yun Ecosystem v2.
 */
#include "Bridge.h"

#ifdef ARDUINO_ARCH_AVR
#include <avr/wdt.h>
#endif

#include <string.h>
#include <stdlib.h>
#include <stdint.h>
#include <Crypto.h>
#include <SHA256.h>

#include "arduino/StringUtils.h"
#include "protocol/crc.h"
#include "protocol/rpc_protocol.h"

using namespace rpc;

#ifndef BRIDGE_TEST_NO_GLOBALS
BridgeClass Bridge(Serial1);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;
#endif

#if BRIDGE_DEBUG_IO
template <typename ActionText>
static void bridge_debug_log_gpio(ActionText action, uint8_t pin, int value) {
  if (!kBridgeDebugIo) return;
  if (!Console) return;
  Console.print(F("[GPIO] "));
  Console.print(action);
  Console.print(F(" D"));
  Console.print(pin);
  Console.print(F(" = "));
  Console.println(value);
}
#endif

namespace {
constexpr size_t kHandshakeTagSize = RPC_HANDSHAKE_TAG_LENGTH;
static_assert(
  kHandshakeTagSize > 0,
  "RPC_HANDSHAKE_TAG_LENGTH must be greater than zero"
);
constexpr size_t kSha256DigestSize = 32;
constexpr char kSerialOverflowMessage[] PROGMEM = "serial_rx_overflow";

#if defined(ARDUINO_ARCH_AVR)
extern "C" char __heap_start;
extern "C" char* __brkval;
#endif

uint16_t getFreeMemory() {
#if defined(ARDUINO_ARCH_AVR)
  char stack_top;
  char* heap_end = __brkval ? __brkval : &__heap_start;
  intptr_t free_bytes = &stack_top - heap_end;
  if (free_bytes < 0) {
    free_bytes = 0;
  }
  if (static_cast<size_t>(free_bytes) > 0xFFFF) {
    free_bytes = 0xFFFF;
  }
  return static_cast<uint16_t>(free_bytes);
#else
  return 0;
#endif
}

}

BridgeClass::BridgeClass(HardwareSerial& serial)
    : _transport(serial, &serial),
      _shared_secret(nullptr),
      _shared_secret_len(0),
      _rx_frame{},
      _awaiting_ack(false),
      _last_command_id(0),
      _retry_count(0),
      _last_send_millis(0),
      _ack_timeout_ms(kAckTimeoutMs),
      _ack_retry_limit(kMaxAckRetries),
      _response_timeout_ms(RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS),
      _command_handler(nullptr),
      _digital_read_handler(nullptr),
      _analog_read_handler(nullptr),
      _get_free_memory_handler(nullptr),
      _status_handler(nullptr),
      _pending_tx_head(0),
      _pending_tx_count(0),
      _synchronized(false)
#if BRIDGE_DEBUG_FRAMES
      , _tx_debug{}
#endif
{
  for (auto& frame : _pending_tx_frames) {
    frame.command_id = 0;
    frame.payload_length = 0;
    memset(frame.payload.data(), 0, frame.payload.size());
  }
}

BridgeClass::BridgeClass(Stream& stream)
    : _transport(stream, nullptr),
      _shared_secret(nullptr),
      _shared_secret_len(0),
      _rx_frame{},
      _awaiting_ack(false),
      _last_command_id(0),
      _retry_count(0),
      _last_send_millis(0),
      _ack_timeout_ms(kAckTimeoutMs),
      _ack_retry_limit(kMaxAckRetries),
      _response_timeout_ms(RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS),
      _command_handler(nullptr),
      _digital_read_handler(nullptr),
      _analog_read_handler(nullptr),
      _get_free_memory_handler(nullptr),
      _status_handler(nullptr),
      _pending_tx_head(0),
      _pending_tx_count(0),
      _synchronized(false)
#if BRIDGE_DEBUG_FRAMES
      , _tx_debug{}
#endif
{
  for (auto& frame : _pending_tx_frames) {
    frame.command_id = 0;
    frame.payload_length = 0;
    memset(frame.payload.data(), 0, frame.payload.size());
  }
}

void BridgeClass::begin(
    unsigned long baudrate, const char* secret, size_t secret_len) {
  _transport.begin(baudrate);

  _shared_secret = reinterpret_cast<const uint8_t*>(secret);
  if (_shared_secret && secret_len > 0) {
    _shared_secret_len = secret_len;
  } else if (_shared_secret) {
    _shared_secret_len = strlen(secret);
  } else {
    _shared_secret_len = 0;
  }

  _awaiting_ack = false;
  _last_command_id = 0;
  _retry_count = 0;
  _last_send_millis = 0;
#if BRIDGE_DEBUG_FRAMES
  _tx_debug = {};
#endif

#ifndef BRIDGE_TEST_NO_GLOBALS
  while (!_synchronized) {
    process();
  }
#endif
}

void BridgeClass::_computeHandshakeTag(const uint8_t* nonce, size_t nonce_len, uint8_t* out_tag) {
  if (_shared_secret_len == 0 || nonce_len == 0 || !_shared_secret) {
    memset(out_tag, 0, kHandshakeTagSize);
    return;
  }

  SHA256 sha256;
  uint8_t digest[kSha256DigestSize];

  sha256.resetHMAC(_shared_secret, _shared_secret_len);
  sha256.update(nonce, nonce_len);
  sha256.finalizeHMAC(_shared_secret, _shared_secret_len, digest, kSha256DigestSize);

  memcpy(out_tag, digest, kHandshakeTagSize);
}

void BridgeClass::_applyTimingConfig(const uint8_t* payload, size_t length) {
  // Default values
  uint16_t ack_timeout_ms = kAckTimeoutMs;
  uint8_t retry_limit = kMaxAckRetries;
  uint32_t response_timeout_ms = RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS;

  if (payload != nullptr && length >= RPC_HANDSHAKE_CONFIG_SIZE) {
    const uint8_t* cursor = payload;
    ack_timeout_ms = rpc::read_u16_be(cursor);
    cursor += 2;
    retry_limit = *cursor++;
    response_timeout_ms = rpc::read_u32_be(cursor);
  }

  // Apply with validation
  _ack_timeout_ms = (ack_timeout_ms >= RPC_HANDSHAKE_ACK_TIMEOUT_MIN_MS &&
                     ack_timeout_ms <= RPC_HANDSHAKE_ACK_TIMEOUT_MAX_MS)
                        ? ack_timeout_ms
                        : kAckTimeoutMs;

  _ack_retry_limit = (retry_limit >= RPC_HANDSHAKE_RETRY_LIMIT_MIN &&
                      retry_limit <= RPC_HANDSHAKE_RETRY_LIMIT_MAX)
                         ? retry_limit
                         : kMaxAckRetries;

  _response_timeout_ms =
      (response_timeout_ms >= RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS &&
       response_timeout_ms <= RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS)
          ? response_timeout_ms
          : RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS;
}

void BridgeClass::onCommand(CommandHandler handler) { _command_handler = handler; }
void BridgeClass::onDigitalReadResponse(DigitalReadHandler handler) { _digital_read_handler = handler; }
void BridgeClass::onAnalogReadResponse(AnalogReadHandler handler) { _analog_read_handler = handler; }
void BridgeClass::onGetFreeMemoryResponse(GetFreeMemoryHandler handler) { _get_free_memory_handler = handler; }
void BridgeClass::onStatus(StatusHandler handler) { _status_handler = handler; }

void BridgeClass::process() {
#if defined(ARDUINO_ARCH_AVR)
  if (kBridgeEnableWatchdog) {
    wdt_reset();
  }
#endif

  // Handle incoming data via transport
  rpc::Frame frame;
  if (_transport.processInput(frame)) {
    dispatch(frame);
  } else {
    rpc::FrameParser::Error error = _transport.getLastError();
    if (error != rpc::FrameParser::Error::NONE) {
      switch (error) {
        case rpc::FrameParser::Error::CRC_MISMATCH:
          _emitStatus(rpc::StatusCode::STATUS_CRC_MISMATCH, (const char*)nullptr);
          break;
        case rpc::FrameParser::Error::MALFORMED:
          _emitStatus(rpc::StatusCode::STATUS_MALFORMED, (const char*)nullptr);
          break;
        case rpc::FrameParser::Error::OVERFLOW:
          _emitStatus(rpc::StatusCode::STATUS_MALFORMED, reinterpret_cast<const __FlashStringHelper*>(kSerialOverflowMessage));
          break;
        default:
          _emitStatus(rpc::StatusCode::STATUS_ERROR, (const char*)nullptr);
          break;
      }
      _transport.clearError();
      _transport.clearOverflow(); // Ensure overflow flag is also cleared if set
    }
  }

  _processAckTimeout();
  // Retry queued frames after transient send failures.
  _flushPendingTxQueue();
  // Also pump console to flush partial buffers
  Console.flush(); 
}

void BridgeClass::flushStream() {
  _transport.flush();
}

void BridgeClass::_handleSystemCommand(const rpc::Frame& frame) {
  const CommandId command = static_cast<CommandId>(frame.header.command_id);
  const size_t payload_length = frame.header.payload_length;
  const uint8_t* payload_data = frame.payload;

  switch (command) {
    case CommandId::CMD_GET_VERSION:
      if (payload_length == 0) {
        uint8_t version_payload[2] = {
            static_cast<uint8_t>(BridgeClass::kFirmwareVersionMajor),
            static_cast<uint8_t>(BridgeClass::kFirmwareVersionMinor)};
        (void)sendFrame(CommandId::CMD_GET_VERSION_RESP, version_payload, sizeof(version_payload));
      }
      break;
    case CommandId::CMD_GET_FREE_MEMORY:
      if (payload_length == 0) {
        uint16_t free_mem = getFreeMemory();
        uint8_t resp_payload[2];
        rpc::write_u16_be(resp_payload, free_mem);
        (void)sendFrame(CommandId::CMD_GET_FREE_MEMORY_RESP, resp_payload, 2);
      }
      break;
    case CommandId::CMD_GET_TX_DEBUG_SNAPSHOT:
      if (payload_length == 0) {
        uint8_t resp[9];
        resp[0] = _pending_tx_count;
        resp[1] = _awaiting_ack ? 1 : 0;
        resp[2] = _retry_count;
        rpc::write_u16_be(&resp[3], _last_command_id);
        rpc::write_u32_be(&resp[5], static_cast<uint32_t>(_last_send_millis));
        (void)sendFrame(CommandId::CMD_GET_TX_DEBUG_SNAPSHOT_RESP, resp, sizeof(resp));
      }
      break;
    case CommandId::CMD_SET_BAUDRATE:
      if (payload_length == 4) {
        uint32_t new_baud = rpc::read_u32_be(payload_data);
        // Send ACK at current baudrate
        (void)sendFrame(CommandId::CMD_SET_BAUDRATE_RESP, nullptr, 0);
        _transport.flush();
        delay(50); // Give time for the ACK to leave the UART buffer
        _transport.setBaudrate(new_baud);
      }
      break;
    case CommandId::CMD_LINK_SYNC:
      {
        const size_t nonce_length = payload_length;
        if (nonce_length != RPC_HANDSHAKE_NONCE_LENGTH) break;
        
        _resetLinkState();
        Console.begin();
        const bool has_secret = (_shared_secret_len > 0);
        const size_t response_length = static_cast<size_t>(nonce_length) + (has_secret ? kHandshakeTagSize : 0);
        
        if (response_length > rpc::MAX_PAYLOAD_SIZE) {
          (void)sendFrame(StatusCode::STATUS_MALFORMED);
          break;
        }

        uint8_t* response = _scratch_payload;
        if (payload_data) {
          memcpy(response, payload_data, nonce_length);
          if (has_secret) {
            uint8_t tag[kHandshakeTagSize];
            _computeHandshakeTag(payload_data, nonce_length, tag);
            memcpy(response + nonce_length, tag, kHandshakeTagSize);
          }
          (void)sendFrame(CommandId::CMD_LINK_SYNC_RESP, response, response_length);
          _synchronized = true;
        }
      }
      break;
    case CommandId::CMD_LINK_RESET:
      if (payload_length == 0 || payload_length == RPC_HANDSHAKE_CONFIG_SIZE) {
        _resetLinkState();
        _applyTimingConfig(payload_data, payload_length);
        Console.begin();
        (void)sendFrame(CommandId::CMD_LINK_RESET_RESP);
      }
      break;
    default:
      break;
  }
}

void BridgeClass::_handleGpioCommand(const rpc::Frame& frame) {
  const CommandId command = static_cast<CommandId>(frame.header.command_id);
  const size_t payload_length = frame.header.payload_length;
  const uint8_t* payload_data = frame.payload;

  if (!payload_data) return;

  switch (command) {
    case CommandId::CMD_SET_PIN_MODE:
      if (payload_length == 2) {
        uint8_t pin = payload_data[0];
        uint8_t mode = payload_data[1];
        ::pinMode(pin, mode);
        #if BRIDGE_DEBUG_IO
        if (kBridgeDebugIo) bridge_debug_log_gpio(F("pinMode"), pin, mode);
        #endif
      }
      break;
    case CommandId::CMD_DIGITAL_WRITE:
      if (payload_length == 2) {
        uint8_t pin = payload_data[0];
        uint8_t value = payload_data[1] ? HIGH : LOW;
        ::digitalWrite(pin, value);
        #if BRIDGE_DEBUG_IO
        if (kBridgeDebugIo) bridge_debug_log_gpio(F("digitalWrite"), pin, value == HIGH ? 1 : 0);
        #endif
      }
      break;
    case CommandId::CMD_ANALOG_WRITE:
      if (payload_length == 2) {
        ::analogWrite(payload_data[0], static_cast<int>(payload_data[1]));
      }
      break;
    case CommandId::CMD_DIGITAL_READ:
      if (payload_length == 1) {
        uint8_t pin = payload_data[0];
        int value = ::digitalRead(pin);
        #if BRIDGE_DEBUG_IO
        if (kBridgeDebugIo) bridge_debug_log_gpio(F("digitalRead"), pin, value);
        #endif
        uint8_t resp_payload = static_cast<uint8_t>(value & 0xFF);
        (void)sendFrame(CommandId::CMD_DIGITAL_READ_RESP, &resp_payload, 1);
      }
      break;
    case CommandId::CMD_ANALOG_READ:
      if (payload_length == 1) {
        uint8_t pin = payload_data[0];
        int value = ::analogRead(pin);
        #if BRIDGE_DEBUG_IO
        if (kBridgeDebugIo) bridge_debug_log_gpio(F("analogRead"), pin, value);
        #endif
        uint8_t resp_payload[2];
        rpc::write_u16_be(resp_payload, static_cast<uint16_t>(value & 0xFFFF));
        (void)sendFrame(CommandId::CMD_ANALOG_READ_RESP, resp_payload, sizeof(resp_payload));
      }
      break;
    default:
      break;
  }
}

void BridgeClass::_handleConsoleCommand(const rpc::Frame& frame) {
  if (static_cast<CommandId>(frame.header.command_id) == CommandId::CMD_CONSOLE_WRITE) {
    Console._push(frame.payload, frame.header.payload_length);
  }
}

void BridgeClass::dispatch(const rpc::Frame& frame) {
  const uint16_t raw_command = frame.header.command_id;
  const CommandId command = static_cast<CommandId>(raw_command);
  
  // 1. Handle Responses (Linux -> MCU)
  DataStore.handleResponse(frame);
  Mailbox.handleResponse(frame);
  FileSystem.handleResponse(frame);
  Process.handleResponse(frame);
  
  // 2. Handle Commands (Linux -> MCU)
  bool command_processed_internally = false;
  bool requires_ack = false;

  switch (command) {
    case CommandId::CMD_GET_VERSION:
    case CommandId::CMD_GET_FREE_MEMORY:
    case CommandId::CMD_GET_TX_DEBUG_SNAPSHOT:
    case CommandId::CMD_SET_BAUDRATE:
    case CommandId::CMD_LINK_RESET:
      if (frame.header.payload_length > 0 && command != CommandId::CMD_SET_BAUDRATE) {
         // Collision with STATUS codes. If payload exists, treat as Status.
         command_processed_internally = false;
      } else {
         _handleSystemCommand(frame);
         command_processed_internally = true;
         requires_ack = (command == CommandId::CMD_LINK_RESET);
      }
      break;
    case CommandId::CMD_LINK_SYNC:
      // Collision with STATUS_CMD_UNKNOWN (2).
      // CMD_LINK_SYNC has payload of size RPC_HANDSHAKE_NONCE_LENGTH (16).
      // STATUS_CMD_UNKNOWN usually has payload of size 2 (command ID).
      if (frame.header.payload_length == RPC_HANDSHAKE_NONCE_LENGTH) {
          _handleSystemCommand(frame);
          command_processed_internally = true;
          requires_ack = true;
      } else {
          command_processed_internally = false;
      }
      break;
    case CommandId::CMD_SET_PIN_MODE:
    case CommandId::CMD_DIGITAL_WRITE:
    case CommandId::CMD_ANALOG_WRITE:
    case CommandId::CMD_DIGITAL_READ:
    case CommandId::CMD_ANALOG_READ:
      _handleGpioCommand(frame);
      command_processed_internally = true;
      requires_ack = (command != CommandId::CMD_DIGITAL_READ && command != CommandId::CMD_ANALOG_READ);
      break;
    case CommandId::CMD_CONSOLE_WRITE:
      _handleConsoleCommand(frame);
      command_processed_internally = true;
      requires_ack = true;
      break;
    case CommandId::CMD_MAILBOX_PUSH:
    case CommandId::CMD_MAILBOX_AVAILABLE:
      Mailbox.handleResponse(frame); // Actually handleCommand, but method name is handleResponse for now
      command_processed_internally = true;
      requires_ack = true;
      break;
    case CommandId::CMD_FILE_WRITE:
      FileSystem.handleResponse(frame); // Actually handleCommand
      command_processed_internally = true;
      requires_ack = true;
      break;
    // Explicitly mark responses as processed to avoid STATUS_CMD_UNKNOWN
    case CommandId::CMD_DATASTORE_GET_RESP:
    case CommandId::CMD_MAILBOX_READ_RESP:
    case CommandId::CMD_FILE_READ_RESP:
    case CommandId::CMD_PROCESS_RUN_RESP:
    case CommandId::CMD_PROCESS_RUN_ASYNC_RESP:
    case CommandId::CMD_PROCESS_POLL_RESP:
    case CommandId::CMD_LINK_SYNC_RESP:
      command_processed_internally = true;
      break;
    default:
      break;
  }

  if (requires_ack) {
    uint8_t ack_payload[2];
    rpc::write_u16_be(ack_payload, raw_command);
    (void)sendFrame(StatusCode::STATUS_ACK, ack_payload, sizeof(ack_payload));
  }

  if (!command_processed_internally &&
      raw_command <= rpc::to_underlying(StatusCode::STATUS_ACK)) {
    const StatusCode status = static_cast<StatusCode>(raw_command);
    const size_t payload_length = frame.header.payload_length;
    const uint8_t* payload_data = frame.payload;
    switch (status) {
      case StatusCode::STATUS_ACK: {
        uint16_t ack_id = 0xFFFF;
        if (payload_length >= 2 && payload_data) {
          ack_id = rpc::read_u16_be(payload_data);
        }
        _handleAck(ack_id);
        if (_status_handler) {
          _status_handler(status, payload_data, static_cast<uint16_t>(payload_length));
        }
        return;
      }
      case StatusCode::STATUS_MALFORMED: {
        uint16_t malformed_id = 0xFFFF;
        if (payload_length >= 2 && payload_data) {
          malformed_id = rpc::read_u16_be(payload_data);
        }
        _handleMalformed(malformed_id);
        if (_status_handler) {
          _status_handler(status, payload_data, static_cast<uint16_t>(payload_length));
        }
        return;
      }
      case StatusCode::STATUS_ERROR:
      case StatusCode::STATUS_CMD_UNKNOWN:
      case StatusCode::STATUS_CRC_MISMATCH:
      case StatusCode::STATUS_TIMEOUT:
      case StatusCode::STATUS_NOT_IMPLEMENTED:
      case StatusCode::STATUS_OVERFLOW:
      case StatusCode::STATUS_OK:
        if (_status_handler) {
          _status_handler(status, payload_data, static_cast<uint16_t>(payload_length));
        }
        return;
    }
  }

  if (!command_processed_internally && _command_handler) {
    _command_handler(frame);
  } else if (!command_processed_internally) {
    if (raw_command > rpc::to_underlying(StatusCode::STATUS_ACK)) {
      (void)sendFrame(StatusCode::STATUS_CMD_UNKNOWN);
    }
  }
}

void BridgeClass::_emitStatus(StatusCode status_code, const char* message) {
  const uint8_t* payload = nullptr;
  uint16_t length = 0;
  if (message && *message) {
    const auto info = measure_bounded_cstring(message, rpc::MAX_PAYLOAD_SIZE);
    length = static_cast<uint16_t>(info.length);
    payload = reinterpret_cast<const uint8_t*>(message);
  }
  (void)sendFrame(status_code, payload, length);
  if (_status_handler) {
    _status_handler(status_code, payload, length);
  }
}

void BridgeClass::_emitStatus(StatusCode status_code, const __FlashStringHelper* message) {
  const uint8_t* payload = nullptr;
  uint16_t length = 0;
  if (message) {
    const char* p = reinterpret_cast<const char*>(message);
    size_t i = 0;
    while (i < rpc::MAX_PAYLOAD_SIZE) {
      uint8_t c = pgm_read_byte(p + i);
      if (c == 0) break;
      _scratch_payload[i] = c;
      i++;
    }
    length = static_cast<uint16_t>(i);
    payload = _scratch_payload;
  }
  (void)sendFrame(status_code, payload, length);
  if (_status_handler) {
    _status_handler(status_code, payload, length);
  }
}

bool BridgeClass::sendFrame(CommandId command_id, const uint8_t* payload, size_t length) {
  return _sendFrame(rpc::to_underlying(command_id), payload, length);
}

bool BridgeClass::sendFrame(StatusCode status_code, const uint8_t* payload, size_t length) {
  return _sendFrame(rpc::to_underlying(status_code), payload, length);
}

bool BridgeClass::_sendFrame(uint16_t command_id, const uint8_t* payload, size_t length) {
  if (!_synchronized) {
    // Allow handshake commands (0-7) and specific responses
    bool allowed = (command_id <= 7) ||
                   (command_id == rpc::to_underlying(CommandId::CMD_GET_VERSION_RESP)) ||
                   (command_id == rpc::to_underlying(CommandId::CMD_LINK_SYNC_RESP)) ||
                   (command_id == rpc::to_underlying(CommandId::CMD_LINK_RESET_RESP));
    if (!allowed) {
      return false;
    }
  }

  if (!_requiresAck(command_id)) {
    return _sendFrameImmediate(command_id, payload, length);
  }

  if (_awaiting_ack) {
    if (_enqueuePendingTx(command_id, payload, length)) {
      return true;
    }
    _processAckTimeout();
    if (!_awaiting_ack && _enqueuePendingTx(command_id, payload, length)) {
      return true;
    }
    return false;
  }

  return _sendFrameImmediate(command_id, payload, length);
}

bool BridgeClass::_sendFrameImmediate(uint16_t command_id,
                                      const uint8_t* payload, size_t length) {
  bool success = _transport.sendFrame(command_id, payload, length);

  if (success && _requiresAck(command_id)) {
    _awaiting_ack = true;
    _retry_count = 0;
    _last_send_millis = millis();
    _last_command_id = command_id;
  }

  return success;
}



#if BRIDGE_DEBUG_FRAMES
BridgeClass::FrameDebugSnapshot BridgeClass::getTxDebugSnapshot() const {
  return _tx_debug;
}

void BridgeClass::resetTxDebugStats() { _tx_debug = {}; }
#endif

bool BridgeClass::_requiresAck(uint16_t command_id) const {
  return command_id > rpc::to_underlying(StatusCode::STATUS_ACK);
}

void BridgeClass::_clearAckState() {
  _awaiting_ack = false;
  _retry_count = 0;
}

void BridgeClass::_handleAck(uint16_t command_id) {
  if (!_awaiting_ack) {
    return;
  }
  if (command_id == 0xFFFF || command_id == _last_command_id) {
    _clearAckState();
    _flushPendingTxQueue();
  }
}

void BridgeClass::_handleMalformed(uint16_t command_id) {
  if (command_id == 0xFFFF || command_id == _last_command_id) {
    _retransmitLastFrame();
  }
}

void BridgeClass::_retransmitLastFrame() {
  if (!_awaiting_ack) {
    return;
  }
  
  if (_transport.retransmitLastFrame()) {
    _retry_count++;
    _last_send_millis = millis();
  }
}

void BridgeClass::_processAckTimeout() {
  if (!_awaiting_ack) {
    return;
  }
  unsigned long now = millis();
  if ((now - _last_send_millis) < _ack_timeout_ms) {
    return;
  }
  if (_retry_count >= _ack_retry_limit) {
    _awaiting_ack = false;
    if (_status_handler) {
      _status_handler(StatusCode::STATUS_TIMEOUT, nullptr, 0);
    }
    _flushPendingTxQueue();
    return;
  }
  _retransmitLastFrame();
}

void BridgeClass::_resetLinkState() {
  _synchronized = false;
  _clearAckState();
  _clearPendingTxQueue();
  _transport.reset();
  // _flow_paused = false; // Handled by transport.reset()
}

void BridgeClass::_flushPendingTxQueue() {
  if (_awaiting_ack || _pending_tx_count == 0) {
    return;
  }
  PendingTxFrame frame;
  if (!_dequeuePendingTx(frame)) {
    return;
  }
    if (!_sendFrameImmediate(
      frame.command_id,
      frame.payload.data(), frame.payload_length)) {
    uint8_t previous_head =
        (_pending_tx_head + kMaxPendingTxFrames - 1) % kMaxPendingTxFrames;
    _pending_tx_head = previous_head;
    _pending_tx_frames[_pending_tx_head] = frame;
    _pending_tx_count++;
  }
}

void BridgeClass::_clearPendingTxQueue() {
  _pending_tx_head = 0;
  _pending_tx_count = 0;
}

bool BridgeClass::_enqueuePendingTx(uint16_t command_id, const uint8_t* payload, size_t length) {
  if (_pending_tx_count >= kMaxPendingTxFrames) {
    return false;
  }
  size_t payload_len = length;
  if (payload_len > rpc::MAX_PAYLOAD_SIZE) {
    return false;
  }
  uint8_t tail = (_pending_tx_head + _pending_tx_count) % kMaxPendingTxFrames;
  _pending_tx_frames[tail].command_id = command_id;
  _pending_tx_frames[tail].payload_length =
      static_cast<uint16_t>(payload_len);
  if (payload_len > 0) {
    memcpy(_pending_tx_frames[tail].payload.data(), payload, payload_len);
  }
  _pending_tx_count++;
  return true;
}

bool BridgeClass::_dequeuePendingTx(PendingTxFrame& frame) {
  if (_pending_tx_count == 0) {
    return false;
  }
  frame = _pending_tx_frames[_pending_tx_head];
  _pending_tx_head = (_pending_tx_head + 1) % kMaxPendingTxFrames;
  _pending_tx_count--;
  return true;
}

void BridgeClass::pinMode(uint8_t pin, uint8_t mode) {
  ::pinMode(pin, mode);
}

void BridgeClass::digitalWrite(uint8_t pin, uint8_t value) {
  ::digitalWrite(pin, value);
}

void BridgeClass::analogWrite(uint8_t pin, int value) {
  uint8_t val_u8 = constrain(value, 0, 255);
  ::analogWrite(pin, static_cast<int>(val_u8));
}

void BridgeClass::requestDigitalRead(uint8_t pin) {
  // Deprecated: MCU no longer initiates pin reads.
  // No-op.
  (void)pin;
}

void BridgeClass::requestAnalogRead(uint8_t pin) {
  // Deprecated: MCU no longer initiates pin reads.
  // No-op.
  (void)pin;
}

void BridgeClass::requestGetFreeMemory() {
  (void)sendFrame(CommandId::CMD_GET_FREE_MEMORY);
}

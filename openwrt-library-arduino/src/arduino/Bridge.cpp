/*
 * This file is part of Arduino Yun Ecosystem v2.
 */
#include "Bridge.h"

#if defined(ARDUINO_ARCH_AVR)
#include <avr/pgmspace.h>
#include <avr/wdt.h>
#endif

#include <string.h> 
#include <stdlib.h> 
#include <stdint.h>
#include <Crypto.h>
#include <SHA256.h>

#include "protocol/crc.h"
#include "protocol/rpc_protocol.h"

using namespace rpc;

#ifndef BRIDGE_ENABLE_WATCHDOG
#define BRIDGE_ENABLE_WATCHDOG 1
#endif

#if defined(ARDUINO_ARCH_AVR) && BRIDGE_ENABLE_WATCHDOG
#ifndef BRIDGE_WATCHDOG_TIMEOUT
#define BRIDGE_WATCHDOG_TIMEOUT WDTO_2S
#endif
#endif

BridgeClass Bridge(Serial1);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

#if defined(ARDUINO_ARCH_AVR)
extern "C" char __heap_start;
extern "C" char* __brkval;
#endif

#if BRIDGE_DEBUG_IO
static void bridge_debug_log_gpio(const char* action, uint8_t pin, int value) {
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
constexpr size_t kFileReadLengthPrefix = 1;
constexpr size_t kMaxFilePathLength = 255;

#if defined(ARDUINO_ARCH_AVR)
uint16_t calculateFreeMemoryBytes() {
  char stack_top;
  char* heap_end = __brkval ? __brkval : &__heap_start;
  intptr_t free_bytes = &stack_top - heap_end;
  if (free_bytes < 0) {
    free_bytes = 0;
  }
  if (free_bytes > 0xFFFF) {
    free_bytes = 0xFFFF;
  }
  return static_cast<uint16_t>(free_bytes);
}
#else
uint16_t calculateFreeMemoryBytes() {
  return 0;
}
#endif
}

BridgeClass::BridgeClass(HardwareSerial& serial)
    : BridgeClass(static_cast<Stream&>(serial)) {
  _hardware_serial = &serial;
}

BridgeClass::BridgeClass(Stream& stream)
    : _stream(stream),
      _hardware_serial(nullptr),
      _shared_secret(nullptr),
      _shared_secret_len(0),
      _parser(),
      _builder(),
      _rx_frame{},
      _command_handler(nullptr),
      _datastore_get_handler(nullptr),
      _mailbox_handler(nullptr),
      _mailbox_available_handler(nullptr),
      _digital_read_handler(nullptr),
      _analog_read_handler(nullptr),
      _process_run_handler(nullptr),
      _process_poll_handler(nullptr),
      _process_run_async_handler(nullptr),
      _file_system_read_handler(nullptr),
      _get_free_memory_handler(nullptr),
      _status_handler(nullptr),
      _pending_datastore_head(0),
      _pending_datastore_count(0),
      _pending_process_poll_head(0),
      _pending_process_poll_count(0),
      _pending_tx_head(0),
      _pending_tx_count(0)
#if BRIDGE_DEBUG_FRAMES
      , _tx_debug{}
#endif
      , _awaiting_ack(false),
      _last_command_id(0),
      _last_cobs_frame{},
      _last_cobs_length(0),
      _retry_count(0),
      _last_send_millis(0),
      _ack_timeout_ms(kAckTimeoutMs),
      _ack_retry_limit(kMaxAckRetries),
      _response_timeout_ms(RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS) {
  memset(_pending_datastore_keys, 0, sizeof(_pending_datastore_keys));
  memset(
      _pending_datastore_key_lengths,
      0,
      sizeof(_pending_datastore_key_lengths));
  memset(_pending_process_pids, 0, sizeof(_pending_process_pids));
  memset(_pending_tx_frames, 0, sizeof(_pending_tx_frames));
}

void BridgeClass::begin(
    unsigned long baudrate, const char* secret, size_t secret_len) {
  if (_hardware_serial != nullptr) {
    _hardware_serial->begin(baudrate);
  }

  _shared_secret = reinterpret_cast<const uint8_t*>(secret);
  if (_shared_secret && secret_len > 0) {
    _shared_secret_len = secret_len;
  } else if (_shared_secret) {
    _shared_secret_len = strlen(secret);
  } else {
    _shared_secret_len = 0;
  }

  _resetLinkState();
  _clearPendingTxQueue();

  _pending_datastore_head = 0;
  _pending_datastore_count = 0;
  memset(_pending_datastore_keys, 0, sizeof(_pending_datastore_keys));
  memset(
      _pending_datastore_key_lengths,
      0,
      sizeof(_pending_datastore_key_lengths));

  _pending_process_poll_head = 0;
  _pending_process_poll_count = 0;
  memset(_pending_process_pids, 0, sizeof(_pending_process_pids));

  _awaiting_ack = false;
  _last_command_id = 0;
  _last_cobs_length = 0;
  _retry_count = 0;
  _last_send_millis = 0;
#if BRIDGE_DEBUG_FRAMES
  _tx_debug = {};
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

void BridgeClass::_applyTimingConfig(const uint8_t* payload, uint16_t length) {
  if (payload == nullptr || length < RPC_HANDSHAKE_CONFIG_SIZE) {
    _ack_timeout_ms = kAckTimeoutMs;
    _ack_retry_limit = kMaxAckRetries;
    _response_timeout_ms = RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS;
    return;
  }

  const uint8_t* cursor = payload;
  uint16_t ack_timeout_ms = rpc::read_u16_be(cursor);
  cursor += 2;
  uint8_t retry_limit = *cursor++;
  uint32_t response_timeout_ms = rpc::read_u32_be(cursor);

  if (
      ack_timeout_ms >= RPC_HANDSHAKE_ACK_TIMEOUT_MIN_MS &&
      ack_timeout_ms <= RPC_HANDSHAKE_ACK_TIMEOUT_MAX_MS) {
    _ack_timeout_ms = ack_timeout_ms;
  } else {
    _ack_timeout_ms = kAckTimeoutMs;
  }

  if (
      retry_limit >= RPC_HANDSHAKE_RETRY_LIMIT_MIN &&
      retry_limit <= RPC_HANDSHAKE_RETRY_LIMIT_MAX) {
    _ack_retry_limit = retry_limit;
  } else {
    _ack_retry_limit = kMaxAckRetries;
  }

  if (
      response_timeout_ms >= RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS &&
      response_timeout_ms <= RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS) {
    _response_timeout_ms = response_timeout_ms;
  } else {
    _response_timeout_ms = RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS;
  }
}

void BridgeClass::onMailboxMessage(MailboxHandler handler) {
  _mailbox_handler = handler;
}

void BridgeClass::onMailboxAvailableResponse(MailboxAvailableHandler handler) {
  _mailbox_available_handler = handler;
}
void BridgeClass::onCommand(CommandHandler handler) { _command_handler = handler; }
void BridgeClass::onDataStoreGetResponse(DataStoreGetHandler handler) { _datastore_get_handler = handler; }
void BridgeClass::onDigitalReadResponse(DigitalReadHandler handler) { _digital_read_handler = handler; }
void BridgeClass::onAnalogReadResponse(AnalogReadHandler handler) { _analog_read_handler = handler; }
void BridgeClass::onProcessRunResponse(ProcessRunHandler handler) { _process_run_handler = handler; }
void BridgeClass::onProcessPollResponse(ProcessPollHandler handler) { _process_poll_handler = handler; }
void BridgeClass::onProcessRunAsyncResponse(ProcessRunAsyncHandler handler) { _process_run_async_handler = handler; }
void BridgeClass::onFileSystemReadResponse(FileSystemReadHandler handler) { _file_system_read_handler = handler; }
void BridgeClass::onGetFreeMemoryResponse(GetFreeMemoryHandler handler) { _get_free_memory_handler = handler; }
void BridgeClass::onStatus(StatusHandler handler) { _status_handler = handler; }

void BridgeClass::process() {
#if defined(ARDUINO_ARCH_AVR) && BRIDGE_ENABLE_WATCHDOG
  wdt_reset();
#endif
  while (_stream.available()) {
    int byte_read = _stream.read(); // Use int to check -1
    if (byte_read >= 0) {
      uint8_t byte = static_cast<uint8_t>(byte_read);
      if (_parser.consume(byte, _rx_frame)) {
        dispatch(_rx_frame); 
      }
    }
  }
  _processAckTimeout();
}

void BridgeClass::flushStream() {
  if (_hardware_serial != nullptr) {
    _hardware_serial->flush();
    return;
  }
  _stream.flush();
}

void BridgeClass::dispatch(const rpc::Frame& frame) {
  switch (frame.header.command_id) {
    case CMD_DIGITAL_READ_RESP:
      if (_digital_read_handler && frame.header.payload_length == 1) {
        int value = frame.payload[0];
        _digital_read_handler(value);
      }
      return;
    case CMD_ANALOG_READ_RESP:
      if (_analog_read_handler && frame.header.payload_length == 2) {
        int value = (int)rpc::read_u16_be(frame.payload);
        _analog_read_handler(value);
      }
      return;
    case CMD_DATASTORE_GET_RESP:
      if (frame.header.payload_length >= 1) {
        uint8_t value_len = frame.payload[0];
        if (frame.header.payload_length >=
            static_cast<uint16_t>(1 + value_len)) {
          const uint8_t* value_ptr = frame.payload + 1;
          const char* key = _popPendingDatastoreKey();
          if (_datastore_get_handler) {
            _datastore_get_handler(key, value_ptr, value_len);
          }
        }
      }
      return;
    case CMD_MAILBOX_READ_RESP:
      if (_mailbox_handler && frame.header.payload_length >= 2) {
        uint16_t message_len = rpc::read_u16_be(frame.payload);
        if (frame.header.payload_length >=
            static_cast<uint16_t>(2 + message_len)) {
          _mailbox_handler(frame.payload + 2, static_cast<size_t>(message_len));
        }
      }
      return;
    case CMD_MAILBOX_AVAILABLE_RESP:
      if (_mailbox_available_handler && frame.header.payload_length == 1) {
        uint8_t count = frame.payload[0];
        _mailbox_available_handler(count);
      }
      return;
    case CMD_PROCESS_RUN_RESP:
      if (_process_run_handler && frame.header.payload_length >= 5) {
        const uint8_t* cursor = frame.payload;
        uint8_t status = *cursor++;
        uint16_t stdout_len = rpc::read_u16_be(cursor);
        cursor += 2;
        if (frame.header.payload_length <
            static_cast<uint16_t>(5 + stdout_len)) {
          return;
        }
        const uint8_t* stdout_ptr = cursor;
        cursor += stdout_len;
        uint16_t stderr_len = rpc::read_u16_be(cursor);
        cursor += 2;
        if (frame.header.payload_length < static_cast<uint16_t>(
                5 + stdout_len + stderr_len)) {
          return;
        }
        const uint8_t* stderr_ptr = cursor;
        _process_run_handler(
            status, stdout_ptr, stdout_len, stderr_ptr, stderr_len);
      }
      return;
    case CMD_PROCESS_POLL_RESP:
      if (frame.header.payload_length >= 6) {
        uint16_t pid = _popPendingProcessPid();
        const uint8_t* p = frame.payload;
        uint8_t status = *p++;
        uint8_t exit_code = *p++;
        uint16_t stdout_len = rpc::read_u16_be(p);
        p += 2;
        uint16_t stderr_len = rpc::read_u16_be(p);
        p += 2;
        if (frame.header.payload_length >=
            (6 + stdout_len + stderr_len)) {
          const uint8_t* stdout_data = p;
          const uint8_t* stderr_data = p + stdout_len;
          if (_process_poll_handler) {
            _process_poll_handler(
                status,
                exit_code,
                stdout_data,
                stdout_len,
                stderr_data,
                stderr_len);
          }
          if (
              pid != 0xFFFF && status == STATUS_OK
              && (stdout_len > 0 || stderr_len > 0)) {
            requestProcessPoll((int)pid);
          }
        }
      }
      return;
    case CMD_PROCESS_RUN_ASYNC_RESP:
      if (_process_run_async_handler && frame.header.payload_length == 2) {
        uint16_t pid = rpc::read_u16_be(frame.payload);
        _process_run_async_handler((int)pid);
      }
      return;
    case CMD_FILE_READ_RESP:
      if (_file_system_read_handler && frame.header.payload_length >= 2) {
        uint16_t data_len = rpc::read_u16_be(frame.payload);
        if (frame.header.payload_length >=
            static_cast<uint16_t>(2 + data_len)) {
          _file_system_read_handler(frame.payload + 2, data_len);
        }
      }
      return;
    case CMD_GET_FREE_MEMORY_RESP:
      if (_get_free_memory_handler && frame.header.payload_length >= 2) {
        uint16_t free_mem = rpc::read_u16_be(frame.payload);
        _get_free_memory_handler(free_mem);
      }
      return;
    case STATUS_ACK: {
      uint16_t ack_id = 0xFFFF;
      if (frame.header.payload_length >= 2) {
        ack_id = rpc::read_u16_be(frame.payload);
      }
      _handleAck(ack_id);
      if (_status_handler) {
        _status_handler(
            (uint8_t)frame.header.command_id,
            frame.payload,
            frame.header.payload_length);
      }
      return;
    }
    default:
      break;
  }

  bool command_processed_internally = false;
  bool requires_ack = false;

  switch (frame.header.command_id) {
    case CMD_GET_VERSION:
      {
        if (frame.header.payload_length != 0) {
          break;
        }
        uint8_t version_payload[2] = {
            (uint8_t)BRIDGE_FIRMWARE_VERSION_MAJOR,
            (uint8_t)BRIDGE_FIRMWARE_VERSION_MINOR};
        sendFrame(CMD_GET_VERSION_RESP, version_payload, sizeof(version_payload));
        command_processed_internally = true;
      }
      break;
    case CMD_GET_FREE_MEMORY:
      {
        if (frame.header.payload_length != 0) {
          break;
        }
        uint16_t free_mem = calculateFreeMemoryBytes();
        uint8_t resp_payload[2];
        resp_payload[0] = (free_mem >> 8) & 0xFF;
        resp_payload[1] = free_mem & 0xFF;
        sendFrame(CMD_GET_FREE_MEMORY_RESP, resp_payload, 2);
        command_processed_internally = true;
      }
      break;

    case CMD_LINK_SYNC:
      {
        const uint16_t nonce_length = frame.header.payload_length;
        if (nonce_length != RPC_HANDSHAKE_NONCE_LENGTH) {
          break;
        }
        _resetLinkState();
        Console.begin();
        const bool has_secret = (_shared_secret_len > 0);
        const size_t response_length =
            static_cast<size_t>(nonce_length) +
            (has_secret ? kHandshakeTagSize : 0);
        if (response_length > rpc::MAX_PAYLOAD_SIZE) {
          sendFrame(STATUS_MALFORMED, nullptr, 0);
          command_processed_internally = true;
          requires_ack = false;
          break;
        }

        uint8_t* response = _scratch_payload;
        memcpy(response, frame.payload, nonce_length);
        if (has_secret) {
          uint8_t tag[kHandshakeTagSize];
          _computeHandshakeTag(frame.payload, nonce_length, tag);
          memcpy(&response[nonce_length], tag, kHandshakeTagSize);
        }

        sendFrame(
            CMD_LINK_SYNC_RESP,
            response,
            static_cast<uint16_t>(response_length));
        command_processed_internally = true;
        requires_ack = true;
      }
      break;
    case CMD_LINK_RESET:
      {
        if (
            frame.header.payload_length != 0 &&
            frame.header.payload_length != RPC_HANDSHAKE_CONFIG_SIZE) {
          break;
        }
        _resetLinkState();
        _applyTimingConfig(frame.payload, frame.header.payload_length);
        Console.begin();
        sendFrame(CMD_LINK_RESET_RESP, nullptr, 0);
        command_processed_internally = true;
        requires_ack = true;
      }
      break;

    case CMD_SET_PIN_MODE:
      if (frame.header.payload_length == 2) {
      uint8_t pin = frame.payload[0];
      uint8_t mode = frame.payload[1];
      ::pinMode(pin, mode);
    #if BRIDGE_DEBUG_IO
      bridge_debug_log_gpio("pinMode", pin, mode);
    #endif
        command_processed_internally = true;
        requires_ack = true;
      }
      break;
    case CMD_DIGITAL_WRITE:
      if (frame.header.payload_length == 2) {
      uint8_t pin = frame.payload[0];
      uint8_t value = frame.payload[1] ? HIGH : LOW;
      ::digitalWrite(pin, value);
    #if BRIDGE_DEBUG_IO
      bridge_debug_log_gpio("digitalWrite", pin, value == HIGH ? 1 : 0);
    #endif
        command_processed_internally = true;
        requires_ack = true;
      }
      break;
    case CMD_ANALOG_WRITE:
      if (frame.header.payload_length == 2) {
        ::analogWrite(frame.payload[0], (int)frame.payload[1]);
        command_processed_internally = true;
        requires_ack = true;
      }
      break;
    case CMD_DIGITAL_READ:
      if (frame.header.payload_length == 1) {
        uint8_t pin = frame.payload[0];
        int value = ::digitalRead(pin);
#if BRIDGE_DEBUG_IO
        bridge_debug_log_gpio("digitalRead", pin, value);
#endif
        uint8_t resp_payload = static_cast<uint8_t>(value & 0xFF);
        sendFrame(CMD_DIGITAL_READ_RESP, &resp_payload, 1);
        command_processed_internally = true;
      }
      break;
    case CMD_ANALOG_READ:
      if (frame.header.payload_length == 1) {
        uint8_t pin = frame.payload[0];
        int value = ::analogRead(pin);
#if BRIDGE_DEBUG_IO
        bridge_debug_log_gpio("analogRead", pin, value);
#endif
        uint8_t resp_payload[2];
        rpc::write_u16_be(resp_payload,
                          static_cast<uint16_t>(value & 0xFFFF));
        sendFrame(CMD_ANALOG_READ_RESP, resp_payload, sizeof(resp_payload));
        command_processed_internally = true;
      }
      break;

    case CMD_CONSOLE_WRITE:
      Console._push(frame.payload, frame.header.payload_length);
      command_processed_internally = true; 
      requires_ack = true;
      break;
    case CMD_DATASTORE_PUT: 
    case CMD_FILE_WRITE:
    case CMD_FILE_REMOVE:
    case CMD_PROCESS_KILL:
      requires_ack = true; 
      break; 
        case CMD_MAILBOX_AVAILABLE:
          break;
    case CMD_DATASTORE_GET:
    case CMD_MAILBOX_READ: 
    case CMD_FILE_READ:
    case CMD_PROCESS_RUN:
    case CMD_PROCESS_RUN_ASYNC:
    case CMD_PROCESS_POLL:
      break; 
    default:
      break;
  }

    if (requires_ack) {
      uint8_t ack_payload[2];
      rpc::write_u16_be(ack_payload, frame.header.command_id);
      sendFrame(STATUS_ACK, ack_payload, sizeof(ack_payload));
  }

    if (!command_processed_internally &&
        frame.header.command_id <= STATUS_NOT_IMPLEMENTED) {
      switch (frame.header.command_id) {
        case STATUS_MALFORMED: {
          uint16_t malformed_id = 0xFFFF;
          if (frame.header.payload_length >= 2) {
            malformed_id = rpc::read_u16_be(frame.payload);
          }
          _handleMalformed(malformed_id);
          if (_status_handler) {
            _status_handler(
                (uint8_t)frame.header.command_id,
                frame.payload,
                frame.header.payload_length);
          }
          return;
        }
        case STATUS_ERROR:
        case STATUS_CMD_UNKNOWN:
        case STATUS_CRC_MISMATCH:
        case STATUS_TIMEOUT:
        case STATUS_NOT_IMPLEMENTED:
        case STATUS_OK:
          if (_status_handler) {
            _status_handler(
                (uint8_t)frame.header.command_id,
                frame.payload,
                frame.header.payload_length);
          }
          return;
        default:
          break;
      }
    }

  if (!command_processed_internally && _command_handler) {
    _command_handler(frame);
  }
}

void BridgeClass::_emitStatus(uint8_t status_code, const char* message) {
  const uint8_t* payload = nullptr;
  uint16_t length = 0;
  if (message && *message) {
    length = static_cast<uint16_t>(strlen(message));
    if (length > rpc::MAX_PAYLOAD_SIZE) {
      length = rpc::MAX_PAYLOAD_SIZE;
    }
    payload = reinterpret_cast<const uint8_t*>(message);
  }
  sendFrame(status_code, payload, length);
  if (_status_handler) {
    _status_handler(status_code, payload, length);
  }
}

bool BridgeClass::sendFrame(uint16_t command_id, const uint8_t* payload,
                            uint16_t payload_len) {
  if (!_requiresAck(command_id)) {
    return _sendFrameImmediate(command_id, payload, payload_len);
  }

  if (_awaiting_ack) {
    if (_enqueuePendingTx(command_id, payload, payload_len)) {
      return true;
    }
    _processAckTimeout();
    if (!_awaiting_ack && _enqueuePendingTx(command_id, payload, payload_len)) {
      return true;
    }
    return false;
  }

  return _sendFrameImmediate(command_id, payload, payload_len);
}

bool BridgeClass::_sendFrameImmediate(uint16_t command_id,
                                      const uint8_t* payload,
                                      uint16_t payload_len) {
  uint8_t* raw_frame_buf = _raw_frame_buffer;
  const size_t raw_capacity = sizeof(_raw_frame_buffer);

  // Use safe build method with buffer size
  size_t raw_len =
      _builder.build(raw_frame_buf, raw_capacity, command_id, payload, payload_len);

  if (raw_len == 0) {
#if BRIDGE_DEBUG_FRAMES
    _tx_debug.build_failures++;
#endif
    return false;
  }

#if BRIDGE_DEBUG_FRAMES
  _tx_debug.command_id = command_id;
  _tx_debug.payload_length = payload_len;
  _tx_debug.raw_length = static_cast<uint16_t>(raw_len);
#endif

  uint8_t* cobs_buf = _last_cobs_frame;
  size_t cobs_len = cobs::encode(raw_frame_buf, raw_len, cobs_buf);

  size_t written = _stream.write(cobs_buf, cobs_len);
  written += _stream.write((uint8_t)0x00);

#if BRIDGE_DEBUG_FRAMES
  _tx_debug.cobs_length = static_cast<uint16_t>(cobs_len);
  _tx_debug.expected_serial_bytes =
      static_cast<uint16_t>(cobs_len + 1);
  _tx_debug.last_write_return = static_cast<uint16_t>(written);
  if (raw_len >= sizeof(uint16_t)) {
    uint16_t crc = rpc::read_u16_be(
        raw_frame_buf + raw_len - sizeof(uint16_t));
    _tx_debug.crc = crc;
  } else {
    _tx_debug.crc = 0;
  }
  _tx_debug.tx_count++;
  if (written != static_cast<size_t>(cobs_len + 1)) {
    _tx_debug.write_shortfall_events++;
    uint16_t expected = static_cast<uint16_t>(cobs_len + 1);
    _tx_debug.last_shortfall =
        expected > _tx_debug.last_write_return
            ? expected - _tx_debug.last_write_return
            : 0;
  } else {
    _tx_debug.last_shortfall = 0;
  }
#endif

  const bool success = written == (cobs_len + 1);
  if (!success) {
    return false;
  }

  if (_requiresAck(command_id)) {
    _recordLastFrame(command_id, cobs_buf, cobs_len);
    _awaiting_ack = true;
    _retry_count = 0;
    _last_send_millis = millis();
  }

  return true;
}

#if BRIDGE_DEBUG_FRAMES
BridgeClass::FrameDebugSnapshot BridgeClass::getTxDebugSnapshot() const {
  return _tx_debug;
}

void BridgeClass::resetTxDebugStats() { _tx_debug = {}; }
#endif

bool BridgeClass::_requiresAck(uint16_t command_id) const {
  return command_id > STATUS_ACK;
}

void BridgeClass::_recordLastFrame(uint16_t command_id,
                                   const uint8_t* cobs_frame,
                                   size_t cobs_len) {
  if (cobs_len > sizeof(_last_cobs_frame)) {
    cobs_len = sizeof(_last_cobs_frame);
  }
  if (cobs_frame != _last_cobs_frame) {
    memcpy(_last_cobs_frame, cobs_frame, cobs_len);
  }
  _last_cobs_length = static_cast<uint16_t>(cobs_len);
  _last_command_id = command_id;
}

void BridgeClass::_clearAckState() {
  _awaiting_ack = false;
  _retry_count = 0;
  _last_cobs_length = 0;
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
  if (!_last_cobs_length) {
    return;
  }
  if (command_id == 0xFFFF || command_id == _last_command_id) {
    _retransmitLastFrame();
  }
}

void BridgeClass::_retransmitLastFrame() {
  if (!_awaiting_ack || !_last_cobs_length) {
    return;
  }
  size_t written = _stream.write(_last_cobs_frame, _last_cobs_length);
  written += _stream.write((uint8_t)0x00);
#if BRIDGE_DEBUG_FRAMES
  _tx_debug.tx_count++;
  _tx_debug.last_write_return = static_cast<uint16_t>(written);
  uint16_t expected = static_cast<uint16_t>(_last_cobs_length + 1);
  _tx_debug.expected_serial_bytes = expected;
  if (written != expected) {
    _tx_debug.write_shortfall_events++;
    _tx_debug.last_shortfall =
        expected > _tx_debug.last_write_return
            ? expected - _tx_debug.last_write_return
            : 0;
  } else {
    _tx_debug.last_shortfall = 0;
  }
#endif
  _retry_count++;
  _last_send_millis = millis();
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
      _status_handler(STATUS_TIMEOUT, nullptr, 0);
    }
    _flushPendingTxQueue();
    return;
  }
  _retransmitLastFrame();
}

void BridgeClass::_resetLinkState() {
  _clearAckState();
  _clearPendingTxQueue();
  _parser.reset();
  _pending_datastore_head = 0;
  _pending_datastore_count = 0;
  for (uint8_t i = 0; i < kMaxPendingDatastore; ++i) {
    _pending_datastore_key_lengths[i] = 0;
    _pending_datastore_keys[i][0] = '\0';
  }
  _pending_process_poll_head = 0;
  _pending_process_poll_count = 0;
  for (uint8_t i = 0; i < kMaxPendingProcessPolls; ++i) {
    _pending_process_pids[i] = 0;
  }
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
          frame.command_id, frame.payload, frame.payload_length)) {
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

bool BridgeClass::_enqueuePendingTx(uint16_t command_id, const uint8_t* payload,
                                    uint16_t payload_len) {
  if (_pending_tx_count >= kMaxPendingTxFrames) {
    return false;
  }
  if (payload_len > rpc::MAX_PAYLOAD_SIZE) {
    return false;
  }
  uint8_t tail = (_pending_tx_head + _pending_tx_count) % kMaxPendingTxFrames;
  _pending_tx_frames[tail].command_id = command_id;
  _pending_tx_frames[tail].payload_length = payload_len;
  if (payload_len > 0 && payload != nullptr) {
    memcpy(_pending_tx_frames[tail].payload, payload, payload_len);
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
  ::analogWrite(pin, (int)val_u8);
}

void BridgeClass::requestDigitalRead(uint8_t pin) {
  (void)pin;
  _emitStatus(STATUS_NOT_IMPLEMENTED, "pin_read_initiate_from_linux");
}

void BridgeClass::requestAnalogRead(uint8_t pin) {
  (void)pin;
  _emitStatus(STATUS_NOT_IMPLEMENTED, "pin_read_initiate_from_linux");
}

void BridgeClass::requestProcessRun(const char* command) {
  if (!command) {
    return;
  }
  size_t cmd_len = strlen(command);
  if (cmd_len == 0) {
    return;
  }
  if (cmd_len > rpc::MAX_PAYLOAD_SIZE) {
    _emitStatus(STATUS_ERROR, "process_run_payload_too_large");
    return;
  }
  sendFrame(
      CMD_PROCESS_RUN,
      reinterpret_cast<const uint8_t*>(command),
      static_cast<uint16_t>(cmd_len));
}

void BridgeClass::requestProcessRunAsync(const char* command) {
  if (!command) {
    return;
  }
  size_t cmd_len = strlen(command);
  if (cmd_len == 0) {
    return;
  }
  if (cmd_len > rpc::MAX_PAYLOAD_SIZE) {
    _emitStatus(STATUS_ERROR, "process_run_async_payload_too_large");
    return;
  }
  sendFrame(
      CMD_PROCESS_RUN_ASYNC,
      reinterpret_cast<const uint8_t*>(command),
      static_cast<uint16_t>(cmd_len));
}

void BridgeClass::requestProcessPoll(int pid) {
  if (pid < 0) {
    return;
  }

  const uint16_t pid_u16 = static_cast<uint16_t>(pid);
  if (!_pushPendingProcessPid(pid_u16)) {
    _emitStatus(STATUS_ERROR, "process_poll_queue_full");
    return;
  }

  uint8_t pid_payload[2];
  rpc::write_u16_be(pid_payload, pid_u16);
  sendFrame(CMD_PROCESS_POLL, pid_payload, 2);
}

void BridgeClass::requestFileSystemRead(const char* filePath) {
  if (!filePath) {
    return;
  }
  size_t path_len = strlen(filePath);
  if (path_len == 0 || path_len > kMaxFilePathLength) {
    return;
  }

  uint8_t* payload = _scratch_payload;
  payload[0] = static_cast<uint8_t>(path_len);
  memcpy(payload + kFileReadLengthPrefix, filePath, path_len);
  const uint16_t total = static_cast<uint16_t>(
      path_len + kFileReadLengthPrefix);
  sendFrame(CMD_FILE_READ, payload, total);
}

void BridgeClass::requestGetFreeMemory() {
  sendFrame(CMD_GET_FREE_MEMORY, nullptr, 0);
}

bool BridgeClass::_trackPendingDatastoreKey(const char* key) {
  if (!key || !*key) {
    return false;
  }

  size_t length = strnlen(key, kMaxDatastoreKeyLength);
  if (length == 0) {
    return false;
  }

  if (_pending_datastore_count >= kMaxPendingDatastore) {
    return false;
  }

  uint8_t slot =
      (_pending_datastore_head + _pending_datastore_count) %
      kMaxPendingDatastore;
  memcpy(_pending_datastore_keys[slot], key, length);
  _pending_datastore_keys[slot][length] = '\0';
  _pending_datastore_key_lengths[slot] = static_cast<uint8_t>(length);
  _pending_datastore_count++;
  return true;
}

const char* _popPendingDatastoreKey() {
  // Not using _popPendingDatastoreKey in the current implementation logic
  // inside BridgeClass, but keeping signature consistent if needed.
  // Here we just use the member function version.
  return nullptr; 
}

const char* BridgeClass::_popPendingDatastoreKey() {
  static char key_buffer[kMaxDatastoreKeyLength + 1] = {0};
  if (_pending_datastore_count == 0) {
    key_buffer[0] = '\0';
    return key_buffer;
  }

  uint8_t slot = _pending_datastore_head;
  uint8_t length = _pending_datastore_key_lengths[slot];
  if (length > kMaxDatastoreKeyLength) {
    length = kMaxDatastoreKeyLength;
  }
  memcpy(key_buffer, _pending_datastore_keys[slot], length);
  key_buffer[length] = '\0';

  _pending_datastore_head =
      (_pending_datastore_head + 1) % kMaxPendingDatastore;
  _pending_datastore_count--;
  _pending_datastore_key_lengths[slot] = 0;
  _pending_datastore_keys[slot][0] = '\0';
  return key_buffer;
}

bool BridgeClass::_pushPendingProcessPid(uint16_t pid) {
  if (_pending_process_poll_count >= kMaxPendingProcessPolls) {
    return false;
  }

  uint8_t slot =
      (_pending_process_poll_head + _pending_process_poll_count) %
      kMaxPendingProcessPolls;
  _pending_process_pids[slot] = pid;
  _pending_process_poll_count++;
  return true;
}

uint16_t BridgeClass::_popPendingProcessPid() {
  if (_pending_process_poll_count == 0) {
    return 0xFFFF;
  }

  uint8_t slot = _pending_process_poll_head;
  uint16_t pid = _pending_process_pids[slot];
  _pending_process_poll_head =
      (_pending_process_poll_head + 1) % kMaxPendingProcessPolls;
  _pending_process_poll_count--;
  _pending_process_pids[slot] = 0;
  return pid;
}
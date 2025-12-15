/*
 * This file is part of Arduino Yun Ecosystem v2.
 */
#include "Bridge.h"
#include "arduino/HardwareAbstraction.h"

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
constexpr size_t kFileReadLengthPrefix = 1;
const char kSerialOverflowMessage[] PROGMEM = "serial_rx_overflow";
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
      _datastore_get_handler(nullptr),
      _mailbox_handler(nullptr),
      _mailbox_available_handler(nullptr),
      _process_run_handler(nullptr),
      _process_poll_handler(nullptr),
      _digital_read_handler(nullptr),
      _analog_read_handler(nullptr),
      _process_run_async_handler(nullptr),
      _file_system_read_handler(nullptr),
      _get_free_memory_handler(nullptr),
      _status_handler(nullptr),
      _pending_tx_head(0),
      _pending_tx_count(0),
      _pending_datastore_head(0),
      _pending_datastore_count(0),
      _pending_process_poll_head(0),
      _pending_process_poll_count(0),
      _synchronized(false)
#if BRIDGE_DEBUG_FRAMES
      , _tx_debug{}
#endif
{
  for (auto& key : _pending_datastore_keys) {
    key.fill(0);
  }
  _pending_datastore_key_lengths.fill(0);
  _pending_process_pids.fill(0);
  // _pending_tx_frames is array of structs, default init is fine or we can loop
  for (auto& frame : _pending_tx_frames) {
    frame.command_id = 0;
    frame.payload_length = 0;
    frame.payload.fill(0);
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
      _datastore_get_handler(nullptr),
      _mailbox_handler(nullptr),
      _mailbox_available_handler(nullptr),
      _process_run_handler(nullptr),
      _process_poll_handler(nullptr),
      _digital_read_handler(nullptr),
      _analog_read_handler(nullptr),
      _process_run_async_handler(nullptr),
      _file_system_read_handler(nullptr),
      _get_free_memory_handler(nullptr),
      _status_handler(nullptr),
      _pending_tx_head(0),
      _pending_tx_count(0),
      _pending_datastore_head(0),
      _pending_datastore_count(0),
      _pending_process_poll_head(0),
      _pending_process_poll_count(0),
      _synchronized(false)
#if BRIDGE_DEBUG_FRAMES
      , _tx_debug{}
#endif
{
  for (auto& key : _pending_datastore_keys) {
    key.fill(0);
  }
  _pending_datastore_key_lengths.fill(0);
  _pending_process_pids.fill(0);
  for (auto& frame : _pending_tx_frames) {
    frame.command_id = 0;
    frame.payload_length = 0;
    frame.payload.fill(0);
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

  for (auto& key : _pending_datastore_keys) {
    key.fill(0);
  }
  _pending_datastore_key_lengths.fill(0);

  _pending_process_poll_head = 0;
  _pending_process_poll_count = 0;
  _pending_process_pids.fill(0);

  _awaiting_ack = false;
  // _flow_paused = false; // Handled by transport
  _last_command_id = 0;
  _retry_count = 0;
  _last_send_millis = 0;
#if BRIDGE_DEBUG_FRAMES
  _tx_debug = {};
#endif

  // Blocking wait for synchronization
  // This ensures the handshake is complete before the sketch continues,
  // preventing race conditions and ensuring the daemon is ready.
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

void BridgeClass::onMailboxMessage(MailboxHandler handler) {
  _mailbox_handler = handler;
}

void BridgeClass::onMailboxAvailableResponse(MailboxAvailableHandler handler) {
  _mailbox_available_handler = handler;
}
void BridgeClass::onCommand(CommandHandler handler) { _command_handler = handler; }
void BridgeClass::onDataStoreGetResponse(DataStoreGetHandler handler) { _datastore_get_handler = handler; }
void BridgeClass::onProcessRunResponse(ProcessRunHandler handler) { _process_run_handler = handler; }
void BridgeClass::onProcessPollResponse(ProcessPollHandler handler) { _process_poll_handler = handler; }
void BridgeClass::onProcessRunAsyncResponse(ProcessRunAsyncHandler handler) { _process_run_async_handler = handler; }
void BridgeClass::onFileSystemReadResponse(FileSystemReadHandler handler) { _file_system_read_handler = handler; }
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
        uint16_t free_mem = bridge::hardware::getFreeMemory();
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
            memcpy(&response[nonce_length], tag, kHandshakeTagSize);
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

void BridgeClass::_handleDatastoreCommand(const rpc::Frame& frame) {
  const CommandId command = static_cast<CommandId>(frame.header.command_id);
  if (command == CommandId::CMD_DATASTORE_GET_RESP) {
      const size_t payload_length = frame.header.payload_length;
      const uint8_t* payload_data = frame.payload;
      
      if (payload_length >= 1 && payload_data != nullptr) {
        uint8_t value_len = payload_data[0];
        const size_t expected = static_cast<size_t>(1 + value_len);
        if (payload_length >= expected) {
          const uint8_t* value_ptr = payload_data + 1;
          const char* key = _popPendingDatastoreKey();
          if (_datastore_get_handler) {
            _datastore_get_handler(key, value_ptr, value_len);
          }
        }
      }
  }
}

void BridgeClass::_handleMailboxCommand(const rpc::Frame& frame) {
  const CommandId command = static_cast<CommandId>(frame.header.command_id);
  const size_t payload_length = frame.header.payload_length;
  const uint8_t* payload_data = frame.payload;

  switch (command) {
    case CommandId::CMD_MAILBOX_READ_RESP:
      if (_mailbox_handler && payload_length >= 2 && payload_data != nullptr) {
        uint16_t message_len = rpc::read_u16_be(payload_data);
        const size_t expected = static_cast<size_t>(2 + message_len);
        if (payload_length >= expected) {
          const uint8_t* body_ptr = payload_data + 2;
          _mailbox_handler(body_ptr, message_len);
        }
      }
      break;
    case CommandId::CMD_MAILBOX_AVAILABLE_RESP:
      if (_mailbox_available_handler && payload_length == 1 && payload_data) {
        uint8_t count = payload_data[0];
        _mailbox_available_handler(count);
      }
      break;
    case CommandId::CMD_MAILBOX_PUSH:
      if (_mailbox_handler && payload_length >= 2 && payload_data != nullptr) {
        uint16_t message_len = rpc::read_u16_be(payload_data);
        const size_t expected = static_cast<size_t>(2 + message_len);
        if (payload_length >= expected) {
          const uint8_t* body_ptr = payload_data + 2;
          _mailbox_handler(body_ptr, message_len);
        }
      }
      break;
    case CommandId::CMD_MAILBOX_AVAILABLE:
       if (_mailbox_available_handler && payload_length == 1 && payload_data) {
        uint8_t count = payload_data[0];
        _mailbox_available_handler(count);
      }
      break;
    default:
      break;
  }
}

void BridgeClass::_handleFileSystemCommand(const rpc::Frame& frame) {
  const CommandId command = static_cast<CommandId>(frame.header.command_id);
  const size_t payload_length = frame.header.payload_length;
  const uint8_t* payload_data = frame.payload;

  switch (command) {
    case CommandId::CMD_FILE_READ_RESP:
      if (_file_system_read_handler && payload_length >= 2 && payload_data) {
        uint16_t data_len = rpc::read_u16_be(payload_data);
        const size_t expected = static_cast<size_t>(2 + data_len);
        if (payload_length >= expected) {
          _file_system_read_handler(payload_data + 2, data_len);
        }
      }
      break;
    case CommandId::CMD_FILE_WRITE:
      if (payload_length > 1 && payload_data) {
           uint8_t path_len = payload_data[0];
           if (path_len < payload_length) {
               const char* path_start = reinterpret_cast<const char*>(payload_data + 1);
               const uint8_t* data_ptr = payload_data + 1 + path_len;
               size_t data_len = payload_length - 1 - path_len;

               bool is_eeprom = false;
#if defined(ARDUINO_ARCH_AVR)
               const size_t prefix_len = 8; // "/eeprom/" length
               if (path_len >= prefix_len) {
                   if (strncmp_P(path_start, PSTR("/eeprom/"), prefix_len) == 0) {
                       is_eeprom = true;
                   }
               }
#else
               const char prefix[] = "/eeprom/";
               const size_t prefix_len = sizeof(prefix) - 1;
               if (path_len >= prefix_len) {
                   if (strncmp(path_start, prefix, prefix_len) == 0) {
                       is_eeprom = true;
                   }
               }
#endif

#if defined(ARDUINO_ARCH_AVR)
               if (is_eeprom && data_len > 0) {
                   int offset = 0;
                   if (path_len > prefix_len) {
                       const char* num_start = path_start + prefix_len;
                       size_t num_len = path_len - prefix_len;
                       bool valid_num = true;
                       for (size_t i = 0; i < num_len; ++i) {
                           if (num_start[i] < '0' || num_start[i] > '9') {
                               valid_num = false;
                               break;
                           }
                       }
                       
                       if (valid_num) {
                           for (size_t i = 0; i < num_len; ++i) {
                               char c = num_start[i];
                               offset = offset * 10 + (c - '0');
                           }
                           
                           for (size_t i = 0; i < data_len; i++) {
                               eeprom_update_byte((uint8_t*)(offset + i), data_ptr[i]);
                           }
                       }
                   }
               }
#else
               (void)data_ptr;
               (void)data_len;
               (void)is_eeprom;
#endif
           }
      }
      break;
    default:
      break;
  }
}

void BridgeClass::_handleProcessCommand(const rpc::Frame& frame) {
  const CommandId command = static_cast<CommandId>(frame.header.command_id);
  const size_t payload_length = frame.header.payload_length;
  const uint8_t* payload_data = frame.payload;

  switch (command) {
    case CommandId::CMD_PROCESS_RUN_RESP:
      if (_process_run_handler && payload_length >= 1 && payload_data) {
        rpc::StatusCode status = static_cast<rpc::StatusCode>(payload_data[0]);
        if (payload_length >= 5) {
            uint16_t stdout_len = rpc::read_u16_be(payload_data + 1);
            const uint8_t* stdout_ptr = payload_data + 3;
            if (payload_length >= static_cast<size_t>(3 + stdout_len + 2)) {
                uint16_t stderr_len = rpc::read_u16_be(payload_data + 3 + stdout_len);
                const uint8_t* stderr_ptr = payload_data + 3 + stdout_len + 2;
                _process_run_handler(status, stdout_ptr, stdout_len, stderr_ptr, stderr_len);
            }
        }
      }
      break;
    case CommandId::CMD_PROCESS_RUN_ASYNC_RESP:
      if (_process_run_async_handler && payload_length >= 2 && payload_data) {
        uint16_t pid = rpc::read_u16_be(payload_data);
        _process_run_async_handler(static_cast<int>(pid));
      }
      break;
    case CommandId::CMD_PROCESS_POLL_RESP:
      if (_process_poll_handler && payload_length >= 2 && payload_data) {
        rpc::StatusCode status = static_cast<rpc::StatusCode>(payload_data[0]);
        uint8_t running = payload_data[1];
        
        // CRITICAL FIX: Ensure PID is popped BEFORE invoking handler to prevent stack recursion loops
        // in synchronous test environments.
        _popPendingProcessPid(); 
        
        if (payload_length >= 6) {
             uint16_t stdout_len = rpc::read_u16_be(payload_data + 2);
             const uint8_t* stdout_ptr = payload_data + 4;
             if (payload_length >= static_cast<size_t>(4 + stdout_len + 2)) {
                 uint16_t stderr_len = rpc::read_u16_be(payload_data + 4 + stdout_len);
                 const uint8_t* stderr_ptr = payload_data + 4 + stdout_len + 2;
                 _process_poll_handler(status, running, stdout_ptr, stdout_len, stderr_ptr, stderr_len);
             }
        }
      }
      break;
    default:
      break;
  }
}

void BridgeClass::dispatch(const rpc::Frame& frame) {
  const uint16_t raw_command = frame.header.command_id;
  const CommandId command = static_cast<CommandId>(raw_command);
  
  // 1. Handle Responses (Linux -> MCU)
  _handleDatastoreCommand(frame);
  _handleMailboxCommand(frame);
  _handleFileSystemCommand(frame);
  _handleProcessCommand(frame);
  
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
      _handleMailboxCommand(frame);
      command_processed_internally = true;
      requires_ack = true;
      break;
    case CommandId::CMD_FILE_WRITE:
      _handleFileSystemCommand(frame);
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

void BridgeClass::requestProcessRun(const char* command) {
  if (!command || !*command) {
    return;
  }
  size_t len = strlen(command);
  if (len > rpc::MAX_PAYLOAD_SIZE) {
    _emitStatus(StatusCode::STATUS_ERROR, F("process_run_payload_too_large"));
    return;
  }
  (void)sendFrame(
      CommandId::CMD_PROCESS_RUN,
      reinterpret_cast<const uint8_t*>(command),
      len);
}

void BridgeClass::requestProcessRunAsync(const char* command) {
  if (!command || !*command) {
    return;
  }
  size_t len = strlen(command);
  if (len > rpc::MAX_PAYLOAD_SIZE) {
    _emitStatus(StatusCode::STATUS_ERROR, F("process_run_async_payload_too_large"));
    return;
  }
  (void)sendFrame(
      CommandId::CMD_PROCESS_RUN_ASYNC,
      reinterpret_cast<const uint8_t*>(command),
      len);
}

void BridgeClass::requestProcessPoll(int pid) {
  if (pid < 0) {
    return;
  }

  const uint16_t pid_u16 = static_cast<uint16_t>(pid);
  if (!_pushPendingProcessPid(pid_u16)) {
    _emitStatus(StatusCode::STATUS_ERROR, F("process_poll_queue_full"));
    return;
  }

  uint8_t pid_payload[2];
  rpc::write_u16_be(pid_payload, pid_u16);
  (void)sendFrame(CommandId::CMD_PROCESS_POLL, pid_payload, 2);
}

void BridgeClass::requestFileSystemRead(const char* filePath) {
  if (!filePath || !*filePath) {
    return;
  }
  size_t len = strlen(filePath);
  if (len > BridgeClass::kMaxFilePathLength) {
    return;
  }

  uint8_t* payload = _scratch_payload;
  payload[0] = static_cast<uint8_t>(len);
  memcpy(payload + kFileReadLengthPrefix, filePath, len);
  const uint16_t total = static_cast<uint16_t>(
      len + kFileReadLengthPrefix);
  (void)sendFrame(CommandId::CMD_FILE_READ, payload, total);
}

bool BridgeClass::_trackPendingDatastoreKey(const char* key) {
  if (!key || !*key) {
    return false;
  }

  const auto info = measure_bounded_cstring(key, kMaxDatastoreKeyLength);
  if (info.length == 0 || info.overflowed) {
    return false;
  }
  const size_t length = info.length;

  if (_pending_datastore_count >= kMaxPendingDatastore) {
    return false;
  }

  uint8_t slot =
      (_pending_datastore_head + _pending_datastore_count) %
      kMaxPendingDatastore;
  memcpy(_pending_datastore_keys[slot].data(), key, length);
  _pending_datastore_keys[slot][length] = '\0';
  _pending_datastore_key_lengths[slot] = static_cast<uint8_t>(length);
  _pending_datastore_count++;
  return true;
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
  memcpy(key_buffer, _pending_datastore_keys[slot].data(), length);
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
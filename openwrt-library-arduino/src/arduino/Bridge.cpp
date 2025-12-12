/*
 * This file is part of Arduino Yun Ecosystem v2.
 */
#include "Bridge.h"

#if defined(ARDUINO_ARCH_AVR)
#include <avr/pgmspace.h>
#include <avr/wdt.h>
#include <avr/eeprom.h> 
#endif

#include <cstring> 
#include <cstdlib> 
#include <cstdint>
#include <algorithm>
#include <iterator>
#include <array>
#include <charconv>
#include <Crypto.h>
#include <SHA256.h>

#include "arduino/StringUtils.h"
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
template <typename ActionText>
static void bridge_debug_log_gpio(ActionText action, uint8_t pin, int value) {
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
      _flow_paused(false), // Initialize Flow Control State
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
  _flow_paused = false;
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
    std::fill(out_tag, out_tag + kHandshakeTagSize, 0);
    return;
  }

  SHA256 sha256;
  std::array<uint8_t, kSha256DigestSize> digest;

  sha256.resetHMAC(_shared_secret, _shared_secret_len);
  sha256.update(nonce, nonce_len);
  sha256.finalizeHMAC(_shared_secret, _shared_secret_len, digest.data(), kSha256DigestSize);

  std::copy_n(digest.begin(), kHandshakeTagSize, out_tag);
}

void BridgeClass::_applyTimingConfig(std::span<const uint8_t> payload) {
  if (payload.size() < RPC_HANDSHAKE_CONFIG_SIZE) {
    _ack_timeout_ms = kAckTimeoutMs;
    _ack_retry_limit = kMaxAckRetries;
    _response_timeout_ms = RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS;
    return;
  }

  const uint8_t* cursor = payload.data();
  if (cursor == nullptr) {
    _ack_timeout_ms = kAckTimeoutMs;
    _ack_retry_limit = kMaxAckRetries;
    _response_timeout_ms = RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS;
    return;
  }
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

  // --- Point 1: Flow Control Logic (High/Low Water Mark) ---
  int available_bytes = _stream.available();
  
  if (!_flow_paused && available_bytes >= kRxHighWaterMark) {
      // Buffer getting full, pause sender
      (void)sendFrame(CommandId::CMD_XOFF);
      _flow_paused = true;
  } else if (_flow_paused && available_bytes <= kRxLowWaterMark) {
      // Buffer drained enough, resume sender
      (void)sendFrame(CommandId::CMD_XON);
      _flow_paused = false;
  }

  while (_stream.available()) {
    int byte_read = _stream.read(); // Use int to check -1
    if (byte_read >= 0) {
      uint8_t byte = static_cast<uint8_t>(byte_read);
      bool parsed = _parser.consume(byte, _rx_frame);
      if (_parser.overflowed()) {
        _parser.reset();
        _emitStatus(StatusCode::STATUS_MALFORMED, reinterpret_cast<const __FlashStringHelper*>(kSerialOverflowMessage));
        continue;
      }
      if (parsed) {
        dispatch(_rx_frame);
      }
    }
  }
  _processAckTimeout();
  // Retry queued frames after transient send failures.
  _flushPendingTxQueue();
  // Also pump console to flush partial buffers
  Console.flush(); 
}

void BridgeClass::flushStream() {
  if (_hardware_serial != nullptr) {
    _hardware_serial->flush();
    return;
  }
  _stream.flush();
}

void BridgeClass::dispatch(const rpc::Frame& frame) {
  const uint16_t raw_command = frame.header.command_id;
  const CommandId command = static_cast<CommandId>(raw_command);
  const size_t payload_length = frame.header.payload_length;
  const std::span<const uint8_t> payload(frame.payload, payload_length);
  const uint8_t* payload_data = payload.data();

  // Switch 1: RESPONSES (from Linux -> MCU requests)
  switch (command) {
    case CommandId::CMD_DATASTORE_GET_RESP:
      if (payload.size() >= 1 && payload_data != nullptr) {
        uint8_t value_len = payload_data[0];
        const size_t expected = static_cast<size_t>(1 + value_len);
        if (payload.size() >= expected) {
          const uint8_t* value_ptr = payload_data + 1;
          const char* key = _popPendingDatastoreKey();
          if (_datastore_get_handler) {
            _datastore_get_handler(key, value_ptr, value_len);
          }
        }
      }
      return;
    case CommandId::CMD_MAILBOX_READ_RESP:
      if (_mailbox_handler && payload.size() >= 2 && payload_data != nullptr) {
        uint16_t message_len = rpc::read_u16_be(payload_data);
        const size_t expected = static_cast<size_t>(2 + message_len);
        if (payload.size() >= expected) {
          const std::span<const uint8_t> body = payload.subspan(2, message_len);
          const uint8_t* body_ptr = body.empty() ? payload_data + 2 : body.data();
          _mailbox_handler(body_ptr, body.size());
        }
      }
      return;
    case CommandId::CMD_MAILBOX_AVAILABLE_RESP:
      if (_mailbox_available_handler && payload.size() == 1 && payload_data) {
        uint8_t count = payload_data[0];
        _mailbox_available_handler(count);
      }
      return;
    case CommandId::CMD_PROCESS_RUN_RESP:
      if (_process_run_handler && payload.size() >= 5 && payload_data) {
        const uint8_t* cursor = payload_data;
        StatusCode status = static_cast<StatusCode>(*cursor++);
        uint16_t stdout_len = rpc::read_u16_be(cursor);
        cursor += 2;
        const size_t stdout_expected = static_cast<size_t>(5 + stdout_len);
        if (payload.size() < stdout_expected) {
          return;
        }
        const uint8_t* stdout_ptr = cursor;
        cursor += stdout_len;
        uint16_t stderr_len = rpc::read_u16_be(cursor);
        cursor += 2;
        const size_t total_expected =
            static_cast<size_t>(5 + stdout_len + stderr_len);
        if (payload.size() < total_expected) {
          return;
        }
        const uint8_t* stderr_ptr = cursor;
        _process_run_handler(
            status, stdout_ptr, stdout_len, stderr_ptr, stderr_len);
      }
      return;
    case CommandId::CMD_PROCESS_POLL_RESP:
      if (payload.size() >= 6 && payload_data) {
        uint16_t pid = _popPendingProcessPid();
        const uint8_t* p = payload_data;
        StatusCode status = static_cast<StatusCode>(*p++);
        uint8_t exit_code = *p++;
        uint16_t stdout_len = rpc::read_u16_be(p);
        p += 2;
        uint16_t stderr_len = rpc::read_u16_be(p);
        p += 2;
        const size_t expected = static_cast<size_t>(6 + stdout_len + stderr_len);
        if (payload.size() >= expected) {
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
              pid != 0xFFFF && status == StatusCode::STATUS_OK
              && (stdout_len > 0 || stderr_len > 0)) {
            requestProcessPoll((int)pid);
          }
        }
      }
      return;
    case CommandId::CMD_PROCESS_RUN_ASYNC_RESP:
      if (_process_run_async_handler && payload.size() == 2 && payload_data) {
        uint16_t pid = rpc::read_u16_be(payload_data);
        _process_run_async_handler((int)pid);
      }
      return;
    case CommandId::CMD_FILE_READ_RESP:
      if (_file_system_read_handler && payload.size() >= 2 && payload_data) {
        uint16_t data_len = rpc::read_u16_be(payload_data);
        const size_t expected = static_cast<size_t>(2 + data_len);
        if (payload.size() >= expected) {
          const std::span<const uint8_t> content = payload.subspan(2, data_len);
          const uint8_t* content_ptr = content.empty() ? payload_data + 2 : content.data();
          _file_system_read_handler(content_ptr, data_len);
        }
      }
      return;
    case CommandId::CMD_GET_FREE_MEMORY_RESP:
      if (_get_free_memory_handler && payload.size() >= 2 && payload_data) {
        uint16_t free_mem = rpc::read_u16_be(payload_data);
        _get_free_memory_handler(free_mem);
      }
      return;
    // --- Flow Control (Ignored but handled to prevent unknown cmd error) ---
    case CommandId::CMD_XOFF:
    case CommandId::CMD_XON:
    case CommandId::CMD_MAILBOX_PROCESSED:
      return; 
    default:
      break;
  }

  bool command_processed_internally = false;
  bool requires_ack = false;

  // Switch 2: COMMANDS (Actions requested by Linux)
  switch (command) {
    case CommandId::CMD_GET_VERSION:
      {
        if (payload_length != 0) {
          break;
        }
        uint8_t version_payload[2] = {
            (uint8_t)BRIDGE_FIRMWARE_VERSION_MAJOR,
            (uint8_t)BRIDGE_FIRMWARE_VERSION_MINOR};
        (void)sendFrame(
          CommandId::CMD_GET_VERSION_RESP,
          std::span<const uint8_t>(version_payload, sizeof(version_payload)));
        command_processed_internally = true;
      }
      break;
    case CommandId::CMD_GET_FREE_MEMORY:
      {
        if (payload_length != 0) {
          break;
        }
        uint16_t free_mem = calculateFreeMemoryBytes();
        uint8_t resp_payload[2];
        resp_payload[0] = (free_mem >> 8) & 0xFF;
        resp_payload[1] = free_mem & 0xFF;
        (void)sendFrame(CommandId::CMD_GET_FREE_MEMORY_RESP, std::span<const uint8_t>(resp_payload, 2));
        command_processed_internally = true;
      }
      break;

    case CommandId::CMD_GET_TX_DEBUG_SNAPSHOT:
      {
        if (payload_length != 0) {
          break;
        }
        uint8_t resp[9];
        resp[0] = _pending_tx_count;
        resp[1] = _awaiting_ack ? 1 : 0;
        resp[2] = _retry_count;
        rpc::write_u16_be(&resp[3], _last_command_id);
        rpc::write_u32_be(&resp[5], static_cast<uint32_t>(_last_send_millis));
        
        (void)sendFrame(CommandId::CMD_GET_TX_DEBUG_SNAPSHOT_RESP, std::span<const uint8_t>(resp, sizeof(resp)));
        command_processed_internally = true;
      }
      break;

    case CommandId::CMD_LINK_SYNC:
      {
        const size_t nonce_length = payload_length;
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
          (void)sendFrame(StatusCode::STATUS_MALFORMED);
          command_processed_internally = true;
          requires_ack = false;
          break;
        }

        uint8_t* response = _scratch_payload;
        if (payload_data == nullptr) {
          break;
        }
        std::copy_n(payload_data, nonce_length, response);
        if (has_secret) {
          uint8_t tag[kHandshakeTagSize];
          _computeHandshakeTag(payload_data, nonce_length, tag);
          std::copy_n(tag, kHandshakeTagSize, &response[nonce_length]);
        }

        (void)sendFrame(
          CommandId::CMD_LINK_SYNC_RESP,
          std::span<const uint8_t>(response, response_length));
        command_processed_internally = true;
        requires_ack = true;
      }
      break;
    case CommandId::CMD_LINK_RESET:
      {
        if (
            payload_length != 0 &&
            payload_length != RPC_HANDSHAKE_CONFIG_SIZE) {
          break;
        }
        _resetLinkState();
        _applyTimingConfig(payload);
        Console.begin();
        (void)sendFrame(CommandId::CMD_LINK_RESET_RESP);
        command_processed_internally = true;
        requires_ack = true;
      }
      break;

    case CommandId::CMD_SET_PIN_MODE:
      if (payload_length == 2 && payload_data) {
      uint8_t pin = payload_data[0];
      uint8_t mode = payload_data[1];
      ::pinMode(pin, mode);
    #if BRIDGE_DEBUG_IO
      bridge_debug_log_gpio(F("pinMode"), pin, mode);
    #endif
        command_processed_internally = true;
        requires_ack = true;
      }
      break;
    case CommandId::CMD_DIGITAL_WRITE:
      if (payload_length == 2 && payload_data) {
      uint8_t pin = payload_data[0];
      uint8_t value = payload_data[1] ? HIGH : LOW;
      ::digitalWrite(pin, value);
    #if BRIDGE_DEBUG_IO
      bridge_debug_log_gpio(
          F("digitalWrite"), pin, value == HIGH ? 1 : 0);
    #endif
        command_processed_internally = true;
        requires_ack = true;
      }
      break;
    case CommandId::CMD_ANALOG_WRITE:
      if (payload_length == 2 && payload_data) {
        ::analogWrite(payload_data[0], static_cast<int>(payload_data[1]));
        command_processed_internally = true;
        requires_ack = true;
      }
      break;
    case CommandId::CMD_DIGITAL_READ:
      if (payload_length == 1 && payload_data) {
        uint8_t pin = payload_data[0];
        int value = ::digitalRead(pin);
#if BRIDGE_DEBUG_IO
  bridge_debug_log_gpio(F("digitalRead"), pin, value);
#endif
        uint8_t resp_payload = static_cast<uint8_t>(value & 0xFF);
        (void)sendFrame(CommandId::CMD_DIGITAL_READ_RESP, std::span<const uint8_t>(&resp_payload, 1));
        command_processed_internally = true;
      }
      break;
    case CommandId::CMD_ANALOG_READ:
      if (payload_length == 1 && payload_data) {
        uint8_t pin = payload_data[0];
        int value = ::analogRead(pin);
#if BRIDGE_DEBUG_IO
  bridge_debug_log_gpio(F("analogRead"), pin, value);
#endif
        uint8_t resp_payload[2];
        rpc::write_u16_be(resp_payload,
                          static_cast<uint16_t>(value & 0xFFFF));
        (void)sendFrame(
          CommandId::CMD_ANALOG_READ_RESP,
          std::span<const uint8_t>(resp_payload, sizeof(resp_payload)));
        command_processed_internally = true;
      }
      break;

    case CommandId::CMD_MAILBOX_PUSH:
      if (_mailbox_handler && payload_length >= 2 && payload_data != nullptr) {
        uint16_t message_len = rpc::read_u16_be(payload_data);
        const size_t expected = static_cast<size_t>(2 + message_len);
        if (payload.size() >= expected) {
          const std::span<const uint8_t> body = payload.subspan(2, message_len);
          const uint8_t* body_ptr = body.empty() ? payload_data + 2 : body.data();
          _mailbox_handler(body_ptr, body.size());
        }
      }
      command_processed_internally = true;
      requires_ack = true;
      break;

    // --- Point 2: File Write Logic (Virtual EEPROM) ---
    case CommandId::CMD_FILE_WRITE:
      {
        // Protocol: [len_path (1B)][path...][data...]
        if (payload_length > 1 && payload_data) {
           uint8_t path_len = payload_data[0];
           if (payload_length >= 1 + path_len) {
               // We don't allocate path buffer to save stack, we inspect in place
               const char* path_start = reinterpret_cast<const char*>(payload_data + 1);
               const uint8_t* data_ptr = payload_data + 1 + path_len;
               size_t data_len = payload_length - 1 - path_len;

               // Check if writing to special path "/eeprom/"
               // Path is not null terminated in buffer, be careful
               const char prefix[] = "/eeprom/";
               const size_t prefix_len = sizeof(prefix) - 1;
               
               bool is_eeprom = false;
               if (path_len >= prefix_len) {
                   if (strncmp(path_start, prefix, prefix_len) == 0) {
                       is_eeprom = true;
                   }
               }

#if defined(ARDUINO_ARCH_AVR)
               if (is_eeprom && data_len > 0) {
                   // Parse offset from filename? e.g. /eeprom/0
                   // Or just assume raw sequential write from 0? 
                   // Let's parse offset from remainder of path
                   int offset = 0;
                   if (path_len > prefix_len) {
                       const char* num_start = path_start + prefix_len;
                       const char* num_end = path_start + path_len;
                       std::from_chars(num_start, num_end, offset);
                   }
                   
                   for (size_t i = 0; i < data_len; i++) {
                       eeprom_update_byte((uint8_t*)(offset + i), data_ptr[i]);
                   }
               }
#endif
           }
        }
        command_processed_internally = true;
        requires_ack = true;
      }
      break;

    case CommandId::CMD_CONSOLE_WRITE:
      Console._push(std::span<const uint8_t>(frame.payload, frame.header.payload_length));
      command_processed_internally = true; 
      requires_ack = true;
      break;
    case CommandId::CMD_DATASTORE_PUT: 
    case CommandId::CMD_FILE_REMOVE:
    case CommandId::CMD_PROCESS_KILL:
      requires_ack = true; 
      break; 
    case CommandId::CMD_MAILBOX_AVAILABLE:
      if (_mailbox_available_handler && payload.size() == 1 && payload_data) {
        uint8_t count = payload_data[0];
        _mailbox_available_handler(count);
      }
      command_processed_internally = true;
      requires_ack = true;
      break;
    case CommandId::CMD_DATASTORE_GET:
    case CommandId::CMD_MAILBOX_READ: 
    case CommandId::CMD_FILE_READ:
    case CommandId::CMD_PROCESS_RUN:
    case CommandId::CMD_PROCESS_RUN_ASYNC:
    case CommandId::CMD_PROCESS_POLL:
      break; 
    default:
      break;
  }

  if (requires_ack) {
    uint8_t ack_payload[2];
    rpc::write_u16_be(ack_payload, raw_command);
    (void)sendFrame(StatusCode::STATUS_ACK, std::span<const uint8_t>(ack_payload, sizeof(ack_payload)));
  }

  if (!command_processed_internally &&
      raw_command <= rpc::to_underlying(StatusCode::STATUS_ACK)) {
    const StatusCode status = static_cast<StatusCode>(raw_command);
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
      case StatusCode::STATUS_OK:
        if (_status_handler) {
          _status_handler(status, payload_data, static_cast<uint16_t>(payload_length));
        }
        return;
    }
  }

  if (!command_processed_internally && _command_handler) {
    _command_handler(frame);
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
  std::span<const uint8_t> view(payload, length);
  (void)sendFrame(status_code, view);
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
  std::span<const uint8_t> view(payload, length);
  (void)sendFrame(status_code, view);
  if (_status_handler) {
    _status_handler(status_code, payload, length);
  }
}

bool BridgeClass::sendFrame(CommandId command_id, std::span<const uint8_t> payload) {
  return _sendFrame(rpc::to_underlying(command_id), payload);
}

bool BridgeClass::sendFrame(StatusCode status_code, std::span<const uint8_t> payload) {
  return _sendFrame(rpc::to_underlying(status_code), payload);
}

bool BridgeClass::_sendFrame(uint16_t command_id, std::span<const uint8_t> payload) {
  if (!_requiresAck(command_id)) {
    return _sendFrameImmediate(command_id, payload);
  }

  if (_awaiting_ack) {
    if (_enqueuePendingTx(command_id, payload)) {
      return true;
    }
    _processAckTimeout();
    if (!_awaiting_ack && _enqueuePendingTx(command_id, payload)) {
      return true;
    }
    return false;
  }

  return _sendFrameImmediate(command_id, payload);
}

bool BridgeClass::_sendFrameImmediate(uint16_t command_id,
                                      std::span<const uint8_t> payload) {
  uint8_t* raw_frame_buf = _raw_frame_buffer;
  const size_t raw_capacity = sizeof(_raw_frame_buffer);
  const uint8_t* payload_ptr = payload.data();
  const size_t payload_len = payload.size();

  // Use safe build method with buffer size
  size_t raw_len =
      _builder.build(
          raw_frame_buf,
          raw_capacity,
          command_id,
          payload_ptr,
          payload_len);

  if (raw_len == 0) {
#if BRIDGE_DEBUG_FRAMES
    _tx_debug.build_failures++;
#endif
    return false;
  }

#if BRIDGE_DEBUG_FRAMES
  _tx_debug.command_id = command_id;
  _tx_debug.payload_length = static_cast<uint16_t>(payload_len);
  _tx_debug.raw_length = static_cast<uint16_t>(raw_len);
#endif

  uint8_t* cobs_buf = _last_cobs_frame;
  size_t cobs_len = cobs::encode(raw_frame_buf, raw_len, cobs_buf);

  size_t written = _writeFrameBytes(cobs_buf, cobs_len);
  if (written == cobs_len) {
    const uint8_t terminator = 0x00;
    written += _writeFrameBytes(&terminator, 1);
  }

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

size_t BridgeClass::_writeFrameBytes(const uint8_t* data, size_t length) {
  if (data == nullptr || length == 0) {
    return 0;
  }

#if !defined(ARDUINO)
  // Host builds rely on the injected Stream stub; fall back to a single write
  // so we do not depend on HardwareSerial extensions like availableForWrite().
  (void)_hardware_serial;
  return _stream.write(data, length);
#else
  if (_hardware_serial == nullptr) {
    return _stream.write(data, length);
  }

  size_t total_written = 0;
  unsigned long last_progress = millis();
  unsigned long timeout = _response_timeout_ms;
  if (timeout == 0) {
    timeout = 1;
  }

  while (total_written < length) {
    size_t available = _hardware_serial->availableForWrite();
    if (available == 0) {
      if ((millis() - last_progress) >= timeout) {
        break;
      }
      yield();
      continue;
    }

    size_t chunk = length - total_written;
    if (chunk > available) {
      chunk = available;
    }

    size_t wrote = _hardware_serial->write(data + total_written, chunk);
    if (wrote == 0) {
      if ((millis() - last_progress) >= timeout) {
        break;
      }
      yield();
      continue;
    }

    total_written += wrote;
    last_progress = millis();
  }

  return total_written;
#endif
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

void BridgeClass::_recordLastFrame(uint16_t command_id,
                                   const uint8_t* cobs_frame,
                                   size_t cobs_len) {
  if (cobs_len > sizeof(_last_cobs_frame)) {
    cobs_len = sizeof(_last_cobs_frame);
  }
  if (cobs_frame != _last_cobs_frame) {
    std::copy_n(cobs_frame, cobs_len, _last_cobs_frame);
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
  size_t written = _writeFrameBytes(_last_cobs_frame, _last_cobs_length);
  if (written == _last_cobs_length) {
    const uint8_t terminator = 0x00;
    written += _writeFrameBytes(&terminator, 1);
  }
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
      _status_handler(StatusCode::STATUS_TIMEOUT, nullptr, 0);
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
  _flow_paused = false;
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
      std::span<const uint8_t>(frame.payload, frame.payload_length))) {
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

bool BridgeClass::_enqueuePendingTx(uint16_t command_id, std::span<const uint8_t> payload) {
  if (_pending_tx_count >= kMaxPendingTxFrames) {
    return false;
  }
  size_t payload_len = payload.size();
  if (payload_len > rpc::MAX_PAYLOAD_SIZE) {
    return false;
  }
  uint8_t tail = (_pending_tx_head + _pending_tx_count) % kMaxPendingTxFrames;
  _pending_tx_frames[tail].command_id = command_id;
  _pending_tx_frames[tail].payload_length =
      static_cast<uint16_t>(payload_len);
  if (payload_len > 0) {
    std::copy_n(
        payload.data(),
        payload_len,
        _pending_tx_frames[tail].payload);
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
  // Deprecated: MCU no longer initiates pin reads.
  // No-op.
}

void BridgeClass::requestAnalogRead(uint8_t pin) {
  // Deprecated: MCU no longer initiates pin reads.
  // No-op.
}

void BridgeClass::requestProcessRun(std::string_view command) {
  if (command.empty()) {
    return;
  }
  if (command.size() > rpc::MAX_PAYLOAD_SIZE) {
    _emitStatus(StatusCode::STATUS_ERROR, "process_run_payload_too_large");
    return;
  }
  (void)sendFrame(
      CommandId::CMD_PROCESS_RUN,
      std::span<const uint8_t>(
        reinterpret_cast<const uint8_t*>(command.data()),
        command.size()));
}

void BridgeClass::requestProcessRunAsync(std::string_view command) {
  if (command.empty()) {
    return;
  }
  if (command.size() > rpc::MAX_PAYLOAD_SIZE) {
    _emitStatus(StatusCode::STATUS_ERROR, "process_run_async_payload_too_large");
    return;
  }
  (void)sendFrame(
      CommandId::CMD_PROCESS_RUN_ASYNC,
      std::span<const uint8_t>(
        reinterpret_cast<const uint8_t*>(command.data()),
        command.size()));
}

void BridgeClass::requestProcessPoll(int pid) {
  if (pid < 0) {
    return;
  }

  const uint16_t pid_u16 = static_cast<uint16_t>(pid);
  if (!_pushPendingProcessPid(pid_u16)) {
    _emitStatus(StatusCode::STATUS_ERROR, "process_poll_queue_full");
    return;
  }

  uint8_t pid_payload[2];
  rpc::write_u16_be(pid_payload, pid_u16);
  (void)sendFrame(CommandId::CMD_PROCESS_POLL, std::span<const uint8_t>(pid_payload, 2));
}

void BridgeClass::requestFileSystemRead(std::string_view filePath) {
  if (filePath.empty()) {
    return;
  }
  if (filePath.size() > BridgeClass::kMaxFilePathLength) {
    return;
  }

  uint8_t* payload = _scratch_payload;
  payload[0] = static_cast<uint8_t>(filePath.size());
  std::copy_n(filePath.begin(), filePath.size(), payload + kFileReadLengthPrefix);
  const uint16_t total = static_cast<uint16_t>(
      filePath.size() + kFileReadLengthPrefix);
  (void)sendFrame(CommandId::CMD_FILE_READ, std::span<const uint8_t>(payload, total));
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
  std::copy_n(key, length, _pending_datastore_keys[slot]);
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
  std::copy_n(
      _pending_datastore_keys[slot],
      length,
      key_buffer);
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

/*
 * This file is part of Arduino MCU Ecosystem v2.
 */
#include "Bridge.h"

// --- [SAFETY GUARD START] ---
// CRITICAL: Prevent accidental STL usage on AVR targets (memory fragmentation risk)
#if defined(ARDUINO_ARCH_AVR)
  #if defined(_GLIBCXX_VECTOR) || defined(_GLIBCXX_STRING)
    #error "CRITICAL: STL detected in AVR build. Use standard arrays/pointers only to prevent heap fragmentation."
  #endif
#endif
// --- [SAFETY GUARD END] ---

#ifdef ARDUINO_ARCH_AVR
#include <avr/wdt.h>
#endif

#include <string.h>
// Note: <stdlib.h> removed - not used (no malloc/free/atoi)
// Note: <stdint.h> provided by Arduino.h
#if __has_include(<Crypto.h>)
  #include <Crypto.h>
#else
  #error "Dependencia faltante: Crypto. Ejecute tools/install.sh primero."
#endif
#include <SHA256.h>

#include "arduino/StringUtils.h"
#include "protocol/crc.h"
#include "protocol/rpc_protocol.h"
#include "protocol/security.h"

#ifndef BRIDGE_TEST_NO_GLOBALS
#if defined(HAVE_HWSERIAL1)
BridgeClass Bridge(Serial1);
#else
BridgeClass Bridge(Serial);
#endif
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;
#endif

#if BRIDGE_DEBUG_IO
template <typename ActionText>
static void bridge_debug_log_gpio(ActionText action, uint8_t pin, int16_t value) {
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

// [OPTIMIZATION] PROGMEM error strings with external linkage for cross-file use
extern const char kSerialOverflowMessage[] PROGMEM;
extern const char kProcessRunPayloadTooLarge[] PROGMEM;
extern const char kProcessRunAsyncPayloadTooLarge[] PROGMEM;
extern const char kProcessPollQueueFull[] PROGMEM;
extern const char kDatastoreQueueFull[] PROGMEM;

const char kSerialOverflowMessage[] PROGMEM = "serial_rx_overflow";
const char kProcessRunPayloadTooLarge[] PROGMEM = "process_run_payload_too_large";
const char kProcessRunAsyncPayloadTooLarge[] PROGMEM = "process_run_async_payload_too_large";
const char kProcessPollQueueFull[] PROGMEM = "process_poll_queue_full";
const char kDatastoreQueueFull[] PROGMEM = "datastore_queue_full";

namespace {
constexpr size_t kHandshakeTagSize = rpc::RPC_HANDSHAKE_TAG_LENGTH;
static_assert(
  kHandshakeTagSize > 0,
  "RPC_HANDSHAKE_TAG_LENGTH must be greater than zero"
);
constexpr size_t kSha256DigestSize = 32;

#if defined(ARDUINO_ARCH_AVR)
extern "C" char __heap_start;
extern "C" char* __brkval;
#endif

struct BaudrateChangeState {
  uint32_t pending_baudrate;
  unsigned long change_timestamp_ms;
  
  static constexpr unsigned long kSettleDelayMs = 50;
  
  bool isReady(unsigned long now_ms) const {
    return pending_baudrate > 0 && (now_ms - change_timestamp_ms) > kSettleDelayMs;
  }
  
  void schedule(uint32_t baudrate, unsigned long now_ms) {
    pending_baudrate = baudrate;
    change_timestamp_ms = now_ms;
  }
  
  void clear() {
    pending_baudrate = 0;
  }
};

static BaudrateChangeState g_baudrate_state = {0, 0};

uint16_t getFreeMemory() {
#if defined(ARDUINO_ARCH_AVR)
  char stack_top;
  char* heap_end = __brkval ? __brkval : &__heap_start;
  intptr_t free_bytes = &stack_top - heap_end;
  if (free_bytes < 0) {
    free_bytes = 0;
  }
  if (static_cast<size_t>(free_bytes) > rpc::RPC_UINT16_MAX) {
    free_bytes = rpc::RPC_UINT16_MAX;
  }
  return static_cast<uint16_t>(free_bytes);
#else
  return 0;
#endif
}

}

BridgeClass::BridgeClass(HardwareSerial& arg_serial)
    : _transport(arg_serial, &arg_serial),
      _shared_secret(nullptr),
      _shared_secret_len(0),
      _rx_frame{},
      _awaiting_ack(false),
      _last_command_id(0),
      _retry_count(0),
      _last_send_millis(0),
    _last_rx_crc(0),
    _last_rx_crc_millis(0),
      _ack_timeout_ms(rpc::RPC_DEFAULT_ACK_TIMEOUT_MS),
      _ack_retry_limit(rpc::RPC_DEFAULT_RETRY_LIMIT),
      _response_timeout_ms(rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS),
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
  for (uint8_t i = 0; i < rpc::RPC_MAX_PENDING_TX_FRAMES; i++) {
    _pending_tx_frames[i].command_id = 0;
    _pending_tx_frames[i].payload_length = 0;
    memset(_pending_tx_frames[i].payload, 0, rpc::MAX_PAYLOAD_SIZE);
  }
}

BridgeClass::BridgeClass(Stream& arg_stream)
    : _transport(arg_stream, nullptr),
      _shared_secret(nullptr),
      _shared_secret_len(0),
      _rx_frame{},
      _awaiting_ack(false),
      _last_command_id(0),
      _retry_count(0),
      _last_send_millis(0),
    _last_rx_crc(0),
    _last_rx_crc_millis(0),
      _ack_timeout_ms(rpc::RPC_DEFAULT_ACK_TIMEOUT_MS),
      _ack_retry_limit(rpc::RPC_DEFAULT_RETRY_LIMIT),
      _response_timeout_ms(rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS),
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
  for (uint8_t i = 0; i < rpc::RPC_MAX_PENDING_TX_FRAMES; i++) {
    _pending_tx_frames[i].command_id = 0;
    _pending_tx_frames[i].payload_length = 0;
    memset(_pending_tx_frames[i].payload, 0, rpc::MAX_PAYLOAD_SIZE);
  }
}

void BridgeClass::begin(
    unsigned long arg_baudrate, const char* arg_secret, size_t arg_secret_len) {
  _transport.begin(arg_baudrate);

  const unsigned long start = millis();
  unsigned long last = start;
  uint16_t spins = 0;
  while ((millis() - start) < 100 && spins < 1000U) {
    _transport.flushRx();
    spins++;
    const unsigned long now = millis();
    if (now == last && spins >= 10U) {
      break;
    }
    last = now;
  }

  _shared_secret = reinterpret_cast<const uint8_t*>(arg_secret);
  if (_shared_secret && arg_secret_len > 0) {
    _shared_secret_len = arg_secret_len;
  } else if (_shared_secret) {
    _shared_secret_len = strlen(arg_secret);
  } else {
    _shared_secret_len = 0;
  }

  _awaiting_ack = false;
  _last_command_id = 0;
  _retry_count = 0;
  _last_send_millis = 0;
  _last_rx_crc = 0;
  _last_rx_crc_millis = 0;
#if BRIDGE_DEBUG_FRAMES
  _tx_debug = {};
#endif

#ifndef BRIDGE_TEST_NO_GLOBALS
  while (!_synchronized) {
    process();
  }
#endif
}

bool BridgeClass::_isRecentDuplicateRx(const rpc::Frame& frame) const {
  const uint16_t payload_len = frame.header.payload_length;
  if (payload_len > rpc::MAX_PAYLOAD_SIZE) {
    return false;
  }

  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE];
  rpc::FrameBuilder builder;
  const size_t raw_len = builder.build(
      raw,
      sizeof(raw),
      frame.header.command_id,
      frame.payload,
      payload_len);

  if (raw_len < rpc::CRC_TRAILER_SIZE) {
    return false;
  }

  const uint32_t crc = rpc::read_u32_be(&raw[raw_len - rpc::CRC_TRAILER_SIZE]);
  if (_last_rx_crc == 0 || crc != _last_rx_crc) {
    return false;
  }

  const unsigned long now = millis();
  const unsigned long elapsed = now - _last_rx_crc_millis;

  if (_ack_timeout_ms > 0 && elapsed < static_cast<unsigned long>(_ack_timeout_ms)) {
    return false;
  }

  const unsigned long window_ms =
      static_cast<unsigned long>(_ack_timeout_ms) *
      static_cast<unsigned long>(_ack_retry_limit + 1);

  return elapsed <= window_ms;
}

void BridgeClass::_markRxProcessed(const rpc::Frame& frame) {
  const uint16_t payload_len = frame.header.payload_length;
  if (payload_len > rpc::MAX_PAYLOAD_SIZE) {
    return;
  }

  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE];
  rpc::FrameBuilder builder;
  const size_t raw_len = builder.build(
      raw,
      sizeof(raw),
      frame.header.command_id,
      frame.payload,
      payload_len);
  if (raw_len < rpc::CRC_TRAILER_SIZE) {
    return;
  }

  _last_rx_crc = rpc::read_u32_be(&raw[raw_len - rpc::CRC_TRAILER_SIZE]);
  _last_rx_crc_millis = millis();
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
  rpc::security::secure_zero(digest, kSha256DigestSize);
}

void BridgeClass::_applyTimingConfig(const uint8_t* payload, size_t length) {
  uint16_t ack_timeout_ms = rpc::RPC_DEFAULT_ACK_TIMEOUT_MS;
  uint8_t retry_limit = rpc::RPC_DEFAULT_RETRY_LIMIT;
  uint32_t response_timeout_ms = rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS;

  if (payload != nullptr && length >= rpc::RPC_HANDSHAKE_CONFIG_SIZE) {
    const uint8_t* cursor = payload;
    ack_timeout_ms = rpc::read_u16_be(cursor);
    cursor += 2;
    retry_limit = *cursor++;
    response_timeout_ms = rpc::read_u32_be(cursor);
  }

    _ack_timeout_ms = (ack_timeout_ms >= rpc::RPC_HANDSHAKE_ACK_TIMEOUT_MIN_MS &&
                ack_timeout_ms <= rpc::RPC_HANDSHAKE_ACK_TIMEOUT_MAX_MS)
                        ? ack_timeout_ms
                  : rpc::RPC_DEFAULT_ACK_TIMEOUT_MS;

    _ack_retry_limit = (retry_limit >= rpc::RPC_HANDSHAKE_RETRY_LIMIT_MIN &&
                 retry_limit <= rpc::RPC_HANDSHAKE_RETRY_LIMIT_MAX)
                         ? retry_limit
                   : rpc::RPC_DEFAULT_RETRY_LIMIT;

  _response_timeout_ms =
      (response_timeout_ms >= rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS &&
       response_timeout_ms <= rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS)
          ? response_timeout_ms
         : rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS;
}

void BridgeClass::onCommand(CommandHandler handler) { _command_handler = handler; }
void BridgeClass::onDigitalReadResponse(DigitalReadHandler handler) { _digital_read_handler = handler; }
void BridgeClass::onAnalogReadResponse(AnalogReadHandler handler) { _analog_read_handler = handler; }
void BridgeClass::onGetFreeMemoryResponse(GetFreeMemoryHandler handler) { _get_free_memory_handler = handler; }
void BridgeClass::onStatus(StatusHandler handler) { _status_handler = handler; }

void BridgeClass::process() {
  if (g_baudrate_state.isReady(millis())) {
    _transport.setBaudrate(g_baudrate_state.pending_baudrate);
    g_baudrate_state.clear();
  }

#if defined(ARDUINO_ARCH_AVR)
  if (kBridgeEnableWatchdog) {
    wdt_reset();
  }
#elif defined(ARDUINO_ARCH_ESP32)
  if (kBridgeEnableWatchdog) {
    esp_task_wdt_reset();
  }
#elif defined(ARDUINO_ARCH_ESP8266)
  if (kBridgeEnableWatchdog) {
    yield();
  }
#endif

  if (_transport.processInput(_rx_frame)) {
    dispatch(_rx_frame);
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
      _transport.clearOverflow();
    }
  }

  _processAckTimeout();
  _flushPendingTxQueue();
  Console.flush(); 
}

void BridgeClass::flushStream() {
  _transport.flush();
}

void BridgeClass::_handleSystemCommand(const rpc::Frame& frame) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(frame.header.command_id);
  const size_t payload_length = frame.header.payload_length;
  const uint8_t* payload_data = frame.payload;

  switch (command) {
    case rpc::CommandId::CMD_GET_VERSION:
      if (payload_length == 0) {
        uint8_t version_payload[2];
        version_payload[0] = static_cast<uint8_t>(kDefaultFirmwareVersionMajor);
        version_payload[1] = static_cast<uint8_t>(kDefaultFirmwareVersionMinor);
        (void)sendFrame(rpc::CommandId::CMD_GET_VERSION_RESP, version_payload, sizeof(version_payload));
      }
      break;
    case rpc::CommandId::CMD_GET_FREE_MEMORY:
      if (payload_length == 0) {
        uint16_t free_mem = getFreeMemory();
        uint8_t resp_payload[2];
        rpc::write_u16_be(resp_payload, free_mem);
        (void)sendFrame(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, resp_payload, 2);
      }
      break;
    case rpc::CommandId::CMD_SET_BAUDRATE:
      if (payload_length == 4) {
        uint32_t new_baud = rpc::read_u32_be(payload_data);
        (void)sendFrame(rpc::CommandId::CMD_SET_BAUDRATE_RESP, nullptr, 0);
        _transport.flush();
        g_baudrate_state.schedule(new_baud, millis());
      }
      break;
    case rpc::CommandId::CMD_LINK_SYNC:
      {
        const size_t nonce_length = payload_length;
        if (nonce_length != rpc::RPC_HANDSHAKE_NONCE_LENGTH) break;
        
        _resetLinkState();
        Console.begin();
        const bool has_secret = (_shared_secret_len > 0);
        const size_t response_length = static_cast<size_t>(nonce_length) + (has_secret ? kHandshakeTagSize : 0);
        
        if (response_length > rpc::MAX_PAYLOAD_SIZE) {
          (void)sendFrame(rpc::StatusCode::STATUS_MALFORMED);
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
          (void)sendFrame(rpc::CommandId::CMD_LINK_SYNC_RESP, response, response_length);
          _synchronized = true;
        }
      }
      break;
    case rpc::CommandId::CMD_LINK_RESET:
      if (payload_length == 0 || payload_length == rpc::RPC_HANDSHAKE_CONFIG_SIZE) {
        _resetLinkState();
        _applyTimingConfig(payload_data, payload_length);
        Console.begin();
        (void)sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP);
      }
      break;
    default:
      break;
  }
}

void BridgeClass::_handleGpioCommand(const rpc::Frame& frame) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(frame.header.command_id);
  const size_t payload_length = frame.header.payload_length;
  const uint8_t* payload_data = frame.payload;

  if (!payload_data) return;

  switch (command) {
    case rpc::CommandId::CMD_SET_PIN_MODE:
      if (payload_length == 2) {
        uint8_t pin = payload_data[0];
        uint8_t mode = payload_data[1];
        ::pinMode(pin, mode);
        #if BRIDGE_DEBUG_IO
        if (kBridgeDebugIo) bridge_debug_log_gpio(F("pinMode"), pin, mode);
        #endif
      }
      break;
    case rpc::CommandId::CMD_DIGITAL_WRITE:
      if (payload_length == 2) {
        uint8_t pin = payload_data[0];
        uint8_t value = payload_data[1] ? HIGH : LOW;
        ::digitalWrite(pin, value);
        #if BRIDGE_DEBUG_IO
        if (kBridgeDebugIo) bridge_debug_log_gpio(F("digitalWrite"), pin, value == HIGH ? 1 : 0);
        #endif
      }
      break;
    case rpc::CommandId::CMD_ANALOG_WRITE:
      if (payload_length == 2) {
        ::analogWrite(payload_data[0], static_cast<int>(payload_data[1]));
      }
      break;
    case rpc::CommandId::CMD_DIGITAL_READ:
      if (payload_length == 1) {
        uint8_t pin = payload_data[0];
        int16_t value = ::digitalRead(pin);
        #if BRIDGE_DEBUG_IO
        if (kBridgeDebugIo) bridge_debug_log_gpio(F("digitalRead"), pin, value);
        #endif
        uint8_t resp_payload = static_cast<uint8_t>(value & rpc::RPC_UINT8_MASK);
        (void)sendFrame(rpc::CommandId::CMD_DIGITAL_READ_RESP, &resp_payload, 1);
      }
      break;
    case rpc::CommandId::CMD_ANALOG_READ:
      if (payload_length == 1) {
        uint8_t pin = payload_data[0];
        int16_t value = ::analogRead(pin);
        #if BRIDGE_DEBUG_IO
        if (kBridgeDebugIo) bridge_debug_log_gpio(F("analogRead"), pin, value);
        #endif
        uint8_t resp_payload[2];
        rpc::write_u16_be(resp_payload, static_cast<uint16_t>(value & rpc::RPC_UINT16_MAX));
        (void)sendFrame(rpc::CommandId::CMD_ANALOG_READ_RESP, resp_payload, sizeof(resp_payload));
      }
      break;
    default:
      break;
  }
}

void BridgeClass::_handleConsoleCommand(const rpc::Frame& frame) {
  if (static_cast<rpc::CommandId>(frame.header.command_id) == rpc::CommandId::CMD_CONSOLE_WRITE) {
    Console._push(frame.payload, frame.header.payload_length);
  }
}

void BridgeClass::dispatch(const rpc::Frame& frame) {
  const uint16_t raw_command = frame.header.command_id;
  const rpc::CommandId command = static_cast<rpc::CommandId>(raw_command);
  
  DataStore.handleResponse(frame);
  Mailbox.handleResponse(frame);
  FileSystem.handleResponse(frame);
  Process.handleResponse(frame);
  
  bool command_processed_internally = false;
  bool requires_ack = false;

  bool is_system_command = false;
  
  if (raw_command >= rpc::RPC_SYSTEM_COMMAND_MIN && raw_command <= rpc::RPC_SYSTEM_COMMAND_MAX) {
      is_system_command = true;
  }

  if (is_system_command) {
      if (command == rpc::CommandId::CMD_LINK_RESET) {
          if (_isRecentDuplicateRx(frame)) {
            _sendAckAndFlush(raw_command);
            return;
          }
          _sendAckAndFlush(raw_command);
          
          _handleSystemCommand(frame);
          command_processed_internally = true;
          requires_ack = false;
      } else {
          _handleSystemCommand(frame);
          command_processed_internally = true;
          
          if (command == rpc::CommandId::CMD_LINK_SYNC) {
              requires_ack = true;
          } else {
              requires_ack = false;
          }
      }
    } else if (raw_command >= rpc::RPC_GPIO_COMMAND_MIN) {
      switch(command) {
        case rpc::CommandId::CMD_SET_PIN_MODE:
        case rpc::CommandId::CMD_DIGITAL_WRITE:
        case rpc::CommandId::CMD_ANALOG_WRITE:
          if (_isRecentDuplicateRx(frame)) {
            _sendAckAndFlush(raw_command);
            return;
          }
          _handleGpioCommand(frame);
          _markRxProcessed(frame);
          command_processed_internally = true;
          requires_ack = true;
          break;
        case rpc::CommandId::CMD_DIGITAL_READ:
        case rpc::CommandId::CMD_ANALOG_READ:
          if (_isRecentDuplicateRx(frame)) {
            return;
          }
          _handleGpioCommand(frame);
          _markRxProcessed(frame);
          command_processed_internally = true;
          requires_ack = false;
          break;
        case rpc::CommandId::CMD_CONSOLE_WRITE:
          if (_isRecentDuplicateRx(frame)) {
            _sendAckAndFlush(raw_command);
            return;
          }
          _handleConsoleCommand(frame);
          _markRxProcessed(frame);
          command_processed_internally = true;
          requires_ack = true;
          break;
        case rpc::CommandId::CMD_MAILBOX_PUSH:
          if (_isRecentDuplicateRx(frame)) {
            _sendAckAndFlush(raw_command);
            return;
          }
          Mailbox.handleResponse(frame); 
          _markRxProcessed(frame);
          command_processed_internally = true;
          requires_ack = true;
          break;
        case rpc::CommandId::CMD_FILE_WRITE:
          if (_isRecentDuplicateRx(frame)) {
            _sendAckAndFlush(raw_command);
            return;
          }
          FileSystem.handleResponse(frame);
          _markRxProcessed(frame);
          command_processed_internally = true;
          requires_ack = true;
          break;
        
        case rpc::CommandId::CMD_DATASTORE_GET_RESP:
        case rpc::CommandId::CMD_MAILBOX_READ_RESP:
        case rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP:
        case rpc::CommandId::CMD_FILE_READ_RESP:
        case rpc::CommandId::CMD_PROCESS_RUN_RESP:
        case rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP:
        case rpc::CommandId::CMD_PROCESS_POLL_RESP:
        case rpc::CommandId::CMD_LINK_SYNC_RESP:
          command_processed_internally = true;
          requires_ack = false; 
          break;

        default:
          break;
      }
  }

  if (requires_ack) {
    uint8_t ack_payload[2];
    rpc::write_u16_be(ack_payload, raw_command);
    (void)sendFrame(rpc::StatusCode::STATUS_ACK, ack_payload, sizeof(ack_payload));
  }

  if (!command_processed_internally) {
      if (raw_command >= rpc::RPC_STATUS_CODE_MIN && raw_command <= rpc::RPC_STATUS_CODE_MAX) {
        
        const rpc::StatusCode status = static_cast<rpc::StatusCode>(raw_command);
        const size_t payload_length = frame.header.payload_length;
        const uint8_t* payload_data = frame.payload;
        
        switch (status) {
          case rpc::StatusCode::STATUS_ACK: {
            uint16_t ack_id = rpc::RPC_INVALID_ID_SENTINEL;
            if (payload_length >= 2 && payload_data) {
              ack_id = rpc::read_u16_be(payload_data);
            }
            _handleAck(ack_id);
            break;
          }
          case rpc::StatusCode::STATUS_MALFORMED: {
            uint16_t malformed_id = rpc::RPC_INVALID_ID_SENTINEL;
            if (payload_length >= 2 && payload_data) {
              malformed_id = rpc::read_u16_be(payload_data);
            }
            _handleMalformed(malformed_id);
            break;
          }
          default:
            break;
        }
        
        if (_status_handler) {
          _status_handler(status, payload_data, static_cast<uint16_t>(payload_length));
        }
        return;
      }
  }

  if (!command_processed_internally && _command_handler) {
    _command_handler(frame);
  } else if (!command_processed_internally) {
    if (raw_command < rpc::RPC_STATUS_CODE_MIN || raw_command > rpc::RPC_STATUS_CODE_MAX) {
        (void)sendFrame(rpc::StatusCode::STATUS_CMD_UNKNOWN);
    }
  }
}

void BridgeClass::_emitStatus(rpc::StatusCode status_code, const char* message) {
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

void BridgeClass::_emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message) {
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

bool BridgeClass::sendFrame(rpc::CommandId command_id, const uint8_t* arg_payload, size_t arg_length) {
  return _sendFrame(rpc::to_underlying(command_id), arg_payload, arg_length);
}

bool BridgeClass::sendFrame(rpc::StatusCode status_code, const uint8_t* arg_payload, size_t arg_length) {
  return _sendFrame(rpc::to_underlying(status_code), arg_payload, arg_length);
}

bool BridgeClass::_sendFrame(uint16_t command_id, const uint8_t* arg_payload, size_t arg_length) {
  if (!_synchronized) {
    bool allowed = (command_id <= rpc::RPC_SYSTEM_COMMAND_MAX) ||
                   (command_id == rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION_RESP)) ||
                   (command_id == rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC_RESP)) ||
                   (command_id == rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET_RESP));
    if (!allowed) {
      return false;
    }
  }

  if (!_requiresAck(command_id)) {
    return _sendFrameImmediate(command_id, arg_payload, arg_length);
  }

  if (_awaiting_ack) {
    if (_enqueuePendingTx(command_id, arg_payload, arg_length)) {
      return true;
    }
    _processAckTimeout();
    if (!_awaiting_ack && _enqueuePendingTx(command_id, arg_payload, arg_length)) {
      return true;
    }
    return false;
  }

  return _sendFrameImmediate(command_id, arg_payload, arg_length);
}

bool BridgeClass::_sendFrameImmediate(uint16_t command_id,
                                      const uint8_t* arg_payload, size_t arg_length) {
  bool success = _transport.sendFrame(command_id, arg_payload, arg_length);

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
  if (command_id >= rpc::RPC_STATUS_CODE_MIN && command_id <= rpc::RPC_STATUS_CODE_MAX) {
      return false;
  }
  if (command_id == rpc::to_underlying(rpc::CommandId::CMD_XOFF) ||
    command_id == rpc::to_underlying(rpc::CommandId::CMD_XON)) {
      return false;
  }

  switch (command_id) {
    case rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE):
    case rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE):
    case rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE):
    case rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE):
    case rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_PUT):
    case rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH):
    case rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE):
      return true;
    default:
      return false;
  }
}

void BridgeClass::_clearAckState() {
  _awaiting_ack = false;
  _retry_count = 0;
}

void BridgeClass::_handleAck(uint16_t command_id) {
  if (!_awaiting_ack) {
    return;
  }
  if (command_id == rpc::RPC_INVALID_ID_SENTINEL || command_id == _last_command_id) {
    _clearAckState();
    _flushPendingTxQueue();
  }
}

void BridgeClass::_handleMalformed(uint16_t command_id) {
  if (command_id == rpc::RPC_INVALID_ID_SENTINEL || command_id == _last_command_id) {
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
      _status_handler(rpc::StatusCode::STATUS_TIMEOUT, nullptr, 0);
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
}

void BridgeClass::_sendAckAndFlush(uint16_t command_id) {
  uint8_t ack_payload[2];
  rpc::write_u16_be(ack_payload, command_id);
  (void)sendFrame(rpc::StatusCode::STATUS_ACK, ack_payload, sizeof(ack_payload));
  _transport.flush();
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
      frame.payload, frame.payload_length)) {
    uint8_t previous_head =
        (_pending_tx_head + rpc::RPC_MAX_PENDING_TX_FRAMES - 1) %
        rpc::RPC_MAX_PENDING_TX_FRAMES;
    _pending_tx_head = previous_head;
    _pending_tx_frames[_pending_tx_head] = frame;
    _pending_tx_count++;
  }
}

void BridgeClass::_clearPendingTxQueue() {
  _pending_tx_head = 0;
  _pending_tx_count = 0;
}

bool BridgeClass::_enqueuePendingTx(uint16_t command_id, const uint8_t* arg_payload, size_t arg_length) {
  if (_pending_tx_count >= rpc::RPC_MAX_PENDING_TX_FRAMES) {
    return false;
  }
  size_t payload_len = arg_length;
  if (payload_len > rpc::MAX_PAYLOAD_SIZE) {
    return false;
  }
  uint8_t tail = (_pending_tx_head + _pending_tx_count) %
      rpc::RPC_MAX_PENDING_TX_FRAMES;
  _pending_tx_frames[tail].command_id = command_id;
  _pending_tx_frames[tail].payload_length =
      static_cast<uint16_t>(payload_len);
  if (payload_len > 0) {
    memcpy(_pending_tx_frames[tail].payload, arg_payload, payload_len);
  }
  _pending_tx_count++;
  return true;
}

bool BridgeClass::_dequeuePendingTx(PendingTxFrame& frame) {
  if (_pending_tx_count == 0) {
    return false;
  }
  frame = _pending_tx_frames[_pending_tx_head];
  _pending_tx_head = (_pending_tx_head + 1) % rpc::RPC_MAX_PENDING_TX_FRAMES;
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
  uint8_t val_u8 = static_cast<uint8_t>(constrain(value, static_cast<int>(rpc::RPC_DIGITAL_LOW), static_cast<int>(rpc::RPC_UINT8_MASK)));
  ::analogWrite(pin, static_cast<int>(val_u8));
}
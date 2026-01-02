/*
 * This file is part of Arduino Yun Ecosystem v2.
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
#include <stdlib.h>
#include <stdint.h>
#include <stdio.h> // Added for debug prints
#if __has_include(<Crypto.h>)
  #include <Crypto.h>
#else
  #error "Dependencia faltante: Crypto. Ejecute tools/install.sh primero."
#endif
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

// Variables estáticas para gestión no bloqueante del cambio de baudrate
static uint32_t s_pending_baudrate = 0;
static unsigned long s_baudrate_change_timestamp = 0;

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

BridgeClass::BridgeClass(HardwareSerial& serial)
    : _transport(serial, &serial),
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
  for (int i = 0; i < rpc::RPC_MAX_PENDING_TX_FRAMES; i++) {
    _pending_tx_frames[i].command_id = 0;
    _pending_tx_frames[i].payload_length = 0;
    memset(_pending_tx_frames[i].payload, 0, rpc::MAX_PAYLOAD_SIZE);
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
    _last_rx_crc(0),
    _last_rx_crc_millis(0),
      _ack_timeout_ms(rpc::RPC_DEFAULT_ACK_TIMEOUT_MS),
      _ack_retry_limit(rpc::RPC_DEFAULT_RETRY_LIMIT),
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
  for (int i = 0; i < rpc::RPC_MAX_PENDING_TX_FRAMES; i++) {
    _pending_tx_frames[i].command_id = 0;
    _pending_tx_frames[i].payload_length = 0;
    memset(_pending_tx_frames[i].payload, 0, rpc::MAX_PAYLOAD_SIZE);
  }
}

void BridgeClass::begin(
    unsigned long baudrate, const char* secret, size_t secret_len) {
  _transport.begin(baudrate);

// [FIX] Omitir el delay de purgado en los tests de host
#ifndef BRIDGE_HOST_TEST
  // [HARDENING] Flush RX buffer to remove bootloader garbage or Linux console noise.
  // Uses the new transport-level flushRx() method.
  unsigned long start = millis();
  while (millis() - start < 100) {
    _transport.flushRx();
  }
#endif

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

  // Fingerprint: CRC32 of the raw frame (header + payload) as used on-wire.
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

  // Retries happen after the sender waits for an ACK timeout.
  // If the same frame arrives *before* that timeout, treat it as a new command
  // to reduce accidental suppression of legitimate repeated operations.
  if (_ack_timeout_ms > 0 && elapsed < static_cast<unsigned long>(_ack_timeout_ms)) {
    return false;
  }

  // Accept duplicates only within the expected retry horizon.
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
}

void BridgeClass::_applyTimingConfig(const uint8_t* payload, size_t length) {
  // Default values
  uint16_t ack_timeout_ms = rpc::RPC_DEFAULT_ACK_TIMEOUT_MS;
  uint8_t retry_limit = rpc::RPC_DEFAULT_RETRY_LIMIT;
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
                  : rpc::RPC_DEFAULT_ACK_TIMEOUT_MS;

  _ack_retry_limit = (retry_limit >= RPC_HANDSHAKE_RETRY_LIMIT_MIN &&
                      retry_limit <= RPC_HANDSHAKE_RETRY_LIMIT_MAX)
                         ? retry_limit
                   : rpc::RPC_DEFAULT_RETRY_LIMIT;

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
// [HARDENING] Máquina de estados para cambio de baudrate NO BLOQUEANTE
  if (s_pending_baudrate > 0) {
    if (millis() - s_baudrate_change_timestamp > 50) {
      _transport.setBaudrate(s_pending_baudrate);
      s_pending_baudrate = 0;
    }
  }

#if defined(ARDUINO_ARCH_AVR)
  if (kBridgeEnableWatchdog) {
    wdt_reset();
  }
#endif

  // Handle incoming data via transport
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
        uint8_t version_payload[2];
        version_payload[0] = static_cast<uint8_t>(kDefaultFirmwareVersionMajor);
        version_payload[1] = static_cast<uint8_t>(kDefaultFirmwareVersionMinor);
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
        // [OPTIMIZATION] Enviar ACK y programar cambio diferido sin bloquear la CPU
        (void)sendFrame(CommandId::CMD_SET_BAUDRATE_RESP, nullptr, 0);
        _transport.flush();
        
        // Iniciamos el temporizador para cambiar la velocidad en el próximo ciclo process()
        // dando tiempo a que el ACK salga físicamente del UART.
        s_pending_baudrate = new_baud;
        s_baudrate_change_timestamp = millis();
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
        uint8_t resp_payload = static_cast<uint8_t>(value & rpc::RPC_UINT8_MASK);
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
        rpc::write_u16_be(resp_payload, static_cast<uint16_t>(value & rpc::RPC_UINT16_MAX));
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
  // These calls update internal state but do not set 'command_processed_internally' automatically here.
  DataStore.handleResponse(frame);
  Mailbox.handleResponse(frame);
  FileSystem.handleResponse(frame);
  Process.handleResponse(frame);
  
  // 2. Handle Commands (Linux -> MCU)
  bool command_processed_internally = false;
  bool requires_ack = false;

  bool is_system_command = false;
  
  // System commands are in [rpc::RPC_SYSTEM_COMMAND_MIN, rpc::RPC_SYSTEM_COMMAND_MAX].
  // Status codes are in [rpc::RPC_STATUS_CODE_MIN, rpc::RPC_STATUS_CODE_MAX].
  if (raw_command >= rpc::RPC_SYSTEM_COMMAND_MIN && raw_command <= rpc::RPC_SYSTEM_COMMAND_MAX) {
      is_system_command = true;
  }

  if (is_system_command) {
      if (command == CommandId::CMD_LINK_RESET) {
          if (_isRecentDuplicateRx(frame)) {
            uint8_t ack_payload[2];
            rpc::write_u16_be(ack_payload, raw_command);
            (void)sendFrame(StatusCode::STATUS_ACK, ack_payload, sizeof(ack_payload));
            return;
          }
          // [FIX] Send ACK immediately before reset destroys state
          uint8_t ack_payload[2];
          rpc::write_u16_be(ack_payload, raw_command);
          (void)sendFrame(StatusCode::STATUS_ACK, ack_payload, sizeof(ack_payload));
          _transport.flush();
          
          _handleSystemCommand(frame);
          command_processed_internally = true;
          requires_ack = false;
      } else {
          // Other system commands
          _handleSystemCommand(frame);
          command_processed_internally = true;
          
          // [FIX] CMD_LINK_SYNC requires specific ACK + Response frame
          if (command == CommandId::CMD_LINK_SYNC) {
              requires_ack = true;
          } else {
              requires_ack = false;
          }
      }
    } else if (raw_command >= rpc::RPC_GPIO_COMMAND_MIN) {
      // High-ID commands (GPIO+, Console, etc)
      switch(command) {
        case CommandId::CMD_SET_PIN_MODE:
        case CommandId::CMD_DIGITAL_WRITE:
        case CommandId::CMD_ANALOG_WRITE:
          if (_isRecentDuplicateRx(frame)) {
            uint8_t ack_payload[2];
            rpc::write_u16_be(ack_payload, raw_command);
            (void)sendFrame(StatusCode::STATUS_ACK, ack_payload, sizeof(ack_payload));
            return;
          }
          _handleGpioCommand(frame);
          _markRxProcessed(frame);
          command_processed_internally = true;
          requires_ack = true;
          break;
        case CommandId::CMD_DIGITAL_READ:
        case CommandId::CMD_ANALOG_READ:
          _handleGpioCommand(frame);
          command_processed_internally = true;
          requires_ack = false;
          break;
        case CommandId::CMD_CONSOLE_WRITE:
          if (_isRecentDuplicateRx(frame)) {
            uint8_t ack_payload[2];
            rpc::write_u16_be(ack_payload, raw_command);
            (void)sendFrame(StatusCode::STATUS_ACK, ack_payload, sizeof(ack_payload));
            return;
          }
          _handleConsoleCommand(frame);
          _markRxProcessed(frame);
          command_processed_internally = true;
          requires_ack = true;
          break;
        case CommandId::CMD_MAILBOX_PUSH:
        case CommandId::CMD_MAILBOX_AVAILABLE:
          if (_isRecentDuplicateRx(frame)) {
            uint8_t ack_payload[2];
            rpc::write_u16_be(ack_payload, raw_command);
            (void)sendFrame(StatusCode::STATUS_ACK, ack_payload, sizeof(ack_payload));
            return;
          }
          Mailbox.handleResponse(frame); 
          _markRxProcessed(frame);
          command_processed_internally = true;
          requires_ack = true;
          break;
        case CommandId::CMD_FILE_WRITE:
          if (_isRecentDuplicateRx(frame)) {
            uint8_t ack_payload[2];
            rpc::write_u16_be(ack_payload, raw_command);
            (void)sendFrame(StatusCode::STATUS_ACK, ack_payload, sizeof(ack_payload));
            return;
          }
          FileSystem.handleResponse(frame);
          _markRxProcessed(frame);
          command_processed_internally = true;
          requires_ack = true;
          break;
        
        // Responses (Linux -> MCU)
        case CommandId::CMD_DATASTORE_GET_RESP:
        case CommandId::CMD_MAILBOX_READ_RESP:
        case CommandId::CMD_MAILBOX_AVAILABLE_RESP:
        case CommandId::CMD_FILE_READ_RESP:
        case CommandId::CMD_PROCESS_RUN_RESP:
        case CommandId::CMD_PROCESS_RUN_ASYNC_RESP:
        case CommandId::CMD_PROCESS_POLL_RESP:
        case CommandId::CMD_LINK_SYNC_RESP:
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
    (void)sendFrame(StatusCode::STATUS_ACK, ack_payload, sizeof(ack_payload));
  }

  // Handle Status/Error frames (when not a system command)
  if (!command_processed_internally) {
      // Status codes are in the reserved range declared by the protocol.
      if (raw_command >= rpc::RPC_STATUS_CODE_MIN && raw_command <= rpc::RPC_STATUS_CODE_MAX) {
        
        const StatusCode status = static_cast<StatusCode>(raw_command);
        const size_t payload_length = frame.header.payload_length;
        const uint8_t* payload_data = frame.payload;
        
        switch (status) {
          case StatusCode::STATUS_ACK: {
            uint16_t ack_id = rpc::RPC_INVALID_ID_SENTINEL;
            if (payload_length >= 2 && payload_data) {
              ack_id = rpc::read_u16_be(payload_data);
            }
            _handleAck(ack_id);
            break;
          }
          case StatusCode::STATUS_MALFORMED: {
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
        return; // Done handling status
      }
  }

  // Unknown command
  if (!command_processed_internally && _command_handler) {
    _command_handler(frame);
  } else if (!command_processed_internally) {
    // Only send UNKNOWN if it's not a response we recognized
    // And if it's NOT a status code
    if (raw_command < rpc::RPC_STATUS_CODE_MIN || raw_command > rpc::RPC_STATUS_CODE_MAX) {
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
#ifdef BRIDGE_HOST_TEST
  printf("[Bridge] _sendFrame ID=%u AwaitingAck=%d PendingCount=%d\n", command_id, _awaiting_ack, _pending_tx_count);
#endif
  if (!_synchronized) {
    bool allowed = (command_id <= rpc::RPC_SYSTEM_COMMAND_MAX) ||
                   (command_id == rpc::to_underlying(CommandId::CMD_GET_VERSION_RESP)) ||
                   (command_id == rpc::to_underlying(CommandId::CMD_LINK_SYNC_RESP)) ||
                   (command_id == rpc::to_underlying(CommandId::CMD_LINK_RESET_RESP));
    if (!allowed) {
      return false;
    }
  }

  // [FIX] No encolar comandos que no requieren ACK (como XON/XOFF o Status)
  if (!_requiresAck(command_id)) {
#ifdef BRIDGE_HOST_TEST
    printf("[Bridge] _sendFrame: No ACK required for ID=%u, sending immediate\n", command_id);
#endif
    return _sendFrameImmediate(command_id, payload, length);
  }

  if (_awaiting_ack) {
#ifdef BRIDGE_HOST_TEST
    printf("[Bridge] _sendFrame: Awaiting ACK, enqueuing ID=%u\n", command_id);
#endif
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
#ifdef BRIDGE_HOST_TEST
    printf("[Bridge] _sendFrameImmediate: ID=%u sent, set _awaiting_ack=true\n", command_id);
#endif
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
  // Status codes and Flow Control do NOT require ACK
  if (command_id >= rpc::RPC_STATUS_CODE_MIN && command_id <= rpc::RPC_STATUS_CODE_MAX) {
      return false;
  }
  // XOFF/XON
  if (command_id == rpc::to_underlying(CommandId::CMD_XOFF) ||
    command_id == rpc::to_underlying(CommandId::CMD_XON)) {
      return false;
  }
  // Response frames (e.g. GET_VERSION_RESP) also don't require ACK,
  // but they are usually handled by the 'requires_ack = false' logic in dispatch
  // for incoming frames.
  // For OUTGOING frames, we check this method.
  
  // By default, system commands except flow control might require ACK 
  // (CMD_LINK_SYNC does, CMD_SET_BAUDRATE does in the sense of waiting for it).
  // But strictly, only what's marked 'requires_ack = true' in spec should.
  // However, this C++ helper is simplistic.
  // Let's rely on the exclusion of Status codes.
  
  return true;
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

bool BridgeClass::_enqueuePendingTx(uint16_t command_id, const uint8_t* payload, size_t length) {
  if (_pending_tx_count >= rpc::RPC_MAX_PENDING_TX_FRAMES) {
    return false;
  }
  size_t payload_len = length;
  if (payload_len > rpc::MAX_PAYLOAD_SIZE) {
    return false;
  }
  uint8_t tail = (_pending_tx_head + _pending_tx_count) %
      rpc::RPC_MAX_PENDING_TX_FRAMES;
  _pending_tx_frames[tail].command_id = command_id;
  _pending_tx_frames[tail].payload_length =
      static_cast<uint16_t>(payload_len);
  if (payload_len > 0) {
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

void BridgeClass::requestDigitalRead(uint8_t pin) {
  (void)pin;
}

void BridgeClass::requestAnalogRead(uint8_t pin) {
  (void)pin;
}

void BridgeClass::requestGetFreeMemory() {
  (void)sendFrame(CommandId::CMD_GET_FREE_MEMORY);
}

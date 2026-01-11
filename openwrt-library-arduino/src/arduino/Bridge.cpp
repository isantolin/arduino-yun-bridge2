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
BridgeClass Bridge(Serial1);
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
// These are defined outside the anonymous namespace to allow extern declarations
// Note: 'extern' is required because 'const' variables have internal linkage by default in C++
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

/**
 * @brief Encapsulated state for deferred baudrate changes.
 * 
 * [SIL-2 COMPLIANCE] This structure eliminates global mutable state by
 * encapsulating the pending baudrate change mechanism. The state is only
 * modified through well-defined entry points in BridgeClass::process().
 * 
 * The deferred change pattern is necessary because:
 * 1. The MCU must ACK the baudrate change command at the OLD speed
 * 2. A delay is needed for the ACK to physically leave the UART
 * 3. Only then can the hardware switch to the new speed
 */
struct BaudrateChangeState {
  uint32_t pending_baudrate;         ///< Target baudrate (0 = no change pending)
  unsigned long change_timestamp_ms; ///< millis() when change was requested
  
  static constexpr unsigned long kSettleDelayMs = 50; ///< Delay before applying change
  
  /// Check if a baudrate change is pending and ready to apply
  bool isReady(unsigned long now_ms) const {
    return pending_baudrate > 0 && (now_ms - change_timestamp_ms) > kSettleDelayMs;
  }
  
  /// Schedule a deferred baudrate change
  void schedule(uint32_t baudrate, unsigned long now_ms) {
    pending_baudrate = baudrate;
    change_timestamp_ms = now_ms;
  }
  
  /// Clear the pending change (call after applying)
  void clear() {
    pending_baudrate = 0;
  }
};

/// Singleton instance for baudrate change state
static BaudrateChangeState g_baudrate_state = {0, 0};

/**
 * @brief Calculate free memory on AVR platforms.
 * 
 * [SIL-2] This function provides a safe estimate of available stack/heap
 * space. Returns 0 on non-AVR platforms. The result is clamped to uint16_t
 * range to prevent overflow.
 * 
 * @return Free bytes between stack and heap (0 on non-AVR)
 */
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

  // [HARDENING] Flush RX buffer to remove bootloader garbage or Linux console noise.
  // Host tests may stub millis() to a constant; keep this loop bounded.
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

// ... (existing code) ...

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

  // [FIX] No encolar comandos que no requieren ACK (como XON/XOFF o Status)
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
  // Status codes and Flow Control do NOT require ACK
  if (command_id >= rpc::RPC_STATUS_CODE_MIN && command_id <= rpc::RPC_STATUS_CODE_MAX) {
      return false;
  }
  // XOFF/XON
  if (command_id == rpc::to_underlying(rpc::CommandId::CMD_XOFF) ||
    command_id == rpc::to_underlying(rpc::CommandId::CMD_XON)) {
      return false;
  }

  // Only fire-and-forget commands require ACK.
  // Keep this aligned with the protocol spec / Python ACK_ONLY_COMMANDS.
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

/**
 * @brief Reset link state to unsynchronized (Fail-Safe transition).
 * 
 * [SIL-2 FAIL-SAFE] This function transitions the bridge to a safe state:
 * 1. Sets _synchronized = false (blocks non-system commands)
 * 2. Clears ACK tracking state
 * 3. Empties pending TX queue
 * 4. Resets transport parser state
 * 
 * Called during:
 * - Link reset command processing
 * - Handshake initiation
 * - Fatal error recovery
 */
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

/*
 * This file is part of Arduino MCU Ecosystem v2.
 */
#include "Bridge.h"

// [SIL-2] Explicitly include Arduino.h to satisfy IntelliSense and ensure
// noInterrupts()/interrupts() are available in all compilation contexts.
#include <Arduino.h>

// --- [SAFETY GUARD START] ---
// CRITICAL: Prevent accidental standard STL usage on ALL architectures (memory fragmentation risk)
// SIL 2 Requirement: Dynamic allocation via standard STL containers is forbidden globally.
// We explicitly allow ETL (Embedded Template Library) as it uses static allocation.
#if (defined(_GLIBCXX_VECTOR) || defined(_GLIBCXX_STRING) || defined(_GLIBCXX_MAP)) && !defined(ETL_VERSION) && !defined(BRIDGE_HOST_TEST)
  #error "CRITICAL: Standard STL detected. Use ETL or standard arrays/pointers only to prevent heap fragmentation (SIL 2 Violation)."
#endif
// --- [SAFETY GUARD END] ---

#ifdef ARDUINO_ARCH_AVR
#include <avr/wdt.h>
#endif

#include <string.h>
#if __has_include(<Crypto.h>)
  #include <Crypto.h>
#else
  #error "Dependencia faltante: Crypto. Ejecute tools/install.sh primero."
#endif
#include <SHA256.h>

#include "arduino/StringUtils.h"
#include "protocol/rle.h"
#include "protocol/rpc_protocol.h"
#include "protocol/security.h"
#include "etl/error_handler.h"
#include "etl/algorithm.h"

#ifndef BRIDGE_TEST_NO_GLOBALS
// [SIL-2] Robust Hardware Serial Detection
#if BRIDGE_USE_USB_SERIAL
  BridgeClass Bridge(Serial);
#elif defined(__AVR_ATmega32U4__) || defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM) || defined(_VARIANT_ARDUINO_ZERO_)
  BridgeClass Bridge(Serial1);
#elif defined(HAVE_HWSERIAL1) && !defined(__AVR_ATmega328P__)
  BridgeClass Bridge(Serial1);
#else
  BridgeClass Bridge(Serial);
#endif
#if BRIDGE_ENABLE_DATASTORE
DataStoreClass DataStore;
#endif
#if BRIDGE_ENABLE_MAILBOX
MailboxClass Mailbox;
#endif
#if BRIDGE_ENABLE_FILESYSTEM
FileSystemClass FileSystem;
#endif
#if BRIDGE_ENABLE_PROCESS
ProcessClass Process;
#endif
#endif

// [OPTIMIZATION] Numerical status codes used instead of PROGMEM strings.

namespace {
constexpr size_t kHandshakeTagSize = rpc::RPC_HANDSHAKE_TAG_LENGTH;
static_assert(
  kHandshakeTagSize > 0,
  "RPC_HANDSHAKE_TAG_LENGTH must be greater than zero"
);
constexpr size_t kSha256DigestSize = 32;

struct BaudrateChangeState {
  uint32_t pending_baudrate;
  unsigned long change_timestamp_ms;
  
  static constexpr unsigned long kSettleDelayMs = BRIDGE_BAUDRATE_SETTLE_MS;
  
  bool isReady(unsigned long now_ms) const {
    return pending_baudrate > 0 && (now_ms - change_timestamp_ms) > kSettleDelayMs;
  }

  void schedule(uint32_t baud, unsigned long now_ms) {
    pending_baudrate = baud;
    change_timestamp_ms = now_ms;
  }

  void clear() {
    pending_baudrate = 0;
    change_timestamp_ms = 0;
  }
};

static BaudrateChangeState g_baudrate_state = {0, 0};

// Global instance pointer for PacketSerial static callback
BridgeClass* g_bridge_instance = nullptr;

} // namespace

BridgeClass::BridgeClass(HardwareSerial& arg_serial)
    : BridgeClass(static_cast<Stream&>(arg_serial)) {
  _hardware_serial = &arg_serial;
}

BridgeClass::BridgeClass(Stream& arg_stream)
    : _stream(arg_stream),
      _hardware_serial(nullptr),
      _packetSerial(),
      _shared_secret(),
      _target_frame(nullptr),
      _frame_received(false),
      _parser(),
      _rx_frame{},
      _scratch_payload(),
      _last_command_id(0),
      _retry_count(0),
      _last_send_millis(0),
      _last_rx_crc(0),
      _last_rx_crc_millis(0),
      _consecutive_crc_errors(0),
      _ack_timeout_ms(rpc::RPC_DEFAULT_ACK_TIMEOUT_MS),
      _ack_retry_limit(rpc::RPC_DEFAULT_RETRY_LIMIT),
      _response_timeout_ms(rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS),
      _command_handler(nullptr),
      _digital_read_handler(nullptr),
      _analog_read_handler(nullptr),
      _get_free_memory_handler(nullptr),
      _status_handler(nullptr),
      _pending_tx_queue(),
      _fsm(),
      _last_raw_frame()
#if BRIDGE_DEBUG_FRAMES
      , _tx_debug{}
#endif
{
    g_bridge_instance = this;
}

void BridgeClass::begin(
    unsigned long arg_baudrate, const char* arg_secret, size_t arg_secret_len) {
  
  (void)arg_baudrate; // Ensure usage in all paths
  
  // [SIL-2] Start the ETL FSM before any other initialization
  _fsm.begin();

  // [MIL-SPEC] FIPS 140-3 Power-On Self-Tests (POST)
  if (!rpc::security::run_cryptographic_self_tests()) {
    // CRITICAL: Cryptographic engine is untrustworthy.
    // Enter safe state and disable the bridge to prevent insecure operation.
    enterSafeState();
    _fsm.cryptoFault();  // Transition FSM to Fault state
    return;
  }

  // [SIL-2] USB Serial Initialization Fix
  // On ATmega32U4 (Yun/Leonardo), Serial is USB CDC and acts as a Stream,
  // bypassing the _hardware_serial check below. We must explicitly initialize it.
  #if BRIDGE_USE_USB_SERIAL
    Serial.begin(arg_baudrate);
    // Optional: Wait for host to open the port, but with a timeout to avoid hanging if headless
    // unsigned long timeout = millis();
    // while (!Serial && (millis() - timeout < 3000)); 
  #endif

  if (_hardware_serial != nullptr) {
      _hardware_serial->begin(arg_baudrate);
  }

  // Configure PacketSerial
  _packetSerial.setStream(&_stream);
  _packetSerial.setPacketHandler(onPacketReceived);

  // [SIL-2] Startup Stabilization
  const unsigned long start = millis();
  while ((millis() - start) < BRIDGE_STARTUP_STABILIZATION_MS) {
    while (_stream.available() > 0) { _stream.read(); }
    const unsigned long now = millis();
    if (now == start) break;
  }

  _shared_secret.clear();
  if (arg_secret) {
    size_t actual_len = (arg_secret_len > 0) ? arg_secret_len : strlen(arg_secret);
    if (actual_len > _shared_secret.capacity()) {
      actual_len = _shared_secret.capacity();
    }
    _shared_secret.assign(reinterpret_cast<const uint8_t*>(arg_secret), 
                         reinterpret_cast<const uint8_t*>(arg_secret) + actual_len);
  }

  // [SIL-2] FSM reset to Unsynchronized state
  _fsm.resetFsm();
  _last_command_id = 0;
  _retry_count = 0;
  _last_send_millis = 0;
  _last_rx_crc = 0;
  _last_rx_crc_millis = 0;
  _last_raw_frame.clear();
#if BRIDGE_DEBUG_FRAMES
  _tx_debug = {};
#endif

//#ifndef BRIDGE_TEST_NO_GLOBALS
//  while (_state == BridgeState::Unsynchronized) {
//    process();
//  }
//#endif

}

void BridgeClass::onPacketReceived(const uint8_t* buffer, size_t size) {
    if (g_bridge_instance && g_bridge_instance->_target_frame) {
        if (g_bridge_instance->_parser.parse(buffer, size, *g_bridge_instance->_target_frame)) {
            g_bridge_instance->_frame_received = true;
        }
    } else {
        (void)buffer;
        (void)size;
    }
}

void BridgeClass::process() {
  noInterrupts();
  bool ready = g_baudrate_state.isReady(millis());
  uint32_t new_baud = g_baudrate_state.pending_baudrate;
  interrupts();

  if (ready) {
    if (_hardware_serial != nullptr) {
        _hardware_serial->flush();
        _hardware_serial->end();
        _hardware_serial->begin(new_baud);
    }
    noInterrupts();
    g_baudrate_state.clear();
    interrupts();
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

  // Polling Input Logic
  _target_frame = &_rx_frame;
  _frame_received = false;
  _packetSerial.update();
  _target_frame = nullptr;

  if (_frame_received) {
    _consecutive_crc_errors = 0;
    dispatch(_rx_frame);
  } else {
    rpc::FrameParser::Error error = _parser.getError();
    if (error != rpc::FrameParser::Error::NONE) {
      if (error == rpc::FrameParser::Error::CRC_MISMATCH) {
        _consecutive_crc_errors++;
        if (_consecutive_crc_errors >= BRIDGE_MAX_CONSECUTIVE_CRC_ERRORS) {
          // [SIL-2] Force Hardware Reset after persistent corruption
          #if defined(ARDUINO_ARCH_AVR)
            wdt_enable(WDTO_15MS);
            while(1); 
          #elif defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_ESP8266)
            ESP.restart();
          #else
            enterSafeState();
          #endif
        }
      }
      if (!_fsm.isUnsynchronized()) {
        switch (error) {
          case rpc::FrameParser::Error::CRC_MISMATCH:
            _emitStatus(rpc::StatusCode::STATUS_CRC_MISMATCH, (const char*)nullptr);
            break;
          case rpc::FrameParser::Error::MALFORMED:
            _emitStatus(rpc::StatusCode::STATUS_MALFORMED, (const char*)nullptr);
            break;
          case rpc::FrameParser::Error::OVERFLOW:
            _emitStatus(rpc::StatusCode::STATUS_MALFORMED, (const char*)nullptr);
            break;
          default:
            _emitStatus(rpc::StatusCode::STATUS_ERROR, (const char*)nullptr);
            break;
        }
      }
      _parser.clearError();
    }
  }

  _processAckTimeout();
  _flushPendingTxQueue();
}

void BridgeClass::flushStream() {
  if (_hardware_serial != nullptr) {
      _hardware_serial->flush();
  } else {
      _stream.flush();
  }
}

void BridgeClass::_handleSystemCommand(const rpc::Frame& frame) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(frame.header.command_id);
  const size_t payload_length = frame.header.payload_length;
  const uint8_t* payload_data = frame.payload.data();

  switch (command) {
    case rpc::CommandId::CMD_GET_VERSION:
      if (payload_length == 0) {
        etl::array<uint8_t, 2> version_payload;
        version_payload[0] = static_cast<uint8_t>(kDefaultFirmwareVersionMajor);
        version_payload[1] = static_cast<uint8_t>(kDefaultFirmwareVersionMinor);
        (void)sendFrame(rpc::CommandId::CMD_GET_VERSION_RESP, version_payload.data(), version_payload.size());
      }
      break;
    case rpc::CommandId::CMD_GET_FREE_MEMORY:
      if (payload_length == 0) {
        uint16_t free_mem = getFreeMemory();
        etl::array<uint8_t, 2> resp_payload;
        rpc::write_u16_be(resp_payload.data(), free_mem);
        (void)sendFrame(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, resp_payload.data(), resp_payload.size());
      }
      break;
    case rpc::CommandId::CMD_GET_CAPABILITIES:
      if (payload_length == 0) {
        etl::array<uint8_t, 8> caps;
        caps[0] = rpc::PROTOCOL_VERSION;
        
        uint8_t arch = 0;
        #if defined(ARDUINO_ARCH_AVR)
        arch = 1;
        #elif defined(ARDUINO_ARCH_ESP32)
        arch = 2;
        #elif defined(ARDUINO_ARCH_ESP8266)
        arch = 3;
        #elif defined(ARDUINO_ARCH_SAMD)
        arch = 4;
        #elif defined(ARDUINO_ARCH_SAM)
        arch = 5;
        #endif
        caps[1] = arch;

        #ifdef NUM_DIGITAL_PINS
        caps[2] = static_cast<uint8_t>(NUM_DIGITAL_PINS);
        #else
        caps[2] = 0;
        #endif

        #ifdef NUM_ANALOG_INPUTS
        caps[3] = static_cast<uint8_t>(NUM_ANALOG_INPUTS);
        #else
        caps[3] = 0;
        #endif

        uint32_t features = 0;
        if (kBridgeEnableWatchdog) features |= 1;
        features |= 2; 
        #if BRIDGE_DEBUG_FRAMES
        features |= 4;
        #endif
        #if BRIDGE_DEBUG_IO
        features |= 8;
        #endif

        #if defined(E2END) && (E2END > 0)
        features |= (1 << 4);
        #endif

        #if (defined(DAC_OUTPUT_CHANNELS) && (DAC_OUTPUT_CHANNELS > 0)) || \
            defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM) || defined(ARDUINO_ARCH_ESP32)
        features |= (1 << 5);
        #endif

        #if defined(HAVE_HWSERIAL1)
        features |= (1 << 6);
        #endif

        #if defined(__FPU_PRESENT) && (__FPU_PRESENT == 1)
        features |= (1 << 7);
        #endif

        #if defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM) || \
            defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_ESP8266) || \
            defined(ARDUINO_ARCH_RP2040)
        features |= (1 << 8);
        #endif

        #if defined(SERIAL_RX_BUFFER_SIZE) && (SERIAL_RX_BUFFER_SIZE > 64)
        features |= (1 << 9);
        #endif

        #if defined(PIN_WIRE_SDA) || defined(SDA) || defined(DT) || \
            defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_ESP8266)
        features |= (1 << 10);
        #endif

        rpc::write_u32_be(&caps[4], features);
        
        (void)sendFrame(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, caps.data(), caps.size());
      }
      break;
    case rpc::CommandId::CMD_SET_BAUDRATE:
      if (payload_length == 4) {
        uint32_t new_baud = rpc::read_u32_be(payload_data);
        (void)sendFrame(rpc::CommandId::CMD_SET_BAUDRATE_RESP, nullptr, 0);
        flushStream();
        noInterrupts();
        g_baudrate_state.schedule(new_baud, millis());
        interrupts();
      }
      break;
    case rpc::CommandId::CMD_LINK_SYNC:
      {
        const size_t nonce_length = payload_length;
        if (nonce_length != rpc::RPC_HANDSHAKE_NONCE_LENGTH) break;
        
        enterSafeState();
        const bool has_secret = !_shared_secret.empty();
        const size_t response_length = static_cast<size_t>(nonce_length) + (has_secret ? kHandshakeTagSize : 0);
        
        if (response_length > rpc::MAX_PAYLOAD_SIZE) {
          (void)sendFrame(rpc::StatusCode::STATUS_MALFORMED);
          break;
        }

        uint8_t* response = _scratch_payload.data();
        if (payload_data) {
          etl::copy_n(payload_data, nonce_length, response);
          if (has_secret) {
            uint8_t tag[kHandshakeTagSize];
            _computeHandshakeTag(payload_data, nonce_length, tag);
            etl::copy_n(tag, kHandshakeTagSize, response + nonce_length);
          }
          (void)sendFrame(rpc::CommandId::CMD_LINK_SYNC_RESP, response, response_length);
          // [SIL-2] Handshake complete -> Transition to Idle via FSM
          _fsm.handshakeComplete();
        }
      }
      break;
    case rpc::CommandId::CMD_LINK_RESET:
      if (payload_length == 0 || payload_length == rpc::RPC_HANDSHAKE_CONFIG_SIZE) {
        enterSafeState();
        _applyTimingConfig(payload_data, payload_length);
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
  const uint8_t* payload_data = frame.payload.data();

  if (!payload_data) return;

  switch (command) {
    case rpc::CommandId::CMD_SET_PIN_MODE:
      if (payload_length == 2) {
        uint8_t pin = payload_data[0];
        uint8_t mode = payload_data[1];
#ifdef NUM_DIGITAL_PINS
        if (pin >= NUM_DIGITAL_PINS) return;
#endif
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
#ifdef NUM_DIGITAL_PINS
        if (pin >= NUM_DIGITAL_PINS) return;
#endif
        ::digitalWrite(pin, value);
        #if BRIDGE_DEBUG_IO
        if (kBridgeDebugIo) bridge_debug_log_gpio(F("digitalWrite"), pin, value == HIGH ? 1 : 0);
        #endif
      }
      break;
    case rpc::CommandId::CMD_ANALOG_WRITE:
      if (payload_length == 2) {
        uint8_t pin = payload_data[0];
#ifdef NUM_DIGITAL_PINS
        if (pin >= NUM_DIGITAL_PINS) return;
#endif
        ::analogWrite(pin, static_cast<int>(payload_data[1]));
      }
      break;
    case rpc::CommandId::CMD_DIGITAL_READ:
      if (payload_length == 1) {
        uint8_t pin = payload_data[0];
#ifdef NUM_DIGITAL_PINS
        if (pin >= NUM_DIGITAL_PINS) {
          (void)sendFrame(rpc::StatusCode::STATUS_MALFORMED);
          return;
        }
#endif
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
#ifdef NUM_ANALOG_INPUTS
        if (pin >= NUM_ANALOG_INPUTS) {
          (void)sendFrame(rpc::StatusCode::STATUS_MALFORMED);
          return;
        }
#endif
        int16_t value = ::analogRead(pin);
        #if BRIDGE_DEBUG_IO
        if (kBridgeDebugIo) bridge_debug_log_gpio(F("analogRead"), pin, value);
        #endif
        etl::array<uint8_t, 2> resp_payload;
        rpc::write_u16_be(resp_payload.data(), static_cast<uint16_t>(value & rpc::RPC_UINT16_MAX));
        (void)sendFrame(rpc::CommandId::CMD_ANALOG_READ_RESP, resp_payload.data(), resp_payload.size());
      }
      break;
    default:
      break;
  }
}

void BridgeClass::dispatch(const rpc::Frame& frame) {
  uint16_t raw_command = frame.header.command_id;
  bool is_compressed = (raw_command & rpc::RPC_CMD_FLAG_COMPRESSED) != 0;
  raw_command &= ~rpc::RPC_CMD_FLAG_COMPRESSED;

  rpc::Frame effective_frame = frame;
  effective_frame.header.command_id = raw_command;

  if (is_compressed && frame.header.payload_length > 0) {
    size_t decoded_len = rle::decode(frame.payload.data(), frame.header.payload_length, _scratch_payload.data(), rpc::MAX_PAYLOAD_SIZE);
    if (decoded_len == 0) {
      _emitStatus(rpc::StatusCode::STATUS_MALFORMED, (const char*)nullptr);
      return;
    }
    etl::copy_n(_scratch_payload.data(), decoded_len, effective_frame.payload.data());
    effective_frame.header.payload_length = static_cast<uint16_t>(decoded_len);
  }

  const rpc::CommandId command = static_cast<rpc::CommandId>(raw_command);
  
  #if BRIDGE_ENABLE_DATASTORE
  DataStore.handleResponse(effective_frame);
  #endif
  #if BRIDGE_ENABLE_MAILBOX
  Mailbox.handleResponse(effective_frame);
  #endif
  #if BRIDGE_ENABLE_FILESYSTEM
  FileSystem.handleResponse(effective_frame);
  #endif
  #if BRIDGE_ENABLE_PROCESS
  Process.handleResponse(effective_frame);
  #endif
  
  bool command_processed_internally = false;
  bool requires_ack = false;

  if (raw_command >= rpc::RPC_SYSTEM_COMMAND_MIN && raw_command <= rpc::RPC_SYSTEM_COMMAND_MAX) {
      if (command == rpc::CommandId::CMD_LINK_RESET) {
          if (_isRecentDuplicateRx(effective_frame)) {
            _sendAckAndFlush(raw_command);
            return;
          }
          _sendAckAndFlush(raw_command);
          _handleSystemCommand(effective_frame);
          command_processed_internally = true;
          requires_ack = false;
      } else {
          _handleSystemCommand(effective_frame);
          command_processed_internally = true;
          requires_ack = (command == rpc::CommandId::CMD_LINK_SYNC);
      }
    } else if (raw_command >= rpc::RPC_GPIO_COMMAND_MIN) {
      switch(command) {
        case rpc::CommandId::CMD_SET_PIN_MODE:
        case rpc::CommandId::CMD_DIGITAL_WRITE:
        case rpc::CommandId::CMD_ANALOG_WRITE:
          if (_isRecentDuplicateRx(effective_frame)) {
            _sendAckAndFlush(raw_command);
            return;
          }
          _handleGpioCommand(effective_frame);
          _markRxProcessed(effective_frame);
          command_processed_internally = true;
          requires_ack = true;
          break;
        case rpc::CommandId::CMD_DIGITAL_READ:
        case rpc::CommandId::CMD_ANALOG_READ:
          if (_isRecentDuplicateRx(effective_frame)) return;
          _handleGpioCommand(effective_frame);
          _markRxProcessed(effective_frame);
          command_processed_internally = true;
          requires_ack = false;
          break;
        case rpc::CommandId::CMD_MAILBOX_PUSH:
          if (_isRecentDuplicateRx(effective_frame)) {
            _sendAckAndFlush(raw_command);
            return;
          }
          #if BRIDGE_ENABLE_MAILBOX
          Mailbox.handleResponse(effective_frame); 
          #endif
          _markRxProcessed(effective_frame);
          command_processed_internally = true;
          requires_ack = true;
          break;
        case rpc::CommandId::CMD_FILE_WRITE:
          if (_isRecentDuplicateRx(effective_frame)) {
            _sendAckAndFlush(raw_command);
            return;
          }
          #if BRIDGE_ENABLE_FILESYSTEM
          FileSystem.handleResponse(effective_frame);
          #endif
          _markRxProcessed(effective_frame);
          command_processed_internally = true;
          requires_ack = true;
          break;
        default:
          break;
      }
  }

  if (requires_ack) {
    etl::array<uint8_t, 2> ack_payload;
    rpc::write_u16_be(ack_payload.data(), raw_command);
    (void)sendFrame(rpc::StatusCode::STATUS_ACK, ack_payload.data(), ack_payload.size());
  }

  if (!command_processed_internally) {
      if (raw_command >= rpc::RPC_STATUS_CODE_MIN && raw_command <= rpc::RPC_STATUS_CODE_MAX) {
        const rpc::StatusCode status = static_cast<rpc::StatusCode>(raw_command);
        const size_t payload_length = effective_frame.header.payload_length;
        const uint8_t* payload_data = effective_frame.payload.data();
        
        switch (status) {
          case rpc::StatusCode::STATUS_ACK: {
            uint16_t ack_id = rpc::RPC_INVALID_ID_SENTINEL;
            if (payload_length >= 2 && payload_data) ack_id = rpc::read_u16_be(payload_data);
            _handleAck(ack_id);
            break;
          }
          case rpc::StatusCode::STATUS_MALFORMED: {
            uint16_t malformed_id = rpc::RPC_INVALID_ID_SENTINEL;
            if (payload_length >= 2 && payload_data) malformed_id = rpc::read_u16_be(payload_data);
            _handleMalformed(malformed_id);
            break;
          }
          default:
            break;
        }
        if (_status_handler) _status_handler(status, payload_data, static_cast<uint16_t>(payload_length));
        return;
      }
  }

  if (!command_processed_internally && _command_handler) {
    _command_handler(effective_frame);
  } else if (!command_processed_internally) {
    if (raw_command < rpc::RPC_STATUS_CODE_MIN || raw_command > rpc::RPC_STATUS_CODE_MAX) {
        (void)sendFrame(rpc::StatusCode::STATUS_CMD_UNKNOWN);
    }
  }
}

void BridgeClass::_doEmitStatus(rpc::StatusCode status_code, const uint8_t* payload, uint16_t length) {
  (void)sendFrame(status_code, payload, length);
  if (_status_handler) _status_handler(status_code, payload, length);
}

void BridgeClass::_emitStatus(rpc::StatusCode status_code, const char* message) {
  const uint8_t* payload = nullptr;
  uint16_t length = 0;
  if (message && *message) {
    const auto info = measure_bounded_cstring(message, rpc::MAX_PAYLOAD_SIZE);
    length = static_cast<uint16_t>(info.length);
    payload = reinterpret_cast<const uint8_t*>(message);
  }
  _doEmitStatus(status_code, payload, length);
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
    payload = _scratch_payload.data();
  }
  _doEmitStatus(status_code, payload, length);
}

bool BridgeClass::sendFrame(rpc::CommandId command_id, const uint8_t* arg_payload, size_t arg_length) {
  return _sendFrame(rpc::to_underlying(command_id), arg_payload, arg_length);
}

bool BridgeClass::sendFrame(rpc::StatusCode status_code, const uint8_t* arg_payload, size_t arg_length) {
  return _sendFrame(rpc::to_underlying(status_code), arg_payload, arg_length);
}

void BridgeClass::sendChunkyFrame(rpc::CommandId command_id, 
                                  const uint8_t* header, size_t header_len, 
                                  const uint8_t* data, size_t data_len) {
  if (header_len >= rpc::MAX_PAYLOAD_SIZE) return; // Header too big to fit any data
  
  const size_t max_chunk_size = rpc::MAX_PAYLOAD_SIZE - header_len;
  size_t offset = 0;

  // Handle empty data case (send at least one frame with just header)
  if (data_len == 0) {
     uint8_t* buffer = getScratchBuffer();
     if (header_len > 0) etl::copy_n(header, header_len, buffer);
     while (!_sendFrame(rpc::to_underlying(command_id), buffer, header_len)) {
       process();
     }
     return;
  }

  while (offset < data_len) {
    size_t bytes_remaining = data_len - offset;
    size_t chunk_size = (bytes_remaining > max_chunk_size) ? max_chunk_size : bytes_remaining;

    uint8_t* buffer = getScratchBuffer();
    
    // 1. Copy Header
    if (header_len > 0) etl::copy_n(header, header_len, buffer);
    
    // 2. Copy Data Chunk
    if (data) etl::copy_n(data + offset, chunk_size, buffer + header_len);

    size_t payload_size = header_len + chunk_size;

    // 3. Send with Back-pressure
    // If the TX queue is full, we must pump the FSM (process()) to clear it
    // before we can send the next chunk. This guarantees sequential delivery.
    while (!_sendFrame(rpc::to_underlying(command_id), buffer, payload_size)) {
      process();
    }

    offset += chunk_size;
  }
}

bool BridgeClass::_isHandshakeCommand(uint16_t command_id) const {
  return (command_id <= rpc::RPC_SYSTEM_COMMAND_MAX) ||
         (command_id == rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION_RESP)) ||
         (command_id == rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC_RESP)) ||
         (command_id == rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET_RESP));
}

bool BridgeClass::_sendFrame(uint16_t command_id, const uint8_t* arg_payload, size_t arg_length) {
  // [SIL-2] Finite State Machine - Outbound Filter via ETL FSM
  if (_fsm.isFault()) {
    // Safety State: Drop all outbound traffic
    return false;
  }
  
  if (_fsm.isUnsynchronized()) {
    // Only allow handshake commands during startup
    if (!_isHandshakeCommand(command_id)) return false;
  }
  // Idle and AwaitingAck are allowed to send

  uint16_t final_cmd = command_id;
  const uint8_t* final_payload = arg_payload;
  size_t final_len = arg_length;

  if (arg_length > 0 && rle::should_compress(arg_payload, arg_length)) {
    size_t compressed_len = rle::encode(arg_payload, arg_length, _scratch_payload.data(), rpc::MAX_PAYLOAD_SIZE);
    if (compressed_len > 0 && compressed_len < arg_length) {
      final_cmd |= rpc::RPC_CMD_FLAG_COMPRESSED;
      final_payload = _scratch_payload.data();
      final_len = compressed_len;
    }
  }

  const bool critical = _requiresAck(final_cmd & ~rpc::RPC_CMD_FLAG_COMPRESSED);

  // [SIL-2] State-Driven Sending Logic via ETL FSM
  if (critical && _fsm.isAwaitingAck()) {
    // Already waiting? Queue it.
    if (_pending_tx_queue.full() || final_len > rpc::MAX_PAYLOAD_SIZE) {
      // Queue full? Try to process timeout to free space (Emergency Valve)
      _processAckTimeout();
      if (_fsm.isAwaitingAck() || _pending_tx_queue.full()) return false;
    }
    
    // Inlined _enqueuePendingTx
    PendingTxFrame frame;
    frame.command_id = final_cmd;
    frame.payload_length = static_cast<uint16_t>(final_len);
    if (final_len > 0 && final_payload) etl::copy_n(final_payload, final_len, frame.payload.data());
    _pending_tx_queue.push(frame);
    return true;
  }

  // Inlined _sendFrameImmediate
  rpc::FrameBuilder builder;
  _last_raw_frame.resize(_last_raw_frame.capacity());
  size_t raw_len = builder.build(_last_raw_frame.data(), _last_raw_frame.size(), final_cmd, final_payload, final_len);
  if (raw_len == 0) {
    _last_raw_frame.clear();
    return false;
  }
  _last_raw_frame.resize(raw_len);
  _packetSerial.send(_last_raw_frame.data(), _last_raw_frame.size());
  flushStream();

  if (critical) {
    _fsm.sendCritical();  // Transition to AwaitingAck
    _retry_count = 0;
    _last_send_millis = millis();
    _last_command_id = final_cmd;
  }
  return true;
}

bool BridgeClass::_requiresAck(uint16_t command_id) const {
  if (command_id >= rpc::RPC_STATUS_CODE_MIN && command_id <= rpc::RPC_STATUS_CODE_MAX) return false;
  if (command_id == rpc::to_underlying(rpc::CommandId::CMD_XOFF) || command_id == rpc::to_underlying(rpc::CommandId::CMD_XON)) return false;
  switch (command_id) {
    case rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE):
    case rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE):
    case rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE):
      return true;
    #if BRIDGE_ENABLE_DATASTORE
    case rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_PUT): return true;
    #endif
    #if BRIDGE_ENABLE_MAILBOX
    case rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH): return true;
    #endif
    #if BRIDGE_ENABLE_FILESYSTEM
    case rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE): return true;
    #endif
    default: return false;
  }
}

void BridgeClass::_clearAckState() { 
  if (_fsm.isAwaitingAck()) {
    _fsm.ackReceived();  // Transition back to Idle
  }
  _retry_count = 0; 
}

void BridgeClass::_handleAck(uint16_t command_id) {
  if (_fsm.isAwaitingAck() && (command_id == rpc::RPC_INVALID_ID_SENTINEL || command_id == _last_command_id)) {
    _clearAckState();
    _flushPendingTxQueue();
  }
}

void BridgeClass::_handleMalformed(uint16_t command_id) {
  if (command_id == rpc::RPC_INVALID_ID_SENTINEL || command_id == _last_command_id) _retransmitLastFrame();
}

void BridgeClass::_retransmitLastFrame() {
  if (_fsm.isAwaitingAck() && !_last_raw_frame.empty()) {
    _packetSerial.send(_last_raw_frame.data(), _last_raw_frame.size());
    _retry_count++;
    _last_send_millis = millis();
    flushStream();
  }
}

void BridgeClass::_processAckTimeout() {
  if (!_fsm.isAwaitingAck()) return;
  unsigned long now = millis();
  if ((now - _last_send_millis) < _ack_timeout_ms) return;
  if (_retry_count >= _ack_retry_limit) {
    if (_status_handler) _status_handler(rpc::StatusCode::STATUS_TIMEOUT, nullptr, 0);
    _fsm.timeout();  // Transition to Unsynchronized via FSM
    enterSafeState(); 
    return;
  }
  _retransmitLastFrame();
}

void BridgeClass::enterSafeState() {
  _fsm.resetFsm();  // Transition to Unsynchronized via ETL FSM
  // Note: _clearAckState() checks FSM state, so we skip to avoid redundant transition
  _retry_count = 0;
  _clearPendingTxQueue();
  _frame_received = false;
  _target_frame = nullptr;
  _last_command_id = 0;
  _last_rx_crc = 0;
  _last_rx_crc_millis = 0;
  _consecutive_crc_errors = 0;
  _last_raw_frame.clear();
}

void BridgeClass::_sendAckAndFlush(uint16_t command_id) {
  etl::array<uint8_t, 2> ack_payload;
  rpc::write_u16_be(ack_payload.data(), command_id);
  (void)sendFrame(rpc::StatusCode::STATUS_ACK, ack_payload.data(), ack_payload.size());
  flushStream();
}

void BridgeClass::_flushPendingTxQueue() {
  if (_fsm.isAwaitingAck() || _pending_tx_queue.empty()) return;
  const PendingTxFrame& frame = _pending_tx_queue.front();

  rpc::FrameBuilder builder;
  _last_raw_frame.resize(_last_raw_frame.capacity());
  size_t raw_len = builder.build(_last_raw_frame.data(), _last_raw_frame.size(), frame.command_id, frame.payload.data(), frame.payload_length);
  if (raw_len > 0) {
    _last_raw_frame.resize(raw_len);
    _packetSerial.send(_last_raw_frame.data(), _last_raw_frame.size());
    flushStream();

    _fsm.sendCritical();  // Transition to AwaitingAck via FSM
    _retry_count = 0;
    _last_send_millis = millis();
    _last_command_id = frame.command_id;
    _pending_tx_queue.pop();
  } else {
    _last_raw_frame.clear();
  }
}

void BridgeClass::_clearPendingTxQueue() { while (!_pending_tx_queue.empty()) _pending_tx_queue.pop(); }

void BridgeClass::_computeHandshakeTag(const uint8_t* nonce, size_t nonce_len, uint8_t* out_tag) {
  if (_shared_secret.empty() || nonce_len == 0 || !nonce) {
    etl::fill_n(out_tag, kHandshakeTagSize, uint8_t{0});
    return;
  }

  // [MIL-SPEC] Use HKDF derived key for handshake authentication.
  // [OPTIMIZATION] Reuse scratch buffer to avoid 64 bytes of stack allocation (key + digest).
  uint8_t* handshake_key = _scratch_payload.data(); // 32 bytes
  uint8_t* digest = _scratch_payload.data() + 32;   // 32 bytes

  rpc::security::hkdf_sha256(
      _shared_secret.data(), _shared_secret.size(),
      rpc::RPC_HANDSHAKE_HKDF_SALT, rpc::RPC_HANDSHAKE_HKDF_SALT_LEN,
      rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH, rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH_LEN,
      handshake_key, 32);

  SHA256 sha256;
  sha256.resetHMAC(handshake_key, 32);
  sha256.update(nonce, nonce_len);
  sha256.finalizeHMAC(handshake_key, 32, digest, kSha256DigestSize);
  etl::copy_n(digest, kHandshakeTagSize, out_tag);
  
  rpc::security::secure_zero(handshake_key, 32);
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
  _ack_timeout_ms = (ack_timeout_ms >= rpc::RPC_HANDSHAKE_ACK_TIMEOUT_MIN_MS && ack_timeout_ms <= rpc::RPC_HANDSHAKE_ACK_TIMEOUT_MAX_MS) ? ack_timeout_ms : rpc::RPC_DEFAULT_ACK_TIMEOUT_MS;
  _ack_retry_limit = (retry_limit >= rpc::RPC_HANDSHAKE_RETRY_LIMIT_MIN && retry_limit <= rpc::RPC_HANDSHAKE_RETRY_LIMIT_MAX) ? retry_limit : rpc::RPC_DEFAULT_RETRY_LIMIT;
  _response_timeout_ms = (response_timeout_ms >= rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS && response_timeout_ms <= rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS) ? response_timeout_ms : rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS;
}

bool BridgeClass::_isRecentDuplicateRx(const rpc::Frame& frame) const {
  if (_last_rx_crc == 0 || frame.crc != _last_rx_crc) return false;
  const unsigned long elapsed = millis() - _last_rx_crc_millis;
  if (_ack_timeout_ms > 0 && elapsed < static_cast<unsigned long>(_ack_timeout_ms)) return false;
  return elapsed <= (static_cast<unsigned long>(_ack_timeout_ms) * (_ack_retry_limit + 1));
}

void BridgeClass::_markRxProcessed(const rpc::Frame& frame) {
  _last_rx_crc = frame.crc;
  _last_rx_crc_millis = millis();
}

#if BRIDGE_DEBUG_FRAMES
BridgeClass::FrameDebugSnapshot BridgeClass::getTxDebugSnapshot() const { return _tx_debug; }
void BridgeClass::resetTxDebugStats() { _tx_debug = {}; }
#endif

// [SIL-2] ETL Error Handler Implementation
namespace etl {
void __attribute__((weak)) handle_error(const etl::exception& e) {
  (void)e;
  Bridge.enterSafeState();
}
}
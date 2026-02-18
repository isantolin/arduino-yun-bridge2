/*
 * This file is part of Arduino MCU Ecosystem v2.
 */
#include "Bridge.h"

// [SIL-2] Explicitly include Arduino.h to satisfy IntelliSense and ensure
// noInterrupts()/interrupts() are available in all compilation contexts.
#include <Arduino.h>
#include <etl/span.h>

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

#include "protocol/rle.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "protocol/PacketBuilder.h"
#include "security/security.h"
#include "etl/error_handler.h"
#include "etl/algorithm.h"

// [OPTIMIZATION] Numerical status codes used instead of PROGMEM strings.

namespace {
constexpr size_t kHandshakeTagSize = rpc::RPC_HANDSHAKE_TAG_LENGTH;
static_assert(
  kHandshakeTagSize > 0,
  "RPC_HANDSHAKE_TAG_LENGTH must be greater than zero"
);
constexpr size_t kSha256DigestSize = 32;

// Global instance pointer for PacketSerial static callback
BridgeClass* g_bridge_instance = nullptr;

} // namespace

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
ConsoleClass Console;
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
      _last_command_id(0),
      _retry_count(0),
      _pending_baudrate(0),
      _last_rx_crc(0),
      _last_rx_crc_millis(0),
      _consecutive_crc_errors(0),
      _ack_timeout_ms(rpc::RPC_DEFAULT_ACK_TIMEOUT_MS),
      _ack_retry_limit(rpc::RPC_DEFAULT_RETRY_LIMIT),
      _response_timeout_ms(rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS),
      _command_handler(),
      _digital_read_handler(),
      _analog_read_handler(),
      _get_free_memory_handler(),
      _status_handler(),
      _pending_tx_queue(),
      _fsm(),
      _timer_service(),
      _last_tick_millis(0),
      _startup_stabilizing(false),
      _command_router()
{
    g_bridge_instance = this;
}

void BridgeClass::begin(
    unsigned long arg_baudrate, etl::string_view arg_secret, size_t arg_secret_len) {
  
  // [SIL-2] Start the ETL FSM before any other initialization
  _fsm.begin();
  
  // [SIL-2] Initialize ETL Message Router
  _command_router.setHandler(this);
  
  // [SIL-2] Initialize ETL Callback Timer Service
  _timer_service.clear();
  
  // Register timers in strict order to match TimerId enum
  // [SIL-2] Delegates are class members to ensure lifetime persistence
  // ETL callback_timer stores pointers to delegates, not copies
  _cb_ack_timeout = etl::delegate<void()>::create<BridgeClass, &BridgeClass::_onAckTimeout>(*this);
  _timer_service.register_timer(_cb_ack_timeout, _ack_timeout_ms, false);

  _cb_rx_dedupe = etl::delegate<void()>::create<BridgeClass, &BridgeClass::_onRxDedupe>(*this);
  _timer_service.register_timer(_cb_rx_dedupe, BRIDGE_RX_DEDUPE_INTERVAL_MS, false);

  _cb_baudrate_change = etl::delegate<void()>::create<BridgeClass, &BridgeClass::_onBaudrateChange>(*this);
  _timer_service.register_timer(_cb_baudrate_change, BRIDGE_BAUDRATE_SETTLE_MS, false);

  _cb_startup_stabilized = etl::delegate<void()>::create<BridgeClass, &BridgeClass::_onStartupStabilized>(*this);
  _timer_service.register_timer(_cb_startup_stabilized, BRIDGE_STARTUP_STABILIZATION_MS, false);
  
  _timer_service.enable(true);
  _last_tick_millis = millis();

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
  #endif

  if (_hardware_serial != nullptr) {
      _hardware_serial->begin(arg_baudrate);
  }

  // Configure PacketSerial
  _packetSerial.setStream(&_stream);
  _packetSerial.setPacketHandler(onPacketReceived);

  // [SIL-2] Non-blocking Startup Stabilization
  // Start timer and set flag - process() will drain the buffer during this period
  _startup_stabilizing = true;
  _timer_service.start(bridge::scheduler::TIMER_STARTUP_STABILIZATION, false);

  _shared_secret.clear();
  if (!arg_secret.empty()) {
    size_t actual_len = (arg_secret_len > 0) ? arg_secret_len : arg_secret.length();
    if (actual_len > _shared_secret.capacity()) {
      actual_len = _shared_secret.capacity();
    }
    const uint8_t* start = reinterpret_cast<const uint8_t*>(arg_secret.data());
    _shared_secret.assign(start, start + actual_len);
  }

  // [SIL-2] FSM reset to Unsynchronized state
  _fsm.resetFsm();
  _last_command_id = 0;
  _retry_count = 0;
  _last_rx_crc = 0;
  _last_rx_crc_millis = 0;

}

void BridgeClass::onPacketReceived(const uint8_t* buffer, size_t size) {
    if (g_bridge_instance && g_bridge_instance->_target_frame) {
        auto result = g_bridge_instance->_parser.parse(etl::span<const uint8_t>(buffer, size));
        if (result.has_value()) {
            *g_bridge_instance->_target_frame = result.value();
            g_bridge_instance->_frame_received = true;
            g_bridge_instance->_last_parse_error.reset();
        } else {
            g_bridge_instance->_last_parse_error = result.error();
        }
    }
}

void BridgeClass::process() {
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

  // [SIL-2] Non-blocking Startup Stabilization Phase
  // During startup, drain the serial buffer to discard garbage
  // Timer will call _onStartupStabilized() when complete
  if (_startup_stabilizing) {
    // [SIL-2] Bounded drain to ensure deterministic WCET
    uint8_t drain_limit = 64; 
    while (_stream.available() > 0 && drain_limit-- > 0) { 
      _stream.read(); 
    }
    // Continue to timer tick at the bottom of process()
  }

  // Polling Input Logic (skip during stabilization)
  bool frame_received = false;
  if (!_startup_stabilizing) {
    BRIDGE_ATOMIC_BLOCK {
      _target_frame = &_rx_frame;
      _frame_received = false;
      _packetSerial.update();
      frame_received = _frame_received;
      _target_frame = nullptr;
    }
  }

  if (frame_received) {
    BRIDGE_ATOMIC_BLOCK { _consecutive_crc_errors = 0; }
    dispatch(_rx_frame);
  } else {
    // [SIL-2] Type-safe error handling with etl::expected
    if (_last_parse_error.has_value()) {
      rpc::FrameError error = _last_parse_error.value();
      if (error == rpc::FrameError::CRC_MISMATCH) {
        BRIDGE_ATOMIC_BLOCK { _consecutive_crc_errors++; }
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
          case rpc::FrameError::CRC_MISMATCH:
            _emitStatus(rpc::StatusCode::STATUS_CRC_MISMATCH, (const char*)nullptr);
            break;
          case rpc::FrameError::MALFORMED:
            _emitStatus(rpc::StatusCode::STATUS_MALFORMED, (const char*)nullptr);
            break;
          case rpc::FrameError::OVERFLOW:
            _emitStatus(rpc::StatusCode::STATUS_MALFORMED, (const char*)nullptr);
            break;
        }
      }
      _last_parse_error.reset();
    }
  }

  // [SIL-2] Centralized Scheduler Tick
  // Replaces manual timeout checks with ETL Timer Service
  const uint32_t now = static_cast<uint32_t>(millis());
  uint32_t delta = now - _last_tick_millis;
  // Handle millis() rollover (overflow) by implicit unsigned arithmetic
  // [SIL-2] Cap delta to prevent timer starvation in test environments
  // where millis() may jump large amounts. In production, process() is
  // called frequently enough that delta is always small.
  constexpr uint32_t kMaxTickDeltaMs = 1000UL;
  if (delta > kMaxTickDeltaMs) {
      delta = kMaxTickDeltaMs;
  }
  if (delta > 0U) {
      _timer_service.tick(delta);
      _last_tick_millis = now;
  }

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
        arch = rpc::RPC_ARCH_AVR;
        #elif defined(ARDUINO_ARCH_ESP32)
        arch = rpc::RPC_ARCH_ESP32;
        #elif defined(ARDUINO_ARCH_ESP8266)
        arch = rpc::RPC_ARCH_ESP8266;
        #elif defined(ARDUINO_ARCH_SAMD)
        arch = rpc::RPC_ARCH_SAMD;
        #elif defined(ARDUINO_ARCH_SAM)
        arch = rpc::RPC_ARCH_SAM;
        #elif defined(ARDUINO_ARCH_RP2040)
        arch = rpc::RPC_ARCH_RP2040;
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
        if (kBridgeEnableWatchdog) features |= rpc::RPC_CAPABILITY_WATCHDOG;
        features |= rpc::RPC_CAPABILITY_RLE; 
        #if BRIDGE_DEBUG_FRAMES
        features |= rpc::RPC_CAPABILITY_DEBUG_FRAMES;
        #endif
        #if BRIDGE_DEBUG_IO
        features |= rpc::RPC_CAPABILITY_DEBUG_IO;
        #endif

        #if defined(E2END) && (E2END > 0)
        features |= rpc::RPC_CAPABILITY_EEPROM;
        #endif

        #if (defined(DAC_OUTPUT_CHANNELS) && (DAC_OUTPUT_CHANNELS > 0)) || \
            defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM) || defined(ARDUINO_ARCH_ESP32)
        features |= rpc::RPC_CAPABILITY_DAC;
        #endif

        #if defined(HAVE_HWSERIAL1)
        features |= rpc::RPC_CAPABILITY_HW_SERIAL1;
        #endif

        #if defined(__FPU_PRESENT) && (__FPU_PRESENT == 1)
        features |= rpc::RPC_CAPABILITY_FPU;
        #endif

        #if defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM) || \
            defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_ESP8266) || \
            defined(ARDUINO_ARCH_RP2040)
        features |= rpc::RPC_CAPABILITY_LOGIC_3V3;
        #endif

        #if defined(SERIAL_RX_BUFFER_SIZE) && (SERIAL_RX_BUFFER_SIZE > 64)
        features |= rpc::RPC_CAPABILITY_BIG_BUFFER;
        #endif

        #if defined(PIN_WIRE_SDA) || defined(SDA) || defined(DT) || \
            defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_ESP8266)
        features |= rpc::RPC_CAPABILITY_I2C;
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
        
        _pending_baudrate = new_baud;
        _timer_service.set_period(bridge::scheduler::TIMER_BAUDRATE_CHANGE, BRIDGE_BAUDRATE_SETTLE_MS);
        _timer_service.start(bridge::scheduler::TIMER_BAUDRATE_CHANGE, false);
      }
      break;
    case rpc::CommandId::CMD_LINK_SYNC:
      {
        const size_t nonce_length = payload_length;
        if (nonce_length != rpc::RPC_HANDSHAKE_NONCE_LENGTH) break;
        
        enterSafeState();
        Console.begin();
        const bool has_secret = !_shared_secret.empty();
        const size_t response_length = static_cast<size_t>(nonce_length) + (has_secret ? kHandshakeTagSize : 0);
        
        if (response_length > rpc::MAX_PAYLOAD_SIZE) {
          (void)sendFrame(rpc::StatusCode::STATUS_MALFORMED);
          break;
        }

        etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buffer; // [RAM OPT] Stack allocation
        
        if (payload_data) {
          etl::copy_n(payload_data, nonce_length, buffer.begin());
          if (has_secret) {
            etl::array<uint8_t, kHandshakeTagSize> tag;
            _computeHandshakeTag(payload_data, nonce_length, tag.data());
            etl::copy_n(tag.begin(), kHandshakeTagSize, buffer.begin() + nonce_length);
          }
          (void)sendFrame(rpc::CommandId::CMD_LINK_SYNC_RESP, buffer.data(), response_length);
          // [SIL-2] Handshake complete -> Transition to Idle via FSM
          _fsm.handshakeComplete();
        }
      }
      break;
    case rpc::CommandId::CMD_LINK_RESET:
      if (payload_length == 0 || payload_length == rpc::RPC_HANDSHAKE_CONFIG_SIZE) {
        enterSafeState();
        _applyTimingConfig(payload_data, payload_length);
        Console.begin();
        (void)sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP);
      }
      break;
    default:
      break;
  }
}

// [SIL-2] Pin Validation Helper
static inline bool _isValidPin(uint8_t pin) {
#ifdef NUM_DIGITAL_PINS
  return pin < NUM_DIGITAL_PINS;
#else
  return true;
#endif
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
        if (!_isValidPin(pin)) return;
        ::pinMode(pin, mode);
      }
      break;
    case rpc::CommandId::CMD_DIGITAL_WRITE:
      if (payload_length == 2) {
        uint8_t pin = payload_data[0];
        uint8_t value = payload_data[1] ? HIGH : LOW;
        if (!_isValidPin(pin)) return;
        ::digitalWrite(pin, value);
      }
      break;
    case rpc::CommandId::CMD_ANALOG_WRITE:
      if (payload_length == 2) {
        uint8_t pin = payload_data[0];
        if (!_isValidPin(pin)) return;
        ::analogWrite(pin, static_cast<int>(payload_data[1]));
      }
      break;
    case rpc::CommandId::CMD_DIGITAL_READ:
      if (payload_length == 1) {
        uint8_t pin = payload_data[0];
        if (!_isValidPin(pin)) {
          (void)sendFrame(rpc::StatusCode::STATUS_MALFORMED);
          return;
        }
        int16_t value = ::digitalRead(pin);
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
#else
        if (!_isValidPin(pin)) {
             (void)sendFrame(rpc::StatusCode::STATUS_MALFORMED);
             return;
        }
#endif
        int16_t value = ::analogRead(pin);
        etl::array<uint8_t, 2> resp_payload;
        rpc::write_u16_be(resp_payload.data(), static_cast<uint16_t>(value & rpc::RPC_UINT16_MAX));
        (void)sendFrame(rpc::CommandId::CMD_ANALOG_READ_RESP, resp_payload.data(), resp_payload.size());
      }
      break;
    default:
      break;
  }
}

void BridgeClass::_handleConsoleCommand(const rpc::Frame& frame) {
  if (static_cast<rpc::CommandId>(frame.header.command_id) == rpc::CommandId::CMD_CONSOLE_WRITE) {
    Console._push(etl::span<const uint8_t>(frame.payload.data(), frame.header.payload_length));
  }
}

void BridgeClass::dispatch(const rpc::Frame& frame) {
  // [SIL-2] Phase 1: Decompress if needed
  uint16_t raw_command = frame.header.command_id;
  bool is_compressed = (raw_command & rpc::RPC_CMD_FLAG_COMPRESSED) != 0;
  raw_command &= ~rpc::RPC_CMD_FLAG_COMPRESSED;

  // [OPTIMIZATION] Construct effective_frame piecewise to avoid copying unused payload bytes.
  // rpc::Frame contains etl::array (fixed size), so assignment copies MAX_PAYLOAD_SIZE bytes.
  rpc::Frame effective_frame;
  effective_frame.header = frame.header;
  effective_frame.header.command_id = raw_command;
  // CRC is not propagated as it is validated before dispatch and not used downstream.

  if (is_compressed && frame.header.payload_length > 0) {
    etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> scratch_payload;
    size_t decoded_len = rle::decode(
        etl::span<const uint8_t>(frame.payload.data(), frame.header.payload_length), 
        etl::span<uint8_t>(scratch_payload.data(), rpc::MAX_PAYLOAD_SIZE));
    if (decoded_len == 0) {
      _emitStatus(rpc::StatusCode::STATUS_MALFORMED, (const char*)nullptr);
      return;
    }
    etl::copy_n(scratch_payload.data(), decoded_len, effective_frame.payload.data());
    effective_frame.header.payload_length = static_cast<uint16_t>(decoded_len);
  } else {
    // Zero-copy optimization: If not compressed, we still need 'effective_frame' 
    // because command_id might have changed (flag stripping).
    // Copy ONLY valid payload bytes, not the full MAX_PAYLOAD_SIZE array.
    if (frame.header.payload_length > 0) {
        etl::copy_n(frame.payload.data(), frame.header.payload_length, effective_frame.payload.data());
    }
  }

  // [SIL-2] Phase 2: Build context and route via ETL message_router
  bridge::router::CommandContext ctx;
  ctx.frame = &effective_frame;
  ctx.raw_command = raw_command;
  ctx.is_duplicate = _isRecentDuplicateRx(effective_frame);
  ctx.requires_ack = false;

  _command_router.route(ctx);
}

// ============================================================================
// [SIL-2] ICommandHandler Implementation - ETL Message Router Callbacks
// ============================================================================

void BridgeClass::onStatusCommand(const bridge::router::CommandContext& ctx) {
  const rpc::StatusCode status = static_cast<rpc::StatusCode>(ctx.raw_command);
  const size_t payload_length = ctx.frame->header.payload_length;
  const uint8_t* payload_data = ctx.frame->payload.data();
  
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
  if (_status_handler.is_valid()) _status_handler(status, payload_data, static_cast<uint16_t>(payload_length));
}

void BridgeClass::onSystemCommand(const bridge::router::CommandContext& ctx) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(ctx.raw_command);
  
  if (command == rpc::CommandId::CMD_LINK_RESET) {
    if (ctx.is_duplicate) {
      _sendAckAndFlush(ctx.raw_command);
      return;
    }
    _sendAckAndFlush(ctx.raw_command);
  }
  
  _handleSystemCommand(*ctx.frame);
  
  if (command == rpc::CommandId::CMD_LINK_SYNC) {
    // Note: LINK_SYNC sends its own response, no ACK needed
  }
}

void BridgeClass::onGpioCommand(const bridge::router::CommandContext& ctx) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(ctx.raw_command);
  
  switch(command) {
    case rpc::CommandId::CMD_SET_PIN_MODE:
    case rpc::CommandId::CMD_DIGITAL_WRITE:
    case rpc::CommandId::CMD_ANALOG_WRITE:
      _handleDedupAck(
          ctx,
          [this, &ctx]() { _handleGpioCommand(*ctx.frame); },
          true);
      break;
      
    case rpc::CommandId::CMD_DIGITAL_READ:
    case rpc::CommandId::CMD_ANALOG_READ:
      if (ctx.is_duplicate) return;
      _handleGpioCommand(*ctx.frame);
      _markRxProcessed(*ctx.frame);
      break;
      
    default:
      break;
  }
}

void BridgeClass::onConsoleCommand(const bridge::router::CommandContext& ctx) {
  _handleDedupAck(
      ctx,
      [this, &ctx]() { _handleConsoleCommand(*ctx.frame); },
      true);
}

void BridgeClass::onDataStoreCommand(const bridge::router::CommandContext& ctx) {
  #if BRIDGE_ENABLE_DATASTORE
  DataStore.handleResponse(*ctx.frame);
  #else
  (void)ctx;
  #endif
}

void BridgeClass::onMailboxCommand(const bridge::router::CommandContext& ctx) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(ctx.raw_command);
  
  if (command == rpc::CommandId::CMD_MAILBOX_PUSH) {
    _handleDedupAck(
        ctx,
        [this, &ctx]() {
#if BRIDGE_ENABLE_MAILBOX
          Mailbox.handleResponse(*ctx.frame);
#else
          (void)ctx;
#endif
        },
        true);
  } else {
    #if BRIDGE_ENABLE_MAILBOX
    Mailbox.handleResponse(*ctx.frame);
    #endif
  }
}

void BridgeClass::onFileSystemCommand(const bridge::router::CommandContext& ctx) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(ctx.raw_command);
  
  if (command == rpc::CommandId::CMD_FILE_WRITE) {
    _handleDedupAck(
        ctx,
        [this, &ctx]() {
#if BRIDGE_ENABLE_FILESYSTEM
          FileSystem.handleResponse(*ctx.frame);
#else
          (void)ctx;
#endif
        },
        true);
  } else {
    #if BRIDGE_ENABLE_FILESYSTEM
    FileSystem.handleResponse(*ctx.frame);
    #endif
  }
}

void BridgeClass::onProcessCommand(const bridge::router::CommandContext& ctx) {
  #if BRIDGE_ENABLE_PROCESS
  Process.handleResponse(*ctx.frame);
  #else
  (void)ctx;
  #endif
}

void BridgeClass::onUnknownCommand(const bridge::router::CommandContext& ctx) {
  if (_command_handler.is_valid()) {
    _command_handler(*ctx.frame);
  } else {
    (void)sendFrame(rpc::StatusCode::STATUS_CMD_UNKNOWN);
  }
}

// Helper for sending ACK without flush
void BridgeClass::_sendAck(uint16_t command_id) {
  etl::array<uint8_t, 2> ack_payload;
  rpc::write_u16_be(ack_payload.data(), command_id);
  (void)sendFrame(rpc::StatusCode::STATUS_ACK, ack_payload.data(), ack_payload.size());
}

void BridgeClass::_doEmitStatus(rpc::StatusCode status_code, const uint8_t* payload, uint16_t length) {
  (void)sendFrame(status_code, payload, length);
  if (_status_handler.is_valid()) _status_handler(status_code, payload, length);
}

void BridgeClass::_emitStatus(rpc::StatusCode status_code, etl::string_view message) {
  const uint8_t* payload = nullptr;
  uint16_t length = 0;
  if (!message.empty()) {
    length = static_cast<uint16_t>(etl::min(message.length(), rpc::MAX_PAYLOAD_SIZE));
    payload = reinterpret_cast<const uint8_t*>(message.data());
  }
  _doEmitStatus(status_code, payload, length);
}

void BridgeClass::_emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message) {
  const uint8_t* payload = nullptr;
  uint16_t length = 0;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buffer; // [RAM OPT] Stack allocation
  
  if (message) {
    const char* p = reinterpret_cast<const char*>(message);
    length = strnlen_P(p, rpc::MAX_PAYLOAD_SIZE);
    memcpy_P(buffer.data(), p, length);
    payload = buffer.data();
  }
  _doEmitStatus(status_code, payload, length);
}

bool BridgeClass::sendFrame(rpc::CommandId command_id, const uint8_t* arg_payload, size_t arg_length) {
  return _sendFrame(rpc::to_underlying(command_id), arg_payload, arg_length);
}

bool BridgeClass::sendFrame(rpc::StatusCode status_code, const uint8_t* arg_payload, size_t arg_length) {
  return _sendFrame(rpc::to_underlying(status_code), arg_payload, arg_length);
}

bool BridgeClass::sendStringCommand(rpc::CommandId command_id, etl::string_view str, size_t max_len) {
  if (str.empty() || str.length() > max_len || str.length() >= rpc::MAX_PAYLOAD_SIZE) return false;
  
  etl::vector<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload;
  rpc::PacketBuilder(payload).add_pascal_string(str);
  return sendFrame(command_id, payload.data(), payload.size());
}

bool BridgeClass::sendKeyValCommand(rpc::CommandId command_id, etl::string_view key, size_t max_key, etl::string_view val, size_t max_val) {
  if (key.empty() || key.length() > max_key || val.length() > max_val) return false;
  if (key.length() + val.length() + 2 > rpc::MAX_PAYLOAD_SIZE) return false;

  etl::vector<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload;
  rpc::PacketBuilder(payload)
    .add_pascal_string(key)
    .add_pascal_string(val);
  return sendFrame(command_id, payload.data(), payload.size());
}

void BridgeClass::sendChunkyFrame(rpc::CommandId command_id, 
                                  const uint8_t* header, size_t header_len, 
                                  const uint8_t* data, size_t data_len) {
  if (header_len >= rpc::MAX_PAYLOAD_SIZE) return; // Header too big to fit any data
  
  // [RAM OPT] Migrate scratch buffer to stack. No init needed as we overwrite.
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buffer;
  
  const size_t max_chunk_size = rpc::MAX_PAYLOAD_SIZE - header_len;
  size_t offset = 0;

  // Handle empty data case (send at least one frame with just header)
  if (data_len == 0) {
     if (header_len > 0) etl::copy_n(header, header_len, buffer.begin());
     while (!_sendFrame(rpc::to_underlying(command_id), buffer.data(), header_len)) {
       process();
     }
     return;
  }

  while (offset < data_len) {
    size_t bytes_remaining = data_len - offset;
    size_t chunk_size = (bytes_remaining > max_chunk_size) ? max_chunk_size : bytes_remaining;

    // 1. Copy Header
    if (header_len > 0) etl::copy_n(header, header_len, buffer.begin());
    
    // 2. Copy Data Chunk
    if (data) etl::copy_n(data + offset, chunk_size, buffer.begin() + header_len);

    size_t payload_size = header_len + chunk_size;

    // 3. Send with Back-pressure
    // If the TX queue is full, we must pump the FSM (process()) to clear it
    // before we can send the next chunk. This guarantees sequential delivery.
    while (!_sendFrame(rpc::to_underlying(command_id), buffer.data(), payload_size)) {
      if (!_fsm.isSynchronized()) return; // [SAFETY] Don't loop if we lost synchronization
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

  // [RAM OPT] Stack allocation for compression buffer
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> scratch_payload;

  if (arg_length > 0 && rle::should_compress(etl::span<const uint8_t>(arg_payload, arg_length))) {
    size_t compressed_len = rle::encode(
        etl::span<const uint8_t>(arg_payload, arg_length), 
        scratch_payload);
    if (compressed_len > 0 && compressed_len < arg_length) {
      final_cmd |= rpc::RPC_CMD_FLAG_COMPRESSED;
      final_payload = scratch_payload.data();
      final_len = compressed_len;
    }
  }

  const bool critical = _requiresAck(final_cmd & ~rpc::RPC_CMD_FLAG_COMPRESSED);

  // [SIL-2] State-Driven Sending Logic via ETL FSM
  if (critical) {
    // [SIL-2] ISR Protection for Queue Access
    bool queue_full = false;
    BRIDGE_ATOMIC_BLOCK {
      queue_full = _pending_tx_queue.full();
    }

    if (queue_full || final_len > rpc::MAX_PAYLOAD_SIZE) {
      return false;
    }
    
    // Inlined _enqueuePendingTx
    PendingTxFrame frame;
    frame.command_id = final_cmd;
    frame.payload_length = static_cast<uint16_t>(final_len);
    if (final_len > 0 && final_payload) etl::copy_n(final_payload, final_len, frame.payload.data());
    
    BRIDGE_ATOMIC_BLOCK {
      _pending_tx_queue.push(frame);
    }

    // If we are not waiting for an ACK, we can start sending this frame immediately.
    // _flushPendingTxQueue will pick it up (it's at the front).
    if (!_fsm.isAwaitingAck()) {
        _flushPendingTxQueue();
    }
    return true;
  }

  // Non-critical frame: Send immediately using stack buffer
  etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE> raw_buffer;
  rpc::FrameBuilder builder;
  size_t raw_len = builder.build(
      raw_buffer, 
      final_cmd, 
      etl::span<const uint8_t>(final_payload, final_len));
  
  if (raw_len > 0) {
    _packetSerial.send(raw_buffer.data(), raw_len);
    flushStream();
  }

  return true;
}

bool BridgeClass::_requiresAck(uint16_t command_id) const {
  return rpc::requires_ack(command_id);
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
    
    // [SIL-2] Stop ACK Timer
    _timer_service.stop(bridge::scheduler::TIMER_ACK_TIMEOUT);

    // [SIL-2] ACK received -> Safe to remove frame from queue
    BRIDGE_ATOMIC_BLOCK {
      if (!_pending_tx_queue.empty()) {
         _pending_tx_queue.pop();
      }
    }

    _flushPendingTxQueue();
  }
}

void BridgeClass::_handleMalformed(uint16_t command_id) {
  if (command_id == rpc::RPC_INVALID_ID_SENTINEL || command_id == _last_command_id) _retransmitLastFrame();
}

void BridgeClass::_retransmitLastFrame() {
  if (_fsm.isAwaitingAck() && !_pending_tx_queue.empty()) {
    const PendingTxFrame& frame = _pending_tx_queue.front();
    
    etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE> raw_buffer;
    rpc::FrameBuilder builder;
    size_t raw_len = builder.build(
        raw_buffer, 
        frame.command_id, 
        etl::span<const uint8_t>(frame.payload.data(), frame.payload_length));
    
    if (raw_len > 0) {
      _packetSerial.send(raw_buffer.data(), raw_len);
      _retry_count++;
      flushStream();
    }
  }
}

void BridgeClass::_onAckTimeout() {
    if (!_fsm.isAwaitingAck()) return;

    if (_retry_count >= _ack_retry_limit) {
      if (_status_handler.is_valid()) _status_handler(rpc::StatusCode::STATUS_TIMEOUT, nullptr, 0);
      _fsm.timeout();  // Transition to Unsynchronized via FSM
      enterSafeState(); 
      return;
    }
    
    _retransmitLastFrame();
    
    // Restart timer for next retry
    _timer_service.start(bridge::scheduler::TIMER_ACK_TIMEOUT, false);
}

void BridgeClass::_onRxDedupe() {
  // [SIL-2] Reset RX deduplication state to allow accepting retried frames.
  // This timer fires periodically to prevent stale CRC from blocking legitimate retries.
  _last_rx_crc = 0;
  _last_rx_crc_millis = 0;
}

void BridgeClass::_onBaudrateChange() {
      if (_pending_baudrate > 0) {
        if (_hardware_serial != nullptr) {
            _hardware_serial->flush();
            _hardware_serial->end();
            _hardware_serial->begin(_pending_baudrate);
        }
        _pending_baudrate = 0;
      }
}

void BridgeClass::_onStartupStabilized() {
  // [SIL-2] Non-blocking startup stabilization complete
  // Final drain of any remaining garbage in the buffer
  // [SIL-2] Bounded drain to ensure determinism (max 256 bytes)
  uint16_t drain_limit = 256;
  while (_stream.available() > 0 && drain_limit-- > 0) { _stream.read(); }
  _startup_stabilizing = false;
}

void BridgeClass::enterSafeState() {
  _fsm.resetFsm();  // Transition to Unsynchronized via ETL FSM
  _timer_service.stop(bridge::scheduler::TIMER_ACK_TIMEOUT);
  _timer_service.stop(bridge::scheduler::TIMER_STARTUP_STABILIZATION);
  _startup_stabilizing = false;
  
  // Note: _clearAckState() checks FSM state, so we skip to avoid redundant transition
  _retry_count = 0;
  _clearPendingTxQueue();
  _frame_received = false;
  _target_frame = nullptr;
  _last_command_id = 0;
  _last_rx_crc = 0;
  _last_rx_crc_millis = 0;
  _consecutive_crc_errors = 0;
}

void BridgeClass::_sendAckAndFlush(uint16_t command_id) {
  _sendAck(command_id);
  flushStream();
}

void BridgeClass::_flushPendingTxQueue() {
  bool empty = false;
  BRIDGE_ATOMIC_BLOCK {
    empty = _pending_tx_queue.empty();
  }

  if (_fsm.isAwaitingAck() || empty) return;
  
  PendingTxFrame frame{};
  BRIDGE_ATOMIC_BLOCK {
    frame = _pending_tx_queue.front();
  }

  etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE> raw_buffer;
  rpc::FrameBuilder builder;
  size_t raw_len = builder.build(
      raw_buffer, 
      frame.command_id, 
      etl::span<const uint8_t>(frame.payload.data(), frame.payload_length));
  
  if (raw_len > 0) {
    _packetSerial.send(raw_buffer.data(), raw_len);
    flushStream();

    _fsm.sendCritical();  // Transition to AwaitingAck via FSM
    _retry_count = 0;
    
    // [SIL-2] Start ACK Timer
    _timer_service.set_period(bridge::scheduler::TIMER_ACK_TIMEOUT, _ack_timeout_ms);
    _timer_service.start(bridge::scheduler::TIMER_ACK_TIMEOUT, false);
    
    _last_command_id = frame.command_id;
    // NOTE: We do NOT pop here. We pop only when ACK is received.
  }
}

void BridgeClass::_clearPendingTxQueue() { 
  while (true) {
    bool empty = false;
    BRIDGE_ATOMIC_BLOCK {
      empty = _pending_tx_queue.empty();
      if (!empty) _pending_tx_queue.pop();
    }
    if (empty) break;
  }
}

void BridgeClass::_computeHandshakeTag(const uint8_t* nonce, size_t nonce_len, uint8_t* out_tag) {
  if (_shared_secret.empty() || nonce_len == 0 || !nonce) {
    etl::fill_n(out_tag, kHandshakeTagSize, uint8_t{0});
    return;
  }

  // [MIL-SPEC] Use HKDF derived key for handshake authentication.
  // [RAM OPT] Allocate scratch buffer on stack (key + digest)
  etl::array<uint8_t, BRIDGE_KEY_AND_DIGEST_BUFFER_SIZE> key_and_digest;
  uint8_t* handshake_key = key_and_digest.data();                        // BRIDGE_HKDF_KEY_LENGTH bytes
  uint8_t* digest = key_and_digest.data() + BRIDGE_HKDF_KEY_LENGTH;      // BRIDGE_HKDF_KEY_LENGTH bytes

  rpc::security::hkdf_sha256(
      _shared_secret.data(), _shared_secret.size(),
      rpc::RPC_HANDSHAKE_HKDF_SALT, rpc::RPC_HANDSHAKE_HKDF_SALT_LEN,
      rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH, rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH_LEN,
      handshake_key, BRIDGE_HKDF_KEY_LENGTH);

  SHA256 sha256;
  sha256.resetHMAC(handshake_key, BRIDGE_HKDF_KEY_LENGTH);
  sha256.update(nonce, nonce_len);
  sha256.finalizeHMAC(handshake_key, BRIDGE_HKDF_KEY_LENGTH, digest, kSha256DigestSize);
  etl::copy_n(digest, kHandshakeTagSize, out_tag);
  
  rpc::security::secure_zero(handshake_key, BRIDGE_HKDF_KEY_LENGTH);
  rpc::security::secure_zero(digest, kSha256DigestSize);
}

void BridgeClass::_applyTimingConfig(const uint8_t* payload, size_t length) {
  uint16_t ack_timeout_ms = rpc::RPC_DEFAULT_ACK_TIMEOUT_MS;
  uint8_t retry_limit = rpc::RPC_DEFAULT_RETRY_LIMIT;
  uint32_t response_timeout_ms = rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS;
  if (payload != nullptr && length >= rpc::payload::HandshakeConfig::SIZE) {
    auto config = rpc::payload::HandshakeConfig::parse(payload);
    ack_timeout_ms = config.ack_timeout_ms;
    retry_limit = config.ack_retry_limit;
    response_timeout_ms = config.response_timeout_ms;
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

// [SIL-2] ETL Error Handler Implementation
namespace etl {
void __attribute__((weak)) handle_error(const etl::exception& e) {
  (void)e;
  Bridge.enterSafeState();
}
}
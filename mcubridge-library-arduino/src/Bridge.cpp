/*
 * This file is part of Arduino MCU Ecosystem v2.
 */
#include "Bridge.h"

// [SIL-2] Explicitly include Arduino.h to satisfy IntelliSense and ensure
// noInterrupts()/interrupts() are available in all compilation contexts.
#include <Arduino.h>
#include <etl/numeric.h>
#include <etl/span.h>
#include <etl/bitset.h>

// --- [SAFETY GUARD START] ---
// CRITICAL: Prevent accidental standard STL usage on ALL architectures (memory
// fragmentation risk) SIL 2 Requirement: Dynamic allocation via standard STL
// containers is forbidden globally. We explicitly allow ETL (Embedded Template
// Library) as it uses static allocation.
#if (defined(_GLIBCXX_VECTOR) || defined(_GLIBCXX_STRING) || \
     defined(_GLIBCXX_MAP)) &&                               \
    !defined(ETL_VERSION) && !defined(BRIDGE_HOST_TEST)
#error \
    "CRITICAL: Standard STL detected. Use ETL or standard arrays/pointers only to prevent heap fragmentation (SIL 2 Violation)."
#endif
// --- [SAFETY GUARD END] ---

#ifdef ARDUINO_ARCH_AVR
#include <avr/wdt.h>
#endif

#include <string.h>


#include "etl/algorithm.h"
#include "etl/error_handler.h"
#include "hal/logging.h"
#include "protocol/PacketBuilder.h"
#include "protocol/rle.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "security/security.h"

// [OPTIMIZATION] Numerical status codes used instead of PROGMEM strings.

namespace {
constexpr size_t kHandshakeTagSize = rpc::RPC_HANDSHAKE_TAG_LENGTH;
static_assert(kHandshakeTagSize > 0,
              "RPC_HANDSHAKE_TAG_LENGTH must be greater than zero");
constexpr size_t kSha256DigestSize = 32;
#if defined(ARDUINO_ARCH_AVR)
constexpr uint8_t kCrcFailResetWatchdogTimeout = WDTO_15MS;
#endif

}  // namespace

#ifndef BRIDGE_TEST_NO_GLOBALS
// [SIL-2] Robust Hardware Serial Detection via macro override or architecture
// defaults
BridgeClass Bridge(BRIDGE_DEFAULT_SERIAL_PORT);

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
      _shared_secret(),
      _cobs{0, 0, 0, 0, true, {0}},
      _frame_received(false),
      _rx_frame{},
      _rng(millis()),
      _last_command_id(0),
      _retry_count(0),
      _pending_baudrate(0),
      _rx_history(),
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
      _timers(),
      _last_tick_millis(0),
      _startup_stabilizing(false) {
  _timers.clear();
}

void BridgeClass::begin(unsigned long arg_baudrate, etl::string_view arg_secret,
                        size_t arg_secret_len) {
  // [SIL-2] Start the ETL FSM before any other initialization
  _fsm.begin();

  // [RAM-OPT] Initialize SimpleTimer (replaces etl::callback_timer<4>)
  _timers.clear();
  _timers.set_period(bridge::scheduler::TIMER_ACK_TIMEOUT, _ack_timeout_ms);
  _timers.set_period(bridge::scheduler::TIMER_RX_DEDUPE, BRIDGE_RX_DEDUPE_INTERVAL_MS);
  _timers.set_period(bridge::scheduler::TIMER_BAUDRATE_CHANGE, BRIDGE_BAUDRATE_SETTLE_MS);
  _timers.set_period(bridge::scheduler::TIMER_STARTUP_STABILIZATION, BRIDGE_STARTUP_STABILIZATION_MS);
  _last_tick_millis = static_cast<uint32_t>(millis());

  // [SIL-2] Memory Integrity POST
  // Verify COBS buffer is functional
  etl::iota(_cobs.buffer.begin(), _cobs.buffer.end(), 0);
  uint16_t post_i = 0;
  if (!etl::all_of(_cobs.buffer.begin(), _cobs.buffer.end(),
                   [&post_i](uint8_t b) {
                     return b == static_cast<uint8_t>(post_i++ & 0xFF);
                   })) {
    enterSafeState();
    _fsm.cryptoFault();  // Use general fault state
    return;
  }
  _cobs.buffer.fill(0);

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
// bypassing the HardwareSerial check. We must explicitly initialize it.
#if BRIDGE_USE_USB_SERIAL
  Serial.begin(arg_baudrate);
#endif

  if (_hardware_serial != nullptr) {
    _hardware_serial->begin(arg_baudrate);
  }

  // [SIL-2] Non-blocking Startup Stabilization
  // Start timer and set flag - process() will drain the buffer during this
  // period
  _startup_stabilizing = true;
  _timers.start(bridge::scheduler::TIMER_STARTUP_STABILIZATION,
                static_cast<uint32_t>(millis()));

  _shared_secret.clear();
  if (!arg_secret.empty()) {
    size_t actual_len =
        (arg_secret_len > 0) ? arg_secret_len : arg_secret.length();
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
  _rx_history.clear();

  // [SIL-2] Register Observers
  add_observer(Console);
#if BRIDGE_ENABLE_DATASTORE
  add_observer(DataStore);
#endif
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

  if (_startup_stabilizing) {
    uint8_t drain_limit = BRIDGE_STARTUP_DRAIN_PER_TICK;
    while (_stream.available() > 0 && drain_limit-- > 0) {
      _stream.read();
    }
  } else {
    // [SIL-2] Streaming COBS Decoder (Zero-Copy parser)
    BRIDGE_ATOMIC_BLOCK {
      while (_stream.available() > 0) {
        uint8_t byte = _stream.read();

        if (byte == rpc::RPC_FRAME_DELIMITER) {
          if (_cobs.in_sync && _cobs.bytes_received >= 5 + rpc::CRC_TRAILER_SIZE) {
            etl::crc32 crc_calc;
            const size_t data_len = _cobs.bytes_received - rpc::CRC_TRAILER_SIZE;
            crc_calc.add(&_cobs.buffer[0], &_cobs.buffer[data_len]);
            
            uint32_t received_crc = rpc::read_u32_be(&_cobs.buffer[data_len]);

            if (crc_calc.value() == received_crc) {
              _rx_frame.header.version = _cobs.buffer[0];
              _rx_frame.header.payload_length = rpc::read_u16_be(&_cobs.buffer[1]);
              _rx_frame.header.command_id = rpc::read_u16_be(&_cobs.buffer[3]);
              _rx_frame.crc = received_crc;

              if (_rx_frame.header.version == rpc::PROTOCOL_VERSION &&
                  _rx_frame.header.payload_length <= rpc::MAX_PAYLOAD_SIZE &&
                  data_len == static_cast<size_t>(_rx_frame.header.payload_length + 5)) {
                if (_rx_frame.header.payload_length > 0) {
                  etl::copy_n(&_cobs.buffer[5], _rx_frame.header.payload_length, _rx_frame.payload.begin());
                }
                _frame_received = true;
                _last_parse_error.reset();
              } else {
                _last_parse_error = rpc::FrameError::MALFORMED;
              }
            } else {
              _last_parse_error = rpc::FrameError::CRC_MISMATCH;
            }
          } else if (_cobs.in_sync && _cobs.bytes_received > 0) {
            _last_parse_error = rpc::FrameError::MALFORMED;
          }

          _cobs.bytes_received = 0;
          _cobs.block_len = 0;
          _cobs.in_sync = true;
          _cobs.code_prev = 0;

          if (_frame_received || _last_parse_error.has_value()) break;
          continue;
        }

        if (!_cobs.in_sync) continue;

        if (_cobs.block_len == 0) {
          _cobs.code = byte;
          _cobs.block_len = byte - 1;
          if (_cobs.bytes_received > 0 && _cobs.code_prev != 0xFF) {
            if (_cobs.bytes_received < rpc::MAX_RAW_FRAME_SIZE) {
              _cobs.buffer[_cobs.bytes_received++] = 0x00;
            } else {
              _cobs.in_sync = false;
              _last_parse_error = rpc::FrameError::OVERFLOW;
            }
          }
          _cobs.code_prev = _cobs.code;
        } else {
          if (_cobs.bytes_received < rpc::MAX_RAW_FRAME_SIZE) {
            _cobs.buffer[_cobs.bytes_received++] = byte;
            _cobs.block_len--;
          } else {
            _cobs.in_sync = false;
            _last_parse_error = rpc::FrameError::OVERFLOW;
          }
        }
      }
    }
  }

  if (_frame_received) {
    BRIDGE_ATOMIC_BLOCK { _consecutive_crc_errors = 0; }
    _frame_received = false;
    dispatch(_rx_frame);
  } else if (_last_parse_error.has_value()) {
#if BRIDGE_HOST_TEST
    fprintf(stderr, "[MCU DECODE ERROR] Code: %d\n", (int)_last_parse_error.value());
#endif
    rpc::FrameError error = _last_parse_error.value();
    if (error == rpc::FrameError::CRC_MISMATCH) {
      BRIDGE_ATOMIC_BLOCK { _consecutive_crc_errors++; }
      if (_consecutive_crc_errors >= BRIDGE_MAX_CONSECUTIVE_CRC_ERRORS) {
#if defined(ARDUINO_ARCH_AVR)
        wdt_enable(kCrcFailResetWatchdogTimeout);
        for (;;) {
        }
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
          emitStatus(rpc::StatusCode::STATUS_CRC_MISMATCH);
          break;
        case rpc::FrameError::MALFORMED:
        case rpc::FrameError::OVERFLOW:
          emitStatus(rpc::StatusCode::STATUS_MALFORMED);
          break;
      }
    }
    _last_parse_error.reset();
  }

  // [RAM-OPT] SimpleTimer tick — check expired timers and dispatch
  const uint32_t now = static_cast<uint32_t>(millis());
  _last_tick_millis = now;
  const uint8_t expired = _timers.check_expired(now);
  if (expired & (1U << bridge::scheduler::TIMER_ACK_TIMEOUT))
    _onAckTimeout();
  if (expired & (1U << bridge::scheduler::TIMER_RX_DEDUPE))
    _onRxDedupe();
  if (expired & (1U << bridge::scheduler::TIMER_BAUDRATE_CHANGE))
    _onBaudrateChange();
  if (expired & (1U << bridge::scheduler::TIMER_STARTUP_STABILIZATION))
    _onStartupStabilized();

  _flushPendingTxQueue();
}

// [SIL-2] Pin Validation Helper
// Using bridge::hal::isValidPin directly in implementation

void BridgeClass::dispatch(const rpc::Frame& frame) {
#if BRIDGE_HOST_TEST
  fprintf(stderr, "[MCU DECODE] dispatch cmd=0x%02X len=%u\n", frame.header.command_id, frame.header.payload_length);
#endif
  // [SIL-2] Phase 1: Decompress if needed
  uint16_t raw_command = frame.header.command_id;
  bool is_compressed = (raw_command & rpc::RPC_CMD_FLAG_COMPRESSED) != 0;
  raw_command &= ~rpc::RPC_CMD_FLAG_COMPRESSED;

  rpc::Frame effective_frame;
  effective_frame.header = frame.header;
  effective_frame.header.command_id = raw_command;
  effective_frame.crc = frame.crc;

  if (is_compressed && frame.header.payload_length > 0) {
    etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> scratch_payload;
    size_t decoded_len = rle::decode(
        etl::span<const uint8_t>(frame.payload.data(),
                                 frame.header.payload_length),
        etl::span<uint8_t>(scratch_payload.data(), rpc::MAX_PAYLOAD_SIZE));
    if (decoded_len == 0) {
      emitStatus(rpc::StatusCode::STATUS_MALFORMED);
      return;
    }
    etl::copy_n(scratch_payload.data(), decoded_len,
                effective_frame.payload.data());
    effective_frame.header.payload_length = static_cast<uint16_t>(decoded_len);
  } else {
    if (frame.header.payload_length > 0) {
      etl::copy_n(frame.payload.data(), frame.header.payload_length,
                  effective_frame.payload.data());
    }
  }

  // [SIL-2] Phase 2: Build context
  bridge::router::CommandContext ctx(&effective_frame, raw_command,
                                     _isRecentDuplicateRx(effective_frame),
                                     rpc::requires_ack(raw_command));

  // [SIL-2] Security/Safety Gate: Only allow handshake commands if not
  // operational.
  if (!_fsm.isSynchronized() && !_isHandshakeCommand(raw_command)) {
    sendFrame(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  // [SIL-2] Phase 3: O(1) Dispatch via Category Switch
  const uint16_t category = (ctx.raw_command - rpc::RPC_STATUS_CODE_MIN) >> 4;
  switch (category) {
    case 0: onStatusCommand(ctx); break;      // 48-63
    case 1: onSystemCommand(ctx); break;      // 64-79
    case 2: onGpioCommand(ctx); break;        // 80-95
    case 3: onConsoleCommand(ctx); break;     // 96-111
    case 4: onDataStoreCommand(ctx); break;   // 112-127
    case 5: onMailboxCommand(ctx); break;     // 128-143
    case 6: onFileSystemCommand(ctx); break;  // 144-159
    case 7: onProcessCommand(ctx); break;     // 160-175
    default: onUnknownCommand(ctx); break;
  }
}

// ============================================================================
// [SIL-2] ICommandHandler Implementation - ETL Message Router Callbacks
// ============================================================================

void BridgeClass::onStatusCommand(const bridge::router::CommandContext& ctx) {
  // [SIL-2] O(1) Dispatch via Switch for Status Codes
  const uint16_t status_val = ctx.raw_command;
  switch (status_val - rpc::RPC_STATUS_CODE_MIN) {
    case 3: _handleStatusMalformed(ctx); break;  // 51
    case 8: _handleStatusAck(ctx); break;         // 56
    default: break;
  }

  if (_status_handler.is_valid()) {
    _status_handler(static_cast<rpc::StatusCode>(status_val),
                    etl::span<const uint8_t>(ctx.frame->payload.data(),
                                             ctx.frame->header.payload_length));
  }
}

void BridgeClass::_handleStatusAck(const bridge::router::CommandContext& ctx) {
  auto msg = rpc::Payload::parse<rpc::payload::AckPacket>(*ctx.frame);
  _handleAck(msg ? msg->command_id : rpc::RPC_INVALID_ID_SENTINEL);
}

void BridgeClass::_handleStatusMalformed(
    const bridge::router::CommandContext& ctx) {
  auto msg = rpc::Payload::parse<rpc::payload::AckPacket>(*ctx.frame);
  _handleMalformed(msg ? msg->command_id : rpc::RPC_INVALID_ID_SENTINEL);
}

void BridgeClass::onSystemCommand(const bridge::router::CommandContext& ctx) {
  // [SIL-2] O(1) Dispatch via Switch for System Commands
  // Index = (cmd - MIN) / 2 (commands are even, responses are odd)
  const uint16_t cmd = ctx.raw_command;
  if (cmd >= rpc::RPC_SYSTEM_COMMAND_MIN &&
      cmd <= rpc::RPC_SYSTEM_COMMAND_MAX) {
    switch ((cmd - rpc::RPC_SYSTEM_COMMAND_MIN) >> 1) {
      case 0: _handleGetVersion(ctx); break;       // 64
      case 1: _handleGetFreeMemory(ctx); break;    // 66
      case 2: _handleLinkSync(ctx); break;         // 68
      case 3: _handleLinkReset(ctx); break;        // 70
      case 4: _handleGetCapabilities(ctx); break;  // 72
      case 5: _handleSetBaudrate(ctx); break;      // 74
      default: break;
    }
  }
}

void BridgeClass::_handleGetVersion(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [&]() {
    _sendResponse<rpc::payload::VersionResponse>(
        rpc::CommandId::CMD_GET_VERSION_RESP, kDefaultFirmwareVersionMajor,
        kDefaultFirmwareVersionMinor);
  });
}

void BridgeClass::_handleGetFreeMemory(
    const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [&]() {
    _sendResponse<rpc::payload::FreeMemoryResponse>(
        rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, getFreeMemory());
  });
}

void BridgeClass::_handleGetCapabilities(
    const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [&]() {
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

    uint8_t dig = 0;
#ifdef NUM_DIGITAL_PINS
    dig = static_cast<uint8_t>(NUM_DIGITAL_PINS);
#endif

    uint8_t ana = 0;
#ifdef NUM_ANALOG_INPUTS
    ana = static_cast<uint8_t>(NUM_ANALOG_INPUTS);
#endif

    etl::bitset<32> features;
    features.set(0);  // RLE Bit (1 << 0) - Always enabled
    if (kBridgeEnableWatchdog) features.set(1);  // Watchdog Bit (1 << 1)

#if BRIDGE_DEBUG_FRAMES
    features.set(2);
#endif
#if BRIDGE_DEBUG_IO
    features.set(3);
#endif
#if defined(E2END) && (E2END > 0)
    features.set(4);
#endif
#if (defined(DAC_OUTPUT_CHANNELS) && (DAC_OUTPUT_CHANNELS > 0)) || \
    defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM) ||     \
    defined(ARDUINO_ARCH_ESP32)
    features.set(5);
#endif
#if defined(HAVE_HWSERIAL1)
    features.set(6);
#endif
#if defined(__FPU_PRESENT) && (__FPU_PRESENT == 1)
    features.set(7);
#endif
#if defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM) ||      \
    defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_ESP8266) || \
    defined(ARDUINO_ARCH_RP2040)
    features.set(8);
#endif
#if defined(SERIAL_RX_BUFFER_SIZE) && (SERIAL_RX_BUFFER_SIZE > 64)
    features.set(9);
#endif
#if defined(PIN_WIRE_SDA) || defined(SDA) || defined(DT) || \
    defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_ESP8266)
    features.set(10);
#endif
#if defined(SCK) || defined(MOSI) || defined(MISO) || \
    defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_ESP8266)
    features.set(11);
#endif

    _sendResponse<rpc::payload::Capabilities>(
        rpc::CommandId::CMD_GET_CAPABILITIES_RESP, rpc::PROTOCOL_VERSION, arch,
        dig, ana, static_cast<uint32_t>(features.to_ulong()));
  });
}

void BridgeClass::_handleSetBaudrate(const bridge::router::CommandContext& ctx) {
  _withPayloadResponse<rpc::payload::SetBaudratePacket>(
      ctx, [&](const rpc::payload::SetBaudratePacket& msg) {
        (void)sendFrame(rpc::CommandId::CMD_SET_BAUDRATE_RESP);
        flushStream();
        _pending_baudrate = msg.baudrate;
        _timers.start_with_period(bridge::scheduler::TIMER_BAUDRATE_CHANGE,
                                  BRIDGE_BAUDRATE_SETTLE_MS,
                                  static_cast<uint32_t>(millis()));
      });
}

void BridgeClass::_handleLinkSync(const bridge::router::CommandContext& ctx) {
  const size_t nonce_length = rpc::RPC_HANDSHAKE_NONCE_LENGTH;
  const bool has_secret = !_shared_secret.empty();
  const size_t expected_payload =
      nonce_length + (has_secret ? kHandshakeTagSize : 0);

  if (ctx.frame->header.payload_length != expected_payload) {
    emitStatus(rpc::StatusCode::STATUS_MALFORMED);
    return;
  }

  _withResponse(ctx, [&]() {
    enterSafeState();
    _fsm.handshakeStart();  // Transition to Syncing state

    const uint8_t* payload_data = ctx.frame->payload.data();
    if (has_secret) {
      bool bypass = (_shared_secret.size() == 14 &&
                     memcmp(_shared_secret.data(), "DEBUG_INSECURE", 14) == 0);
      if (!bypass) {
        etl::array<uint8_t, kHandshakeTagSize> expected_tag;
        _computeHandshakeTag(
            etl::span<const uint8_t>(payload_data, nonce_length),
            expected_tag.data());
        if (!rpc::security::timing_safe_equal(payload_data + nonce_length,
                                              expected_tag.data(),
                                              kHandshakeTagSize)) {
          emitStatus(rpc::StatusCode::STATUS_ERROR, F("Mutual Auth Failed"));
          enterSafeState();
          _fsm.handshakeFailed();
          return;
        }
      }
    }

    const size_t response_length =
        nonce_length + (has_secret ? kHandshakeTagSize : 0);
    etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buffer;

    if (payload_data) {
      etl::copy_n(payload_data, nonce_length, buffer.begin());
      if (has_secret) {
        etl::array<uint8_t, kHandshakeTagSize> tag;
        _computeHandshakeTag(
            etl::span<const uint8_t>(payload_data, nonce_length), tag.data());
        etl::copy_n(tag.begin(), kHandshakeTagSize,
                    buffer.begin() + nonce_length);
      }
      (void)sendFrame(rpc::CommandId::CMD_LINK_SYNC_RESP,
                      etl::span<const uint8_t>(buffer.data(), response_length));
      _fsm.handshakeComplete();
      notify_observers(MsgBridgeSynchronized());
    }
  });
}

void BridgeClass::_handleLinkReset(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [&]() {
    enterSafeState();
    if (ctx.frame->header.payload_length ==
        rpc::payload::HandshakeConfig::SIZE) {
      _applyTimingConfig(etl::span<const uint8_t>(
          ctx.frame->payload.data(), ctx.frame->header.payload_length));
    }
    (void)sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP);
  });
}

void BridgeClass::onGpioCommand(const bridge::router::CommandContext& ctx) {
  // [SIL-2] O(1) Dispatch via Switch for GPIO Commands
  switch (ctx.raw_command - rpc::RPC_GPIO_COMMAND_MIN) {
    case 0: _handleSetPinMode(ctx); break;    // 80
    case 1: _handleDigitalWrite(ctx); break;  // 81
    case 2: _handleAnalogWrite(ctx); break;   // 82
    case 3: _handleDigitalRead(ctx); break;   // 83
    case 4: _handleAnalogRead(ctx); break;    // 84
    default: break;
  }
}

void BridgeClass::_handleSetPinMode(const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::PinMode>(
      ctx, [](const rpc::payload::PinMode& msg) {
        if (bridge::hal::isValidPin(msg.pin)) ::pinMode(msg.pin, msg.mode);
      });
}

void BridgeClass::_handleDigitalWrite(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::DigitalWrite>(
      ctx, [](const rpc::payload::DigitalWrite& msg) {
        if (bridge::hal::isValidPin(msg.pin))
          ::digitalWrite(msg.pin, msg.value ? HIGH : LOW);
      });
}

void BridgeClass::_handleAnalogWrite(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::AnalogWrite>(
      ctx, [](const rpc::payload::AnalogWrite& msg) {
        if (bridge::hal::isValidPin(msg.pin)) ::analogWrite(msg.pin, msg.value);
      });
}

void BridgeClass::_handleDigitalRead(const bridge::router::CommandContext& ctx) {
  _handlePinRead<rpc::payload::DigitalReadResponse>(
      ctx, rpc::CommandId::CMD_DIGITAL_READ_RESP,
      [](uint8_t pin) { return bridge::hal::isValidPin(pin); },
      [](uint8_t pin) -> uint8_t {
        return static_cast<uint8_t>(::digitalRead(pin) & rpc::RPC_UINT8_MASK);
      });
}

void BridgeClass::_handleAnalogRead(const bridge::router::CommandContext& ctx) {
  _handlePinRead<rpc::payload::AnalogReadResponse>(
      ctx, rpc::CommandId::CMD_ANALOG_READ_RESP,
      [](uint8_t pin) {
#ifdef NUM_ANALOG_INPUTS
        return pin < NUM_ANALOG_INPUTS;
#else
        return bridge::hal::isValidPin(pin);
#endif
      },
      [](uint8_t pin) -> uint16_t {
        return static_cast<uint16_t>(::analogRead(pin) & rpc::RPC_UINT16_MAX);
      });
}

void BridgeClass::onConsoleCommand(const bridge::router::CommandContext& ctx) {
  if (ctx.raw_command ==
      rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE)) {
    _handleConsoleWrite(ctx);
  }
}

void BridgeClass::_handleConsoleWrite(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::ConsoleWrite>(
      ctx, [](const rpc::payload::ConsoleWrite& msg) {
        Console._push(etl::span<const uint8_t>(msg.data, msg.length));
      });
}

void BridgeClass::onDataStoreCommand(
    const bridge::router::CommandContext& ctx) {
  if (ctx.raw_command ==
      rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP)) {
    _withPayload<rpc::payload::DatastoreGetResponse>(
        ctx, [](const rpc::payload::DatastoreGetResponse& msg) {
#if BRIDGE_ENABLE_DATASTORE
          if (DataStore._datastore_get_handler.is_valid()) {
            etl::string_view key = DataStore._popPendingDatastoreKey();
            if (!key.empty()) {
              DataStore._datastore_get_handler(
                  key, etl::span<const uint8_t>(msg.value, msg.value_len));
            }
          }
#endif
        });
  }
}

void BridgeClass::onMailboxCommand(const bridge::router::CommandContext& ctx) {
  // [SIL-2] O(1) Dispatch via Switch for Mailbox Commands
  switch (ctx.raw_command - (rpc::RPC_MAILBOX_COMMAND_MIN + 3)) {
    case 0: _handleMailboxPush(ctx); break;           // 131
    case 1: _handleMailboxReadResp(ctx); break;       // 132
    case 2: _handleMailboxAvailableResp(ctx); break;  // 133
    default: break;
  }
}

void BridgeClass::_handleMailboxPush(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::MailboxPush>(
      ctx, [](const rpc::payload::MailboxPush& msg) {
#if BRIDGE_ENABLE_MAILBOX
        Mailbox._onIncomingData(etl::span<const uint8_t>(msg.data, msg.length));
#endif
      });
}

void BridgeClass::_handleMailboxReadResp(
    const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::MailboxReadResponse>(
      ctx, [](const rpc::payload::MailboxReadResponse& msg) {
#if BRIDGE_ENABLE_MAILBOX
        Mailbox._onIncomingData(
            etl::span<const uint8_t>(msg.content, msg.length));
#endif
      });
}

void BridgeClass::_handleMailboxAvailableResp(
    const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::MailboxAvailableResponse>(
      ctx, [](const rpc::payload::MailboxAvailableResponse& msg) {
#if BRIDGE_ENABLE_MAILBOX
        if (Mailbox._mailbox_available_handler.is_valid()) {
          Mailbox._mailbox_available_handler(msg.count);
        }
#endif
      });
}

void BridgeClass::_handleFileWrite(const bridge::router::CommandContext& ctx) {
  _withAck(ctx, []() { /* No payload processing needed for ACK-only write */ });
}

void BridgeClass::_handleFileReadResp(
    const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::FileReadResponse>(
      ctx, [](const rpc::payload::FileReadResponse& msg) {
#if BRIDGE_ENABLE_FILESYSTEM
        if (FileSystem._file_system_read_handler.is_valid()) {
          FileSystem._file_system_read_handler(
              etl::span<const uint8_t>(msg.content, msg.length));
        }
#endif
      });
}

void BridgeClass::onFileSystemCommand(
    const bridge::router::CommandContext& ctx) {
  // [SIL-2] O(1) Dispatch via Switch for File System Commands
  switch (ctx.raw_command - rpc::RPC_FILESYSTEM_COMMAND_MIN) {
    case 0: _handleFileWrite(ctx); break;     // 144
    case 3: _handleFileReadResp(ctx); break;  // 147
    default: break;
  }
}

void BridgeClass::onProcessCommand(const bridge::router::CommandContext& ctx) {
  // [SIL-2] O(1) Dispatch via Switch for Process Commands
  switch (ctx.raw_command - (rpc::RPC_PROCESS_COMMAND_MIN + 5)) {
    case 0: _handleProcessRunAsyncResp(ctx); break;  // 165
    case 1: _handleProcessPollResp(ctx); break;      // 166
    default: break;
  }
}

void BridgeClass::_handleProcessRunAsyncResp(
    const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::ProcessRunAsyncResponse>(
      ctx, [](const rpc::payload::ProcessRunAsyncResponse& msg) {
#if BRIDGE_ENABLE_PROCESS
        if (Process._process_run_async_handler.is_valid()) {
          Process._process_run_async_handler(static_cast<int16_t>(msg.pid));
        }
#endif
      });
}

void BridgeClass::_handleProcessPollResp(
    const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::ProcessPollResponse>(
      ctx, [](const rpc::payload::ProcessPollResponse& msg) {
#if BRIDGE_ENABLE_PROCESS
        if (Process._process_poll_handler.is_valid()) {
          Process._process_poll_handler(
              static_cast<rpc::StatusCode>(msg.status), msg.exit_code,
              etl::span<const uint8_t>(msg.stdout_data, msg.stdout_len),
              etl::span<const uint8_t>(msg.stderr_data, msg.stderr_len));
          Process._popPendingProcessPid();
        }
#endif
      });
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
  _sendResponse<rpc::payload::AckPacket>(rpc::StatusCode::STATUS_ACK,
                                         command_id);
}

void BridgeClass::_doEmitStatus(rpc::StatusCode status_code,
                                etl::span<const uint8_t> payload) {
  (void)sendFrame(status_code, payload);
  if (_status_handler.is_valid()) _status_handler(status_code, payload);

  // [SIL-2] Notify Observers
  notify_observers(MsgBridgeError{status_code});
}

void BridgeClass::emitStatus(rpc::StatusCode status_code,
                             etl::string_view message) {
  etl::span<const uint8_t> payload;
  if (!message.empty()) {
    payload = etl::span<const uint8_t>(
        reinterpret_cast<const uint8_t*>(message.data()),
        etl::min(message.length(), rpc::MAX_PAYLOAD_SIZE));
  }
  _doEmitStatus(status_code, payload);
}

void BridgeClass::emitStatus(rpc::StatusCode status_code,
                             const __FlashStringHelper* message) {
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE>
      buffer;  // [RAM OPT] Stack allocation
  size_t length = 0;

  if (message) {
    const char* p = reinterpret_cast<const char*>(message);
    length = strnlen_P(p, rpc::MAX_PAYLOAD_SIZE);
    memcpy_P(buffer.data(), p, length);
  }
  _doEmitStatus(status_code, etl::span<const uint8_t>(buffer.data(), length));
}

#include "protocol/rpc_cobs.h"

void BridgeClass::_sendRawFrame(uint16_t command_id,
                                etl::span<const uint8_t> payload) {
  etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE> raw_buffer;
  rpc::FrameBuilder builder;
  size_t raw_len = builder.build(raw_buffer, command_id, payload);

  if (raw_len > 0) {
#if BRIDGE_DEBUG_IO
    // [SIL-2] Safety Guard: Prefer logging to Console if primary stream is
    // Serial
    Print* log_stream = &Serial;
#if BRIDGE_ENABLE_CONSOLE
    if (_hardware_serial == &Serial) log_stream = &Console;
#endif
    bridge::logging::log_traffic(*log_stream, "[SERIAL -> MCU]", "RAW",
                                 raw_buffer.data(), raw_len);
#endif

    etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE + 2> cobs_buffer;
    size_t encoded_len = rpc::cobs::encode(
        etl::span<const uint8_t>(raw_buffer.data(), raw_len),
        etl::span<uint8_t>(cobs_buffer.data(), cobs_buffer.size()));

    if (encoded_len > 0) {
      _stream.write(cobs_buffer.data(), encoded_len);
      _stream.write(rpc::RPC_FRAME_DELIMITER);
      flushStream();
    }
  }
}

bool BridgeClass::sendFrame(rpc::CommandId command_id,
                            etl::span<const uint8_t> payload) {
  return _sendFrame(rpc::to_underlying(command_id), payload);
}

bool BridgeClass::sendFrame(rpc::StatusCode status_code,
                            etl::span<const uint8_t> payload) {
  return _sendFrame(rpc::to_underlying(status_code), payload);
}

bool BridgeClass::sendStringCommand(rpc::CommandId command_id,
                                    etl::string_view str, size_t max_len) {
  if (str.empty() || str.length() > max_len ||
      str.length() >= rpc::MAX_PAYLOAD_SIZE)
    return false;

  etl::vector<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload;
  rpc::PacketBuilder(payload).add_pascal_string(str);
  return sendFrame(command_id,
                   etl::span<const uint8_t>(payload.data(), payload.size()));
}

bool BridgeClass::sendKeyValCommand(rpc::CommandId command_id,
                                    etl::string_view key, size_t max_key,
                                    etl::string_view val, size_t max_val) {
  if (key.empty() || key.length() > max_key || val.length() > max_val)
    return false;
  if (key.length() + val.length() + 2 > rpc::MAX_PAYLOAD_SIZE) return false;

  etl::vector<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload;
  rpc::PacketBuilder(payload).add_pascal_string(key).add_pascal_string(val);
  return sendFrame(command_id,
                   etl::span<const uint8_t>(payload.data(), payload.size()));
}

bool BridgeClass::sendChunkyFrame(rpc::CommandId command_id,
                                  etl::span<const uint8_t> header,
                                  etl::span<const uint8_t> data) {
  if (header.size() >= rpc::MAX_PAYLOAD_SIZE) return false;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buffer;
  const size_t max_chunk = rpc::MAX_PAYLOAD_SIZE - header.size();
  size_t offset = 0;

  do {
    const size_t chunk_size = etl::min(data.size() - offset, max_chunk);
    if (header.size() > 0)
      etl::copy_n(header.data(), header.size(), buffer.begin());
    if (chunk_size > 0 && data.data())
      etl::copy_n(data.data() + offset, chunk_size,
                  buffer.begin() + header.size());

    if (!_sendFrame(rpc::to_underlying(command_id),
                    etl::span<const uint8_t>(buffer.data(),
                                             header.size() + chunk_size)))
      return false;
    offset += chunk_size;
  } while (offset < data.size());

  return true;
}
bool BridgeClass::_isHandshakeCommand(uint16_t command_id) const {
  // [SIL-2] Protocol Security: Only allow specific commands during pre-sync phase.
  
  // Define allowed ranges
  struct Range { uint16_t min; uint16_t max; };
  static constexpr Range allowed_ranges[] = {
      {rpc::RPC_STATUS_CODE_MIN, rpc::RPC_STATUS_CODE_MAX},
      {rpc::RPC_SYSTEM_COMMAND_MIN, rpc::RPC_SYSTEM_COMMAND_MAX}
  };
  
  // Define specific allowed IDs
  static constexpr uint16_t allowed_ids[] = {
      rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION_RESP),
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC_RESP),
      rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET_RESP)
  };

  // Check if command is in any allowed range using ETL algorithms
  if (etl::any_of(etl::begin(allowed_ranges), etl::end(allowed_ranges),
                  [command_id](const Range& r) {
                    return command_id >= r.min && command_id <= r.max;
                  })) {
    return true;
  }

  // Check if command matches any specific allowed ID using ETL algorithms
  return etl::any_of(etl::begin(allowed_ids), etl::end(allowed_ids),
                     [command_id](uint16_t id) { return id == command_id; });
}

bool BridgeClass::_sendFrame(uint16_t command_id,
                             etl::span<const uint8_t> payload) {
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
  etl::span<const uint8_t> final_payload = payload;

  // [RAM OPT] Stack allocation for compression buffer
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> scratch_payload;

  if (payload.size() > 0 && rle::should_compress(payload)) {
    size_t compressed_len = rle::encode(payload, scratch_payload);
    if (compressed_len > 0 && compressed_len < payload.size()) {
      final_cmd |= rpc::RPC_CMD_FLAG_COMPRESSED;
      final_payload = etl::span<const uint8_t>(scratch_payload.data(),
                                               compressed_len);
    }
  }

  const bool critical =
      rpc::requires_ack(final_cmd & ~rpc::RPC_CMD_FLAG_COMPRESSED);

  // [SIL-2] State-Driven Sending Logic via ETL FSM
  if (critical) {
    // [SIL-2] ISR Protection for Queue Access
    bool queue_full = false;
    BRIDGE_ATOMIC_BLOCK { queue_full = _pending_tx_queue.full(); }

    if (queue_full || final_payload.size() > rpc::MAX_PAYLOAD_SIZE) {
      return false;
    }

    // Inlined _enqueuePendingTx
    PendingTxFrame frame;
    frame.command_id = final_cmd;
    frame.payload_length = static_cast<uint16_t>(final_payload.size());
    if (final_payload.size() > 0 && final_payload.data())
      etl::copy_n(final_payload.data(), final_payload.size(),
                  frame.payload.data());

    BRIDGE_ATOMIC_BLOCK { _pending_tx_queue.push(frame); }

    // If we are not waiting for an ACK, we can start sending this frame
    // immediately. _flushPendingTxQueue will pick it up (it's at the front).
    if (!_fsm.isAwaitingAck()) {
      _flushPendingTxQueue();
    }
    return true;
  }

  // Non-critical frame: Send immediately using stack buffer
  _sendRawFrame(final_cmd, final_payload);
  return true;
}

void BridgeClass::_clearAckState() {
  if (_fsm.isAwaitingAck()) {
    _fsm.ackReceived();  // Transition back to Idle
  }
  _retry_count = 0;
}

void BridgeClass::_handleAck(uint16_t command_id) {
  if (_fsm.isAwaitingAck() && (command_id == rpc::RPC_INVALID_ID_SENTINEL ||
                               command_id == _last_command_id)) {
    _clearAckState();

    // [SIL-2] Stop ACK Timer
    _timers.stop(bridge::scheduler::TIMER_ACK_TIMEOUT);

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
  if (command_id == rpc::RPC_INVALID_ID_SENTINEL ||
      command_id == _last_command_id)
    _retransmitLastFrame();
}

void BridgeClass::_retransmitLastFrame() {
  if (_fsm.isAwaitingAck() && !_pending_tx_queue.empty()) {
    const PendingTxFrame& frame = _pending_tx_queue.front();
    _sendRawFrame(frame.command_id, etl::span<const uint8_t>(
                                        frame.payload.data(),
                                        frame.payload_length));
    _retry_count++;
  }
}

void BridgeClass::_onAckTimeout() {
  if (!_fsm.isAwaitingAck()) return;

  if (_retry_count >= _ack_retry_limit) {
    if (_status_handler.is_valid())
      _status_handler(rpc::StatusCode::STATUS_TIMEOUT,
                      etl::span<const uint8_t>());
    _fsm.timeout();  // Transition to Unsynchronized via FSM
    enterSafeState();
    return;
  }

  _retransmitLastFrame();

  // Restart timer for next retry
  _timers.start(bridge::scheduler::TIMER_ACK_TIMEOUT,
                static_cast<uint32_t>(millis()));
}

void BridgeClass::_onRxDedupe() {
  // [SIL-2] Reset RX deduplication state to allow accepting retried frames.
  // This timer fires periodically to prevent stale CRC from blocking legitimate
  // retries.
  _rx_history.clear();
}

void BridgeClass::_onBaudrateChange() {
  if (_pending_baudrate > 0) {
#ifndef BRIDGE_HOST_TEST
    if (_hardware_serial != nullptr) {
      _hardware_serial->flush();
      _hardware_serial->end();
      _hardware_serial->begin(_pending_baudrate);
    }
#endif
    _pending_baudrate = 0;
  }
}

void BridgeClass::_onStartupStabilized() {
  // [SIL-2] Non-blocking startup stabilization complete
  // Final drain of any remaining garbage in the buffer
  // [SIL-2] Bounded drain to ensure determinism
  uint16_t drain_limit = BRIDGE_STARTUP_DRAIN_FINAL;
  while (_stream.available() > 0 && drain_limit-- > 0) {
    _stream.read();
  }
  _startup_stabilizing = false;
}

void BridgeClass::enterSafeState() {
  _fsm.resetFsm();  // Transition to Unsynchronized via ETL FSM
  _timers.stop(bridge::scheduler::TIMER_ACK_TIMEOUT);
  _timers.stop(bridge::scheduler::TIMER_STARTUP_STABILIZATION);
  _timers.stop(bridge::scheduler::TIMER_BAUDRATE_CHANGE);
  _startup_stabilizing = false;
  _pending_baudrate = 0;

  // Note: _clearAckState() checks FSM state, so we skip to avoid redundant
  // transition
  _retry_count = 0;
  _clearPendingTxQueue();
  _frame_received = false;
  _last_command_id = 0;
  _rx_history.clear();
  _consecutive_crc_errors = 0;

#if BRIDGE_ENABLE_PROCESS
  Process.reset();
#endif

  // [SIL-2] Notify Observers of lost connection
  notify_observers(MsgBridgeLost());
}

void BridgeClass::_sendAckAndFlush(uint16_t command_id) {
  _sendAck(command_id);
  flushStream();
}

void BridgeClass::_flushPendingTxQueue() {
  bool empty = false;
  BRIDGE_ATOMIC_BLOCK { empty = _pending_tx_queue.empty(); }

  if (_fsm.isAwaitingAck() || empty) return;

  PendingTxFrame frame{};
  BRIDGE_ATOMIC_BLOCK { frame = _pending_tx_queue.front(); }

  _sendRawFrame(
      frame.command_id,
      etl::span<const uint8_t>(frame.payload.data(), frame.payload_length));
  _fsm.sendCritical();  // Transition to AwaitingAck via FSM
  _retry_count = 0;

  // [SIL-2] Start ACK Timer
  _timers.start_with_period(bridge::scheduler::TIMER_ACK_TIMEOUT,
                            _ack_timeout_ms,
                            static_cast<uint32_t>(millis()));

  _last_command_id = frame.command_id;
  // NOTE: We do NOT pop here. We pop only when ACK is received.
}

void BridgeClass::_clearPendingTxQueue() {
  BRIDGE_ATOMIC_BLOCK {
    while (!_pending_tx_queue.empty()) {
      _pending_tx_queue.pop();
    }
  }
}

void BridgeClass::_computeHandshakeTag(etl::span<const uint8_t> nonce,
                                       uint8_t* out_tag) {
  if (_shared_secret.empty() || nonce.empty()) {
    etl::fill_n(out_tag, kHandshakeTagSize, uint8_t{0});
    return;
  }

  // [MIL-SPEC] Use HKDF derived key for handshake authentication.
  // [RAM OPT] Allocate scratch buffer on stack (key + digest)
  etl::array<uint8_t, BRIDGE_KEY_AND_DIGEST_BUFFER_SIZE> key_and_digest;
  uint8_t* handshake_key =
      key_and_digest.data();  // BRIDGE_HKDF_KEY_LENGTH bytes
  uint8_t* digest = key_and_digest.data() +
                    BRIDGE_HKDF_KEY_LENGTH;  // BRIDGE_HKDF_KEY_LENGTH bytes

  ::hkdf<SHA256>(handshake_key, BRIDGE_HKDF_KEY_LENGTH, _shared_secret.data(),
                 _shared_secret.size(), rpc::RPC_HANDSHAKE_HKDF_SALT,
                 rpc::RPC_HANDSHAKE_HKDF_SALT_LEN,
                 rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH,
                 rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH_LEN);

  SHA256 sha256;
  sha256.resetHMAC(handshake_key, BRIDGE_HKDF_KEY_LENGTH);
  sha256.update(nonce.data(), nonce.size());
  sha256.finalizeHMAC(handshake_key, BRIDGE_HKDF_KEY_LENGTH, digest,
                      kSha256DigestSize);
  etl::copy_n(digest, kHandshakeTagSize, out_tag);

  rpc::security::secure_zero(handshake_key, BRIDGE_HKDF_KEY_LENGTH);
  rpc::security::secure_zero(digest, kSha256DigestSize);
}

void BridgeClass::_applyTimingConfig(etl::span<const uint8_t> payload) {
  uint16_t ack_timeout_ms = rpc::RPC_DEFAULT_ACK_TIMEOUT_MS;
  uint8_t retry_limit = rpc::RPC_DEFAULT_RETRY_LIMIT;
  uint32_t response_timeout_ms = rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS;

  if (!payload.empty() && payload.size() >= rpc::payload::HandshakeConfig::SIZE) {
    auto config = rpc::payload::HandshakeConfig::parse(payload.data());
    ack_timeout_ms = etl::clamp(config.ack_timeout_ms, 
                                static_cast<uint16_t>(rpc::RPC_HANDSHAKE_ACK_TIMEOUT_MIN_MS),
                                static_cast<uint16_t>(rpc::RPC_HANDSHAKE_ACK_TIMEOUT_MAX_MS));
    retry_limit = etl::clamp(config.ack_retry_limit,
                             static_cast<uint8_t>(rpc::RPC_HANDSHAKE_RETRY_LIMIT_MIN),
                             static_cast<uint8_t>(rpc::RPC_HANDSHAKE_RETRY_LIMIT_MAX));
    response_timeout_ms = etl::clamp(config.response_timeout_ms,
                                     rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS,
                                     rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS);
  }

  _ack_timeout_ms = ack_timeout_ms;
  _ack_retry_limit = retry_limit;
  _response_timeout_ms = response_timeout_ms;
}

bool BridgeClass::_isRecentDuplicateRx(const rpc::Frame& frame) const {
  if (frame.crc == 0) return false;

  auto it =
      etl::find_if(_rx_history.begin(), _rx_history.end(),
                   [&frame](const RxHistory& r) { return r.crc == frame.crc; });

  if (it != _rx_history.end()) {
    const uint32_t elapsed = static_cast<uint32_t>(millis()) - it->timestamp;
    if (_ack_timeout_ms > 0 &&
        elapsed < static_cast<uint32_t>(_ack_timeout_ms))
      return false;
    return elapsed <= (static_cast<uint32_t>(_ack_timeout_ms) *
                       (_ack_retry_limit + 1));
  }
  return false;
}

void BridgeClass::_markRxProcessed(const rpc::Frame& frame) {
  _rx_history.push(RxHistory{frame.crc, static_cast<uint32_t>(millis())});
}

// [SIL-2] ETL Error Handler Implementation
namespace etl {
void __attribute__((weak)) handle_error(const etl::exception& e) {
  (void)e;
  Bridge.enterSafeState();
}
}  // namespace etl

#include "Bridge.h"
#include "services/SPIService.h"
#include <Arduino.h>
#include <etl/numeric.h>
#include <etl/span.h>
#include "util/pb_copy.h"

#if (defined(_GLIBCXX_VECTOR) || defined(_GLIBCXX_STRING) || \
     defined(_GLIBCXX_MAP)) &&                               \
    !defined(ETL_VERSION) && !defined(BRIDGE_HOST_TEST)
#error "CRITICAL: Standard STL detected. Use ETL only (SIL 2 Violation)."
#endif

#if defined(ARDUINO_ARCH_AVR)
#include <avr/wdt.h>
#elif defined(ARDUINO_ARCH_ESP32)
#include <esp_task_wdt.h>
#elif defined(ARDUINO_ARCH_ESP8266)
#include <Arduino.h>
#endif

#include <string.h>
#include <etl/algorithm.h>
// #include <etl/error_handler.h>
#include "hal/logging.h"
#include "protocol/rle.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "security/security.h"

namespace {
constexpr size_t kHandshakeTagSize = rpc::RPC_HANDSHAKE_TAG_LENGTH;
constexpr uint8_t kRpcCommandStride = 2;  // Pair: CMD + RESP

constexpr uint8_t bit_index_from_mask(uint32_t mask) {
  uint8_t bit_index = 0;
  while (mask > 1U) {
    mask /= 2U;
    ++bit_index;
  }
  return bit_index;
}

constexpr uint8_t kCompressedCommandBit =
    bit_index_from_mask(rpc::RPC_CMD_FLAG_COMPRESSED);
}

BridgeClass::BridgeClass(HardwareSerial& arg_serial)
    : BridgeClass(static_cast<Stream&>(arg_serial)) {
  _hardware_serial = &arg_serial;
}

BridgeClass::BridgeClass(Stream& arg_stream)
    : _stream(arg_stream),
      _hardware_serial(nullptr),
      _shared_secret(),
      _cobs{rpc::RxState::AWAITING_SYNC, 0, {0}},
      _frame_builder(),
      _last_parse_error(),
      _flags(),
      _rx_frame{},
      _rng(millis()),
      _last_command_id(0),
      _tx_sequence_id(0),
      _retry_count(0),
      _pending_baudrate(0),
      _rx_fifo(),
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
      _tx_pool_head(0),
      _fsm(),
      _timers(),
      _last_tick_millis(0) {
  _flags.reset();
  _timers.clear();
}

void BridgeClass::begin(unsigned long arg_baudrate, etl::string_view arg_secret,
                        size_t arg_secret_len) {
  bridge::hal::init();
  _fsm.begin();
  _timers.clear();
  _rx_history.clear();
  _timers.set_period(bridge::scheduler::TIMER_ACK_TIMEOUT, _ack_timeout_ms);
  _timers.set_period(bridge::scheduler::TIMER_RX_DEDUPE, bridge::config::RX_DEDUPE_INTERVAL_MS);
  _timers.set_period(bridge::scheduler::TIMER_BAUDRATE_CHANGE, bridge::config::BAUDRATE_SETTLE_MS);
  _timers.set_period(bridge::scheduler::TIMER_STARTUP_STABILIZATION, bridge::config::STARTUP_STABILIZATION_MS);
  _last_tick_millis = bridge::now_ms();

  _cobs.buffer.fill(0);
  _cobs.state = rpc::RxState::AWAITING_SYNC;
  _cobs.bytes_received = 0;

  if (!rpc::security::run_cryptographic_self_tests()) {
    enterSafeState();
    _fsm.cryptoFault();
    return;
  }

#if BRIDGE_USE_USB_SERIAL
  Serial.begin(arg_baudrate);
#endif

  if (_hardware_serial != nullptr) {
    _hardware_serial->begin(arg_baudrate);
  }

  _timers.start(bridge::scheduler::TIMER_STARTUP_STABILIZATION, bridge::now_ms());

  _shared_secret.clear();
  if (!arg_secret.empty()) {
    size_t actual_len = (arg_secret_len > 0) ? arg_secret_len : arg_secret.length();
    if (actual_len > _shared_secret.capacity()) actual_len = _shared_secret.capacity();
    const uint8_t* start = reinterpret_cast<const uint8_t*>(arg_secret.data());
    // [SIL-2/C++14] Use etl::copy with back_inserter for safe and bounded assignment
    etl::copy(start, start + actual_len, etl::back_inserter(_shared_secret));
  }

  _fsm.resetFsm();
  _last_command_id = 0;
  _tx_sequence_id = 0;
  _retry_count = 0;
  _rx_history.clear();

  add_observer(Console);
#if BRIDGE_ENABLE_DATASTORE
  add_observer(DataStore);
#endif
}

void BridgeClass::process() {
#if BRIDGE_ENABLE_WATCHDOG
  #if defined(ARDUINO_ARCH_AVR)
    wdt_reset();
  #elif defined(ARDUINO_ARCH_ESP32)
    esp_task_wdt_reset();
  #elif defined(ARDUINO_ARCH_ESP8266)
    yield();
  #endif
#endif

  const uint32_t now = bridge::now_ms();
  const auto expired = _timers.check_expired(now);
  if (expired.test(bridge::scheduler::TIMER_ACK_TIMEOUT)) _onAckTimeout();
  if (expired.test(bridge::scheduler::TIMER_RX_DEDUPE)) _onRxDedupe();
  if (expired.test(bridge::scheduler::TIMER_BAUDRATE_CHANGE)) _onBaudrateChange();
  if (expired.test(bridge::scheduler::TIMER_STARTUP_STABILIZATION)) _onStartupStabilized();

  if (_fsm.isStabilizing()) {
    uint16_t drain_limit = bridge::config::STARTUP_DRAIN_PER_TICK;
    while (_stream.available() > 0 && drain_limit-- > 0) {
        static_cast<void>(_stream.read());
    }
  } else {
    // [SIL-2] Serial ISR Isolation: Quick drain to FIFO, then process.
    // Includes a 50ms timeout to avoid blocking execution.
    const uint32_t drain_start = bridge::now_ms();
    while (_stream.available() > 0 && !_rx_fifo.full() && (bridge::now_ms() - drain_start < 50)) {
      uint8_t byte;
      BRIDGE_ATOMIC_BLOCK { byte = static_cast<uint8_t>(_stream.read()); }
      _rx_fifo.push(byte);
    }

    while (!_rx_fifo.empty()) {
      const uint8_t byte = _rx_fifo.front();
      _rx_fifo.pop();
      _processIncomingByte(byte);
      if (_flags.test(bridge::FlagId::FRAME_RECEIVED) || 
          _cobs.state == rpc::RxState::FRAME_READY ||
          _last_parse_error.has_value()) break;
    }
  }

  if (_cobs.state == rpc::RxState::FRAME_READY) {
      _handleReceivedFrame();
  } else if (_last_parse_error.has_value()) {
    const rpc::FrameError error = _last_parse_error.value();
    _last_parse_error.reset();
    if (error == rpc::FrameError::CRC_MISMATCH) {
      if (++_consecutive_crc_errors >= bridge::config::MAX_CONSECUTIVE_CRC_ERRORS) {
#if defined(ARDUINO_ARCH_AVR)
        wdt_enable(WDTO_15MS); for (;;) {}
#else
        enterSafeState();
#endif
      }
    }
  }
}

void BridgeClass::forceSafeState() {
  // [SIL-2] Force all pins to safe state (Input with Pullups)
  uint8_t digital_pins = 0;
  uint8_t analog_pins = 0;
  bridge::hal::getPinCounts(digital_pins, analog_pins);

  for (uint8_t i = 0; i < digital_pins; ++i) {
    pinMode(i, INPUT_PULLUP);
  }
#if defined(ARDUINO_ARCH_AVR)
  wdt_enable(WDTO_2S);
#endif
}

void BridgeClass::_processIncomingByte(const uint8_t byte) {
  if (byte == rpc::RPC_FRAME_DELIMITER) {
      if (_cobs.state == rpc::RxState::RECEIVING && _cobs.bytes_received >= rpc::MIN_FRAME_SIZE) {
          _cobs.state = rpc::RxState::FRAME_READY;
      } else {
          _cobs.state = rpc::RxState::RECEIVING;
          _cobs.bytes_received = 0;
      }
      return;
  }

  switch (_cobs.state) {
      case rpc::RxState::RECEIVING:
          if (_cobs.bytes_received >= _cobs.buffer.size()) {
              _cobs.state = rpc::RxState::OVERFLOW;
              _last_parse_error = rpc::FrameError::OVERFLOW;
          } else {
              _cobs.buffer[_cobs.bytes_received++] = byte;
          }
          break;
      case rpc::RxState::AWAITING_SYNC:
      case rpc::RxState::OVERFLOW:
      case rpc::RxState::FRAME_READY:
          // Ignore bytes in these states until next delimiter
          break;
  }
}

void BridgeClass::_handleReceivedFrame() {
  _cobs.state = rpc::RxState::RECEIVING; // Reset FSM for next frame immediately

  // [OPTIMIZATION] Use shared transient buffer for decoding
  const size_t decoded_len = rpc::cobs::decode(
      etl::span<const uint8_t>(_cobs.buffer.data(), _cobs.bytes_received),
      etl::span<uint8_t>(_transient_buffer.data(), _transient_buffer.size()));

  _cobs.bytes_received = 0;

  if (decoded_len > 0) {
      rpc::FrameParser parser;
      const auto result = parser.parse(etl::span<const uint8_t>(_transient_buffer.data(), decoded_len));
      if (result.has_value()) {
          _rx_frame = result.value();
          _consecutive_crc_errors = 0;

          const uint16_t sequence_id = _rx_frame.header.sequence_id;
          if (_rx_history.contains(sequence_id)) {
              if (rpc::requires_ack(_rx_frame.header.command_id)) {
                  _sendAckAndFlush(_rx_frame.header.command_id, sequence_id);
              }
              return;
          }
          _dispatchCommand(_rx_frame, sequence_id);
      } else {
          _last_parse_error = result.error();
      }
  } else {
      _last_parse_error = rpc::FrameError::MALFORMED;
  }
}

void BridgeClass::_dispatchCommand(const rpc::Frame& frame, uint16_t sequence_id) {
  rpc::Frame effective_frame;
  auto decomp_res = _decompressFrame(frame, effective_frame);
  if (!decomp_res.has_value()) {
    emitStatus(rpc::StatusCode::STATUS_MALFORMED);
    return;
  }

  uint16_t raw_cmd = effective_frame.header.command_id;
  if (!_isSecurityCheckPassed(raw_cmd)) {
    sendFrame(rpc::StatusCode::STATUS_ERROR, sequence_id);
    return;
  }

  bridge::router::CommandContext ctx(&effective_frame, raw_cmd,
                                     false, // Dedupe already checked in _handleReceivedFrame
                                     rpc::requires_ack(raw_cmd),
                                     sequence_id);

  notify_observers(MsgBridgeCommand{raw_cmd, sequence_id, effective_frame.payload});

  static constexpr etl::array<void (BridgeClass::*)(const bridge::router::CommandContext&), 9> kGroupHandlers{{
      &BridgeClass::onStatusCommand,    // 0x30
      &BridgeClass::onSystemCommand,    // 0x40
      &BridgeClass::onGpioCommand,      // 0x50
      &BridgeClass::onConsoleCommand,   // 0x60
      &BridgeClass::onDataStoreCommand, // 0x70
      &BridgeClass::onMailboxCommand,   // 0x80
      &BridgeClass::onFileSystemCommand,// 0x90
      &BridgeClass::onProcessCommand,   // 0xA0
      &BridgeClass::onSpiCommand        // 0xB0
  }};

  const uint8_t group_idx = (raw_cmd >> 4) - 3;
  if (group_idx < kGroupHandlers.size()) {
    (this->*kGroupHandlers[group_idx])(ctx);
  } else {
    onUnknownCommand(ctx);
  }

  _markRxProcessed(effective_frame);
}

bool BridgeClass::_isSecurityCheckPassed(uint16_t command_id) const {
  if (_fsm.isSynchronized()) return true;
  return _isHandshakeCommand(command_id);
}

void BridgeClass::onStatusCommand(const bridge::router::CommandContext& ctx) {
  static constexpr etl::array<CmdHandler, 9> kStatusHandlers{{
      &BridgeClass::_unusedCommandSlot,     // 48: STATUS_OK
      &BridgeClass::_unusedCommandSlot,     // 49: STATUS_ERROR
      &BridgeClass::_unusedCommandSlot,     // 50: STATUS_CMD_UNKNOWN
      &BridgeClass::_handleStatusMalformed, // 51: MALFORMED
      &BridgeClass::_unusedCommandSlot,     // 52
      &BridgeClass::_unusedCommandSlot,     // 53
      &BridgeClass::_unusedCommandSlot,     // 54
      &BridgeClass::_unusedCommandSlot,     // 55
      &BridgeClass::_handleStatusAck        // 56: ACK
  }};
  _dispatchJumpTable(ctx, rpc::RPC_STATUS_CODE_MIN, kStatusHandlers.data(), kStatusHandlers.size());

  if (_status_handler.is_valid()) {
    _status_handler(static_cast<rpc::StatusCode>(ctx.raw_command), ctx.frame->payload);
  }
}

void BridgeClass::onSystemCommand(const bridge::router::CommandContext& ctx) {
  static constexpr etl::array<CmdHandler, 13> kSystemHandlers{{
      &BridgeClass::_handleGetVersion,      // 0: 64
      &BridgeClass::_unusedCommandSlot,     // 1: 65
      &BridgeClass::_handleGetFreeMemory,   // 2: 66
      &BridgeClass::_unusedCommandSlot,     // 3: 67
      &BridgeClass::_handleLinkSync,        // 4: 68
      &BridgeClass::_unusedCommandSlot,     // 5: 69
      &BridgeClass::_handleLinkReset,       // 6: 70
      &BridgeClass::_unusedCommandSlot,     // 7: 71
      &BridgeClass::_handleGetCapabilities, // 8: 72
      &BridgeClass::_unusedCommandSlot,     // 9: 73
      &BridgeClass::_handleSetBaudrate,     // 10: 74
      &BridgeClass::_unusedCommandSlot,     // 11: 75
      &BridgeClass::_handleEnterBootloader  // 12: 76
  }};
  _dispatchJumpTable(ctx, rpc::RPC_SYSTEM_COMMAND_MIN, kSystemHandlers.data(), kSystemHandlers.size());
}

void BridgeClass::onGpioCommand(const bridge::router::CommandContext& ctx) {
  static constexpr etl::array<CmdHandler, 5> kGpioHandlers{{
      &BridgeClass::_handleSetPinMode, &BridgeClass::_handleDigitalWrite,
      &BridgeClass::_handleAnalogWrite, &BridgeClass::_handleDigitalRead,
      &BridgeClass::_handleAnalogRead
  }};
  _dispatchJumpTable(ctx, rpc::RPC_GPIO_COMMAND_MIN, kGpioHandlers.data(), kGpioHandlers.size());
}

void BridgeClass::onConsoleCommand(const bridge::router::CommandContext& ctx) {
  static constexpr etl::array<CmdHandler, 1> kConsoleHandlers{{
      &BridgeClass::_handleConsoleWrite
  }};
  _dispatchJumpTable(ctx, rpc::RPC_CONSOLE_COMMAND_MIN, kConsoleHandlers.data(), kConsoleHandlers.size());
}

void BridgeClass::onDataStoreCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_DATASTORE) {
    static constexpr etl::array<CmdHandler, 3> kDataStoreHandlers{{
        &BridgeClass::_unusedCommandSlot, // 112
        &BridgeClass::_unusedCommandSlot, // 113
        &BridgeClass::_handleDatastoreGetResp // 114
    }};
    _dispatchJumpTable(ctx, rpc::RPC_DATASTORE_COMMAND_MIN, kDataStoreHandlers.data(), kDataStoreHandlers.size());
  } else {
    (void)ctx;
    emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
  }
}

void BridgeClass::onMailboxCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_MAILBOX) {
    static constexpr etl::array<CmdHandler, 6> kMailboxHandlers{{
        &BridgeClass::_unusedCommandSlot, // 128
        &BridgeClass::_unusedCommandSlot, // 129
        &BridgeClass::_unusedCommandSlot, // 130
        &BridgeClass::_handleMailboxPush, // 131
        &BridgeClass::_handleMailboxReadResp, // 132
        &BridgeClass::_handleMailboxAvailableResp // 133
    }};
    _dispatchJumpTable(ctx, rpc::RPC_MAILBOX_COMMAND_MIN, kMailboxHandlers.data(), kMailboxHandlers.size());
  } else {
    (void)ctx;
    emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
  }
}

void BridgeClass::onFileSystemCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_FILESYSTEM) {
    static constexpr etl::array<CmdHandler, 4> kFsHandlers{{
        &BridgeClass::_handleFileWrite, // 144
        &BridgeClass::_handleFileRead, // 145
        &BridgeClass::_handleFileRemove, // 146
        &BridgeClass::_handleFileReadResp // 147
    }};
    _dispatchJumpTable(ctx, rpc::RPC_FILESYSTEM_COMMAND_MIN, kFsHandlers.data(), kFsHandlers.size());
  } else {
    (void)ctx;
    emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
  }
}

void BridgeClass::onProcessCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_PROCESS) {
    static constexpr etl::array<CmdHandler, 7> kProcessHandlers{{
        &BridgeClass::_unusedCommandSlot, // 160
        &BridgeClass::_unusedCommandSlot, // 161
        &BridgeClass::_handleProcessKill, // 162
        &BridgeClass::_unusedCommandSlot, // 163
        &BridgeClass::_unusedCommandSlot, // 164
        &BridgeClass::_handleProcessRunAsyncResp, // 165
        &BridgeClass::_handleProcessPollResp // 166
    }};
    _dispatchJumpTable(ctx, rpc::RPC_PROCESS_COMMAND_MIN, kProcessHandlers.data(), kProcessHandlers.size());
  } else {
    (void)ctx;
    emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
  }
}

void BridgeClass::onSpiCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_SPI) {
    static constexpr etl::array<CmdHandler, 5> kSpiHandlers{{
        &BridgeClass::_handleSpiBegin,
        &BridgeClass::_handleSpiTransfer,
        &BridgeClass::_unusedCommandSlot,
        &BridgeClass::_handleSpiEnd,
        &BridgeClass::_handleSpiSetConfig
    }};
    _dispatchJumpTable(ctx, rpc::RPC_SPI_COMMAND_MIN, kSpiHandlers.data(), kSpiHandlers.size());
  } else {
    (void)ctx;
    emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
  }
}

void BridgeClass::_handleSpiBegin(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_SPI) {
    _withAck(ctx, []() { SPIService.begin(); });
  } else {
    (void)ctx;
  }
}

void BridgeClass::_handleSpiEnd(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_SPI) {
    _withAck(ctx, []() { SPIService.end(); });
  } else {
    (void)ctx;
  }
}

void BridgeClass::_handleSpiSetConfig(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_SPI) {
    _withPayloadAck<rpc::payload::SpiConfig>(ctx, [](const rpc::payload::SpiConfig& msg) {
      uint8_t bitOrder = (msg.bit_order == 0) ? 0 : 1;
      uint8_t dataMode = static_cast<uint8_t>(msg.data_mode);
      SPIService.setConfig(msg.frequency, bitOrder, dataMode);
    });
  } else {
    (void)ctx;
  }
}

void BridgeClass::_handleSpiTransfer(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_SPI) {
    if (ctx.is_duplicate) return;
    rpc::payload::SpiTransfer req = {};
    static etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buffer;
    etl::span<uint8_t> decode_span(buffer.data(), buffer.size());
    rpc::util::pb_setup_decode_span(req.data, decode_span);
    auto res = rpc::Payload::parse<rpc::payload::SpiTransfer>(*ctx.frame, req);
    if (res.has_value()) {
      if (SPIService.isInitialized()) {
        size_t len = decode_span.size();
        if (len > 0) SPIService.transfer(buffer.data(), len);
        rpc::payload::SpiTransferResponse resp = {};
        etl::span<const uint8_t> out_span(buffer.data(), len);
        rpc::util::pb_setup_encode_span(resp.data, out_span);
        _sendPbResponse(rpc::CommandId::CMD_SPI_TRANSFER_RESP, ctx.sequence_id, resp);
      }
    }
  } else {
    (void)ctx;
  }
}

void BridgeClass::_handleEnterBootloader(const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::EnterBootloader>(ctx, [this](const rpc::payload::EnterBootloader& msg) {
    if (msg.magic == 0xDEADC0DE) {
      this->flushStream();
      delay(100);
#if defined(ARDUINO_ARCH_AVR)
      wdt_enable(WDTO_15MS); for (;;) {}
#elif defined(ARDUINO_ARCH_ESP32)
      ESP.restart();
#elif defined(ARDUINO_ARCH_SAMD)
      NVIC_SystemReset();
#endif
    }
  });
}

void BridgeClass::_handleGetVersion(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc::payload::VersionResponse resp = {};
    resp.major = bridge::config::FIRMWARE_VERSION_MAJOR;
    resp.minor = bridge::config::FIRMWARE_VERSION_MINOR;
    resp.patch = bridge::config::FIRMWARE_VERSION_PATCH;
    _sendPbResponse(rpc::CommandId::CMD_GET_VERSION_RESP, ctx.sequence_id, resp);
  });
}

void BridgeClass::_handleGetFreeMemory(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc::payload::FreeMemoryResponse resp = {};
    resp.value = getFreeMemory();
    _sendPbResponse(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, ctx.sequence_id, resp);
  });
}

void BridgeClass::_handleLinkSync(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::LinkSync>(ctx, [this, &ctx](const rpc::payload::LinkSync& msg) {
    etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> tag;
    _computeHandshakeTag(etl::span<const uint8_t>(msg.nonce, rpc::RPC_HANDSHAKE_NONCE_LENGTH), etl::span<uint8_t>(tag.data(), tag.size()));
    if (!_shared_secret.empty()) {
      etl::span<const uint8_t> expected(tag.data(), tag.size());
      etl::span<const uint8_t> received(msg.tag, rpc::RPC_HANDSHAKE_TAG_LENGTH);
      if (!rpc::security::timing_safe_equal(expected, received)) {
#if defined(BRIDGE_HOST_TEST)
        printf("[DEBUG] Handshake Tag Mismatch!\n");
        printf("  Expected: "); for(int i=0; i<16; i++) printf("%02X ", expected[i]); printf("\n");
        printf("  Received: "); for(int i=0; i<16; i++) printf("%02X ", received[i]); printf("\n");
#endif
        _fsm.handshakeStart(); _fsm.handshakeFailed(); return;
      }
    }
    rpc::payload::LinkSync resp = {};
    etl::copy_n(msg.nonce, rpc::RPC_HANDSHAKE_NONCE_LENGTH, resp.nonce);
    etl::copy_n(tag.data(), tag.size(), resp.tag);
    _fsm.handshakeStart(); _fsm.handshakeComplete();
    _sendPbResponse(rpc::CommandId::CMD_LINK_SYNC_RESP, ctx.sequence_id, resp);
    notify_observers(MsgBridgeSynchronized());
  });
}

void BridgeClass::_handleLinkReset(const bridge::router::CommandContext& ctx) {
  _withAck(ctx, [this, &ctx]() {
    enterSafeState();
    sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP, ctx.sequence_id);
  });
}

void BridgeClass::_handleGetCapabilities(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc::payload::Capabilities resp = {};
    resp.ver = rpc::PROTOCOL_VERSION;
    resp.arch = bridge::hal::getArchId();
    bridge::hal::getPinCounts(resp.dig, resp.ana);
    resp.feat = bridge::hal::getCapabilities();
    _sendPbResponse(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, ctx.sequence_id, resp);
  });
}

void BridgeClass::_handleSetBaudrate(const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::SetBaudratePacket>(ctx, [this](const rpc::payload::SetBaudratePacket& msg) {
    _pending_baudrate = msg.baudrate;
    _timers.start(bridge::scheduler::TIMER_BAUDRATE_CHANGE, bridge::now_ms());
  });
}

void BridgeClass::_handleSetPinMode(const bridge::router::CommandContext& ctx) {
  _handlePinSetter<rpc::payload::PinMode>(ctx, [](const auto& msg) { ::pinMode(msg.pin, msg.mode); });
}

void BridgeClass::_handleDigitalWrite(const bridge::router::CommandContext& ctx) {
  _handlePinSetter<rpc::payload::DigitalWrite>(ctx, [](const auto& msg) { ::digitalWrite(msg.pin, msg.value); });
}

void BridgeClass::_handleAnalogWrite(const bridge::router::CommandContext& ctx) {
  _handlePinSetter<rpc::payload::AnalogWrite>(ctx, [](const auto& msg) { ::analogWrite(msg.pin, msg.value); });
}

void BridgeClass::_handleDigitalRead(const bridge::router::CommandContext& ctx) {
  _handlePinRead<rpc::payload::DigitalReadResponse>(ctx, rpc::CommandId::CMD_DIGITAL_READ_RESP, bridge::hal::isValidPin, [](uint8_t p) { return ::digitalRead(p); });
}

void BridgeClass::_handleAnalogRead(const bridge::router::CommandContext& ctx) {
  _handlePinRead<rpc::payload::AnalogReadResponse>(ctx, rpc::CommandId::CMD_ANALOG_READ_RESP, bridge::hal::isValidPin, [](uint8_t p) { return ::analogRead(p); });
}

void BridgeClass::_handleConsoleWrite(const bridge::router::CommandContext& ctx) {
  _dispatchWithBytes<rpc::payload::ConsoleWrite>(ctx, &rpc::payload::ConsoleWrite::data, [](auto s) { Console._push(s); }, true);
}

void BridgeClass::_handleDatastoreGetResp(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_DATASTORE
  _dispatchWithBytes<rpc::payload::DatastoreGetResponse>(ctx, &rpc::payload::DatastoreGetResponse::value, [](auto s) { DataStore._onResponse(s); });
#else
  (void)ctx;
  emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
#endif
}

void BridgeClass::_handleMailboxPush(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_MAILBOX
  _dispatchWithBytes<rpc::payload::MailboxPush>(ctx, &rpc::payload::MailboxPush::data, [](auto s) { Mailbox._onIncomingData(s); }, true);
#else
  (void)ctx;
  emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
#endif
}

void BridgeClass::_handleMailboxReadResp(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_MAILBOX
  _dispatchWithBytes<rpc::payload::MailboxReadResponse>(ctx, &rpc::payload::MailboxReadResponse::content, [](auto s) { Mailbox._onIncomingData(s); });
#else
  (void)ctx;
  emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
#endif
}

void BridgeClass::_handleMailboxAvailableResp(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_MAILBOX
  _withPayload<rpc::payload::MailboxAvailableResponse>(ctx, [](const auto& msg) { Mailbox._onAvailableResponse(msg); });
#else
  (void)ctx;
  emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
#endif
}

void BridgeClass::_handleFileWrite(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_FILESYSTEM
  rpc::payload::FileWrite msg = {};
  etl::span<uint8_t> data_span(_transient_buffer.data(), _transient_buffer.size());
  rpc::util::pb_setup_decode_span(msg.data, data_span);
  _withPayload<rpc::payload::FileWrite>(ctx, [&data_span](const rpc::payload::FileWrite& parsed_msg) {
    FileSystem._onWrite(parsed_msg, etl::span<const uint8_t>(data_span.data(), data_span.size()));
  }, msg);
#else
  (void)ctx;
  emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
#endif
}

void BridgeClass::_handleFileRead(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_FILESYSTEM
  _withPayload<rpc::payload::FileRead>(ctx, [](const rpc::payload::FileRead& msg) { FileSystem._onRead(msg); });
#else
  (void)ctx;
  emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
#endif
}

void BridgeClass::_handleFileRemove(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_FILESYSTEM
  _withPayload<rpc::payload::FileRemove>(ctx, [](const rpc::payload::FileRemove& msg) { FileSystem._onRemove(msg); });
#else
  (void)ctx;
  emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
#endif
}

void BridgeClass::_handleFileReadResp(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_FILESYSTEM
  _dispatchWithBytes<rpc::payload::FileReadResponse>(ctx, &rpc::payload::FileReadResponse::content, [](const auto& s) { FileSystem._onResponse(s); });
#else
  (void)ctx;
  emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
#endif
}

void BridgeClass::_handleProcessKill(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_PROCESS
  _withPayloadAck<rpc::payload::ProcessKill>(ctx, [this](const rpc::payload::ProcessKill& msg) {
    if (!Process._kill(msg.pid)) emitStatus(rpc::StatusCode::STATUS_ERROR);
  });
#else
  (void)ctx;
  emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
#endif
}

void BridgeClass::_handleProcessRunAsyncResp(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_PROCESS
  _withPayload<rpc::payload::ProcessRunAsyncResponse>(ctx, [](const auto& msg) { Process._onRunAsyncResponse(msg); });
#else
  (void)ctx;
  emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
#endif
}

void BridgeClass::_handleProcessPollResp(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_PROCESS
  static constexpr size_t HALF_BUF = rpc::MAX_PAYLOAD_SIZE / 2;
  uint8_t* stdout_ptr = _transient_buffer.data();
  uint8_t* stderr_ptr = _transient_buffer.data() + HALF_BUF;
  etl::span<uint8_t> stdout_span(stdout_ptr, HALF_BUF);
  etl::span<uint8_t> stderr_span(stderr_ptr, HALF_BUF);
  rpc::payload::ProcessPollResponse msg = {};
  rpc::util::pb_setup_decode_span(msg.stdout_data, stdout_span);
  rpc::util::pb_setup_decode_span(msg.stderr_data, stderr_span);
  _withPayload<rpc::payload::ProcessPollResponse>(ctx, [&](const auto& inner_msg) {
    Process._onPollResponse(inner_msg, etl::span<const uint8_t>(stdout_span), etl::span<const uint8_t>(stderr_span));
  }, msg);
#else
  (void)ctx;
  emitStatus(rpc::StatusCode::STATUS_NOT_IMPLEMENTED);
#endif
}

void BridgeClass::onUnknownCommand(const bridge::router::CommandContext& ctx) {
  if (_command_handler.is_valid()) _command_handler(*ctx.frame);
  else _sendError(rpc::StatusCode::STATUS_CMD_UNKNOWN, ctx.raw_command, ctx.sequence_id);
}

void BridgeClass::_sendError(rpc::StatusCode status, uint16_t command_id, uint16_t sequence_id) {
  rpc::payload::AckPacket msg = {};
  msg.command_id = command_id;
  _sendPbResponse(status, sequence_id, msg);
}

void BridgeClass::_handleStatusAck(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::AckPacket>(ctx, [this](const rpc::payload::AckPacket& msg) { _handleAck(static_cast<uint16_t>(msg.command_id)); });
}

void BridgeClass::_handleStatusMalformed(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::AckPacket>(ctx, [this](const rpc::payload::AckPacket& msg) { _handleMalformed(static_cast<uint16_t>(msg.command_id)); });
}

void BridgeClass::_unusedCommandSlot(const bridge::router::CommandContext& ctx) { onUnknownCommand(ctx); }

void BridgeClass::_dispatchJumpTable(const bridge::router::CommandContext& ctx, uint16_t min_id, const CmdHandler* handlers, uint8_t count, uint8_t stride) {
  if (ctx.raw_command < min_id) return;
  const uint8_t index = static_cast<uint8_t>((ctx.raw_command - min_id) / stride);
  if (index < count && handlers[index]) (this->*handlers[index])(ctx);
}

void BridgeClass::_handleAck(uint16_t command_id) {
  bool awaiting = false; BRIDGE_ATOMIC_BLOCK { awaiting = _fsm.isAwaitingAck(); }
  if (awaiting && (command_id == _last_command_id)) {
    _clearAckState(); _timers.stop(bridge::scheduler::TIMER_ACK_TIMEOUT);
    BRIDGE_ATOMIC_BLOCK { if (!_pending_tx_queue.empty()) _pending_tx_queue.pop(); }
    _flushPendingTxQueue();
  }
}

void BridgeClass::_handleMalformed(uint16_t command_id) { if (command_id == _last_command_id) _retransmitLastFrame(); }

void BridgeClass::_retransmitLastFrame() {
  PendingTxFrame f; bool has_frame = false;
  BRIDGE_ATOMIC_BLOCK { if (!_pending_tx_queue.empty()) { f = _pending_tx_queue.front(); has_frame = true; } }
  if (has_frame) { _sendRawFrame(f.command_id, 0, etl::span<const uint8_t>(_tx_payload_pool.data() + f.buffer_offset, f.payload_length)); _retry_count++; }
}

void BridgeClass::_onAckTimeout() {
  bool awaiting = false; BRIDGE_ATOMIC_BLOCK { awaiting = _fsm.isAwaitingAck(); }
  if (!awaiting) return;
  if (_retry_count >= _ack_retry_limit) { BRIDGE_ATOMIC_BLOCK { _fsm.timeout(); } enterSafeState(); return; }
  _retransmitLastFrame(); _timers.start(bridge::scheduler::TIMER_ACK_TIMEOUT, bridge::now_ms());
}

void BridgeClass::_onRxDedupe() { _rx_history.clear(); }

void BridgeClass::_onBaudrateChange() { if (_pending_baudrate > 0) { if (_hardware_serial) _hardware_serial->begin(_pending_baudrate); _pending_baudrate = 0; } }

void BridgeClass::_onStartupStabilized() {
  uint16_t drain_limit = bridge::config::STARTUP_DRAIN_FINAL;
  while (_stream.available() > 0 && drain_limit-- > 0) _stream.read();
  BRIDGE_ATOMIC_BLOCK { _fsm.stabilized(); }
}

void BridgeClass::enterSafeState() {
  BRIDGE_ATOMIC_BLOCK { _fsm.resetFsm(); }
  _timers.clear(); _pending_baudrate = 0; _retry_count = 0; _clearPendingTxQueue();
  _flags.reset(bridge::FlagId::FRAME_RECEIVED); _rx_history.clear(); _consecutive_crc_errors = 0;
#if BRIDGE_ENABLE_PROCESS
  Process.reset();
#endif
  notify_observers(MsgBridgeLost());
}

void BridgeClass::emitStatus(rpc::StatusCode status_code, etl::span<const uint8_t> payload) {
  sendFrame(status_code, 0, payload);
  if (_status_handler.is_valid()) _status_handler(status_code, payload);
  notify_observers(MsgBridgeError{status_code});
}

void BridgeClass::emitStatus(rpc::StatusCode status_code, etl::string_view message) { emitStatus(status_code, etl::span<const uint8_t>(reinterpret_cast<const uint8_t*>(message.data()), message.length())); }

void BridgeClass::emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message) {
  if (message == nullptr) { emitStatus(status_code, etl::span<const uint8_t>()); return; }
#if defined(ARDUINO_ARCH_AVR)
  strncpy_P(reinterpret_cast<char*>(_transient_buffer.data()), (PGM_P)message, _transient_buffer.size() - 1);
#else
  strncpy(reinterpret_cast<char*>(_transient_buffer.data()), reinterpret_cast<const char*>(message), _transient_buffer.size() - 1);
#endif
  _transient_buffer[_transient_buffer.size() - 1] = '\0';
  emitStatus(status_code, etl::span<const uint8_t>(_transient_buffer.data(), strlen(reinterpret_cast<char*>(_transient_buffer.data()))));
}

bool BridgeClass::sendFrame(rpc::StatusCode status_code, uint16_t sequence_id, etl::span<const uint8_t> payload) { return _sendFrame(rpc::to_underlying(status_code), sequence_id, payload); }
bool BridgeClass::sendFrame(rpc::CommandId command_id, uint16_t sequence_id, etl::span<const uint8_t> payload) { return _sendFrame(rpc::to_underlying(command_id), sequence_id, payload); }

void BridgeClass::_sendRawFrame(uint16_t command_id, uint16_t sequence_id, etl::span<const uint8_t> payload) {
  etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE> raw_buffer;
  size_t raw_len = _frame_builder.build(etl::span<uint8_t>(raw_buffer.data(), raw_buffer.size()), command_id, sequence_id, payload);
  if (raw_len > 0) {
    size_t enc_len = rpc::cobs::encode(etl::span<const uint8_t>(raw_buffer.data(), raw_len), etl::span<uint8_t>(_transient_buffer.data(), _transient_buffer.size()));
    if (enc_len > 0) { _stream.write(_transient_buffer.data(), enc_len); _stream.write(rpc::RPC_FRAME_DELIMITER); flushStream(); }
  }
}

void BridgeClass::_flushPendingTxQueue() {
  PendingTxFrame f; bool has_frame = false;
  BRIDGE_ATOMIC_BLOCK { if (!_fsm.isAwaitingAck() && !_pending_tx_queue.empty()) { f = _pending_tx_queue.front(); has_frame = true; } }
  if (has_frame) {
    uint16_t seq = ++_tx_sequence_id;
    _sendRawFrame(f.command_id, seq, etl::span<const uint8_t>(_tx_payload_pool.data() + f.buffer_offset, f.payload_length));
    BRIDGE_ATOMIC_BLOCK { _fsm.sendCritical(); } _retry_count = 0;
    _timers.start(bridge::scheduler::TIMER_ACK_TIMEOUT, bridge::now_ms()); _last_command_id = f.command_id;
  }
}

void BridgeClass::_clearPendingTxQueue() { BRIDGE_ATOMIC_BLOCK { while (!_pending_tx_queue.empty()) _pending_tx_queue.pop(); _tx_pool_head = 0; } }
void BridgeClass::_clearAckState() {
  BRIDGE_ATOMIC_BLOCK { if (_fsm.isAwaitingAck()) { _fsm.ackReceived(); if (!_pending_tx_queue.empty()) { _pending_tx_queue.pop(); if (_pending_tx_queue.empty()) _tx_pool_head = 0; } } }
  _retry_count = 0;
}

void BridgeClass::_sendAckAndFlush(uint16_t command_id, uint16_t sequence_id) {
  rpc::payload::AckPacket msg = {}; msg.command_id = command_id;
  _sendPbResponse(rpc::StatusCode::STATUS_ACK, sequence_id, msg); flushStream();
}

bool BridgeClass::_sendFrame(uint16_t command_id, uint16_t sequence_id, etl::span<const uint8_t> payload) {
  bool fault, unsync; BRIDGE_ATOMIC_BLOCK { fault = _fsm.isFault(); unsync = _fsm.isUnsynchronized(); }
  if (fault) return false;
  if (unsync && !_isHandshakeCommand(command_id)) return false;
  if (rpc::requires_ack(command_id)) {
    if (_isQueueFull() || (_tx_pool_head + payload.size() > _tx_payload_pool.size())) return false;
    PendingTxFrame f; f.command_id = command_id; f.payload_length = static_cast<uint16_t>(payload.size()); f.buffer_offset = _tx_pool_head;
    etl::copy_n(payload.data(), f.payload_length, _tx_payload_pool.data() + _tx_pool_head); _tx_pool_head += f.payload_length;
    BRIDGE_ATOMIC_BLOCK { _pending_tx_queue.push(f); } _flushPendingTxQueue(); return true;
  }
  _sendRawFrame(command_id, sequence_id, payload); return true;
}

bool BridgeClass::_isHandshakeCommand(uint16_t cmd) const { return (cmd >= rpc::RPC_STATUS_CODE_MIN && cmd <= rpc::RPC_STATUS_CODE_MAX) || (cmd >= rpc::RPC_SYSTEM_COMMAND_MIN && cmd <= rpc::RPC_SYSTEM_COMMAND_MAX); }
bool BridgeClass::_isRecentDuplicateRx(const rpc::Frame& frame) const { return _rx_history.contains(frame.header.sequence_id); }
void BridgeClass::_markRxProcessed(const rpc::Frame& frame) { _rx_history.push(frame.header.sequence_id); }

etl::expected<void, rpc::FrameError> BridgeClass::_decompressFrame(const rpc::Frame& org, rpc::Frame& eff) {
  eff.header = org.header; eff.crc = org.crc;
  if (!bitRead(org.header.command_id, kCompressedCommandBit)) { eff.payload = org.payload; return {}; }
  bitWrite(eff.header.command_id, kCompressedCommandBit, 0);
  static etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> decompression_buffer;
  size_t decoded_len = rle::decode(org.payload, etl::span<uint8_t>(decompression_buffer.data(), decompression_buffer.size()));
  if (decoded_len == 0 && org.header.payload_length > 0) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);
  eff.header.payload_length = static_cast<uint16_t>(decoded_len); eff.payload = etl::span<const uint8_t>(decompression_buffer.data(), decoded_len);
  return {};
}

void BridgeClass::_computeHandshakeTag(etl::span<const uint8_t> nonce, etl::span<uint8_t> out_tag) {
  etl::array<uint8_t, bridge::config::HKDF_KEY_LENGTH> handshake_key;
  rpc::security::hkdf_sha256(etl::span<uint8_t>(handshake_key.data(), handshake_key.size()), etl::span<const uint8_t>(_shared_secret.data(), _shared_secret.size()), etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_SALT, rpc::RPC_HANDSHAKE_HKDF_SALT_LEN), etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH, rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH_LEN));
  rpc::security::McuBridgeSha256 sha256; sha256.resetHMAC(handshake_key.data(), handshake_key.size()); sha256.update(nonce.data(), nonce.size());
  etl::array<uint8_t, rpc::security::McuBridgeSha256::HASH_SIZE> full_tag;
  sha256.finalizeHMAC(handshake_key.data(), handshake_key.size(), full_tag.data(), full_tag.size());
  etl::copy_n(full_tag.begin(), etl::min(full_tag.size(), out_tag.size()), out_tag.begin());
  rpc::security::secure_zero(etl::span<uint8_t>(handshake_key.data(), handshake_key.size())); rpc::security::secure_zero(etl::span<uint8_t>(full_tag.data(), full_tag.size()));
}

void BridgeClass::_applyTimingConfig(etl::span<const uint8_t> payload) {
  rpc::payload::HandshakeConfig msg = {}; pb_istream_t stream = pb_istream_from_buffer(payload.data(), payload.size());
  if (pb_decode(&stream, rpc::Payload::Descriptor<rpc::payload::HandshakeConfig>::fields(), &msg)) {
    if (msg.ack_timeout_ms > 0) { _ack_timeout_ms = msg.ack_timeout_ms; _timers.set_period(bridge::scheduler::TIMER_ACK_TIMEOUT, _ack_timeout_ms); }
    if (msg.ack_retry_limit > 0) _ack_retry_limit = msg.ack_retry_limit;
    if (msg.response_timeout_ms > 0) _response_timeout_ms = msg.response_timeout_ms;
  }
}

#ifndef BRIDGE_TEST_NO_GLOBALS
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
#if BRIDGE_ENABLE_SPI
SPIServiceClass SPIService;
#endif
#endif

namespace etl { void __attribute__((weak)) handle_error(const etl::exception& e) { (void)e; Bridge.enterSafeState(); } }

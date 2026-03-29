#include "Bridge.h"
#include "hal/progmem_compat.h"
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
#include "hal/logging.h"
#include "protocol/rle.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "security/security.h"

namespace {
constexpr uint8_t kCompressedCommandBit = rpc::RPC_CMD_FLAG_COMPRESSED_BIT;
}

BridgeClass::BridgeClass(HardwareSerial& arg_serial)
    : BridgeClass(static_cast<Stream&>(arg_serial)) {
  _hardware_serial = &arg_serial;
}

BridgeClass::BridgeClass(Stream& arg_stream)
    : _stream(arg_stream),
      _hardware_serial(nullptr),
      _shared_secret(),
      _frame_builder(),
      _last_parse_error(rpc::FrameError::NONE),
      _flags(),
      _rx_frame{},
      _rng(bridge::now_ms()),
      _last_command_id(0),
      _tx_sequence_id(0),
      _retry_count(0),
      _pending_baudrate(0),
      _rx_storage(),
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
      _transient_buffer(),
      _pending_tx_queue(),
      _tx_payload_pool(),
      _tx_pool_head(0),
      _fsm(),
      _timers(),
      _last_tick_millis(0),
      _packet_serial(etl::span<uint8_t>(_rx_storage.data(), _rx_storage.size()),
                     etl::span<uint8_t>(_transient_buffer.data(), _transient_buffer.size())) {
  _flags.reset();
  _timers.clear();
}

void BridgeClass::begin(unsigned long arg_baudrate, etl::string_view arg_secret,
                        size_t arg_secret_len) {
  // [SIL-2] Initialize Hardware (Watchdog, Safe Pin States) via HAL
  bridge::hal::init();

  _fsm.begin();
  _timers.clear();
  _rx_history.clear();
  
  // Set deterministic periods based on protocol spec
  _timers.set_period(bridge::scheduler::TIMER_ACK_TIMEOUT, _ack_timeout_ms);
  _timers.set_period(bridge::scheduler::TIMER_RX_DEDUPE, bridge::config::RX_DEDUPE_INTERVAL_MS);
  _timers.set_period(bridge::scheduler::TIMER_BAUDRATE_CHANGE, bridge::config::BAUDRATE_SETTLE_MS);
  _timers.set_period(bridge::scheduler::TIMER_STARTUP_STABILIZATION, bridge::config::STARTUP_STABILIZATION_MS);
  _last_tick_millis = bridge::now_ms();

  // [MIL-SPEC] Cryptographic Power-On Self-Test (POST)
  if (!rpc::security::run_cryptographic_self_tests()) {
    enterSafeState();
    _fsm.cryptoFault();
    return;
  }

  if (_hardware_serial != nullptr) {
    _hardware_serial->begin(arg_baudrate);
#if !defined(BRIDGE_HOST_TEST)
    _hardware_serial->setTimeout(bridge::config::SERIAL_TIMEOUT_MS);
#endif
  }

  _packet_serial.setPacketHandler(etl::delegate<void(etl::span<const uint8_t>)>::create<BridgeClass, &BridgeClass::_onPacketReceived>(*this));

  _timers.start(bridge::scheduler::TIMER_STARTUP_STABILIZATION, bridge::now_ms());

  _shared_secret.clear();
  if (!arg_secret.empty()) {
    size_t actual_len = (arg_secret_len > 0) ? arg_secret_len : arg_secret.length();
    if (actual_len > _shared_secret.capacity()) actual_len = _shared_secret.capacity();
    const uint8_t* start = reinterpret_cast<const uint8_t*>(arg_secret.data());
    etl::copy(start, start + actual_len, etl::back_inserter(_shared_secret));
  }

  _fsm.resetFsm();
  _last_command_id = 0;
  _tx_sequence_id = 0;
  _retry_count = 0;
  _rx_history.clear();

  // Feature registration using zero-cost abstractions
  add_observer(Console);
#if BRIDGE_ENABLE_DATASTORE
  add_observer(DataStore);
#endif
#if BRIDGE_ENABLE_MAILBOX
  add_observer(Mailbox);
#endif
#if BRIDGE_ENABLE_PROCESS
  add_observer(Process);
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  add_observer(FileSystem);
#endif
}

void BridgeClass::process() {
  if constexpr (bridge::config::ENABLE_WATCHDOG) {
    #if defined(ARDUINO_ARCH_AVR)
      wdt_reset();
    #elif defined(ARDUINO_ARCH_ESP32)
      esp_task_wdt_reset();
    #elif defined(ARDUINO_ARCH_ESP8266)
      yield();
    #endif
  }

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
    _packet_serial.update(_stream);
  }

  if (_last_parse_error != rpc::FrameError::NONE) {
    const rpc::FrameError error = _last_parse_error;
    _last_parse_error = rpc::FrameError::NONE;
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

void BridgeClass::_onPacketReceived(etl::span<const uint8_t> packet) {
  _handleReceivedFrame(packet);
}

void BridgeClass::_handleReceivedFrame(etl::span<const uint8_t> decoded_payload) {
  if (decoded_payload.empty()) return;

  rpc::FrameParser parser;
  const auto result = parser.parse(decoded_payload);
  if (result.has_value()) {
      rpc::Frame raw_frame = result.value();
      rpc::Frame effective_frame;

      auto decomp_res = _decompressFrame(raw_frame, effective_frame);
      if (!decomp_res) {
          emitStatus(rpc::StatusCode::STATUS_MALFORMED);
          return;
      }

      _rx_frame = effective_frame;
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
}

void BridgeClass::forceSafeState() {
  bridge::hal::forceSafeState();
#if defined(ARDUINO_ARCH_AVR)
  wdt_enable(WDTO_2S);
#endif
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
    (void)sendFrame(rpc::StatusCode::STATUS_ERROR, sequence_id);
    return;
  }

  bridge::router::CommandContext ctx(&effective_frame, raw_cmd,
                                     false,
                                     rpc::requires_ack(raw_cmd),
                                     sequence_id);

  notify_observers(MsgBridgeCommand{raw_cmd, sequence_id, effective_frame.payload});

  // [SIL-2] O(1) Jump Table for Command Categories
  static const CmdHandler kGroupHandlers[] PROGMEM = {
      &BridgeClass::onStatusCommand,    // 0x30
      &BridgeClass::onSystemCommand,    // 0x40
      &BridgeClass::onGpioCommand,      // 0x50
      &BridgeClass::onConsoleCommand,   // 0x60
      &BridgeClass::onDataStoreCommand, // 0x70
      &BridgeClass::onMailboxCommand,   // 0x80
      &BridgeClass::onFileSystemCommand,// 0x90
      &BridgeClass::onProcessCommand,   // 0xA0
      &BridgeClass::onSpiCommand        // 0xB0
  };

  const uint8_t group_idx = (raw_cmd >> rpc::RPC_COMMAND_GROUP_SHIFT) - rpc::RPC_COMMAND_GROUP_OFFSET;
  if (group_idx < ETL_ARRAY_SIZE(kGroupHandlers)) {
    CmdHandler handler;
    bridge::hal::copy_from_progmem(&handler, &kGroupHandlers[group_idx]);
    (this->*handler)(ctx);
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
  static const CmdHandler kStatusHandlers[] PROGMEM = {
      &BridgeClass::_unusedCommandSlot,     // 48: STATUS_OK
      &BridgeClass::_unusedCommandSlot,     // 49: STATUS_ERROR
      &BridgeClass::_unusedCommandSlot,     // 50: STATUS_CMD_UNKNOWN
      &BridgeClass::_handleStatusMalformed, // 51: MALFORMED
      &BridgeClass::_unusedCommandSlot,     // 52
      &BridgeClass::_unusedCommandSlot,     // 53
      &BridgeClass::_unusedCommandSlot,     // 54
      &BridgeClass::_unusedCommandSlot,     // 55
      &BridgeClass::_handleStatusAck        // 56: ACK
  };
  _dispatchJumpTable(ctx, rpc::RPC_STATUS_CODE_MIN, kStatusHandlers, ETL_ARRAY_SIZE(kStatusHandlers));

  if (_status_handler.is_valid()) {
    _status_handler(static_cast<rpc::StatusCode>(ctx.raw_command), ctx.frame->payload);
  }
}

void BridgeClass::onSystemCommand(const bridge::router::CommandContext& ctx) {
  static const CmdHandler kSystemHandlers[] PROGMEM = {
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
  };
  _dispatchJumpTable(ctx, rpc::RPC_SYSTEM_COMMAND_MIN, kSystemHandlers, ETL_ARRAY_SIZE(kSystemHandlers));
}

void BridgeClass::onGpioCommand(const bridge::router::CommandContext& ctx) {
  static const CmdHandler kGpioHandlers[] PROGMEM = {
      &BridgeClass::_handleSetPinMode, &BridgeClass::_handleDigitalWrite,
      &BridgeClass::_handleAnalogWrite, &BridgeClass::_handleDigitalRead,
      &BridgeClass::_handleAnalogRead
  };
  _dispatchJumpTable(ctx, rpc::RPC_GPIO_COMMAND_MIN, kGpioHandlers, ETL_ARRAY_SIZE(kGpioHandlers));
}

void BridgeClass::onConsoleCommand(const bridge::router::CommandContext& ctx) {
  static const CmdHandler kConsoleHandlers[] PROGMEM = {
      &BridgeClass::_handleConsoleWrite
  };
  _dispatchJumpTable(ctx, rpc::RPC_CONSOLE_COMMAND_MIN, kConsoleHandlers, ETL_ARRAY_SIZE(kConsoleHandlers));
}

void BridgeClass::onDataStoreCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_DATASTORE) {
    static const CmdHandler kDataStoreHandlers[] PROGMEM = {
        &BridgeClass::_unusedCommandSlot, // 112
        &BridgeClass::_unusedCommandSlot, // 113
        &BridgeClass::_handleDatastoreGetResp // 114
    };
    _dispatchJumpTable(ctx, rpc::RPC_DATASTORE_COMMAND_MIN, kDataStoreHandlers, ETL_ARRAY_SIZE(kDataStoreHandlers));
  } else {
    onUnknownCommand(ctx);
  }
}

void BridgeClass::onMailboxCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_MAILBOX) {
    static const CmdHandler kMailboxHandlers[] PROGMEM = {
        &BridgeClass::_unusedCommandSlot, // 128
        &BridgeClass::_unusedCommandSlot, // 129
        &BridgeClass::_unusedCommandSlot, // 130
        &BridgeClass::_handleMailboxPush, // 131
        &BridgeClass::_handleMailboxReadResp, // 132
        &BridgeClass::_handleMailboxAvailableResp // 133
    };
    _dispatchJumpTable(ctx, rpc::RPC_MAILBOX_COMMAND_MIN, kMailboxHandlers, ETL_ARRAY_SIZE(kMailboxHandlers));
  } else {
    onUnknownCommand(ctx);
  }
}

void BridgeClass::onFileSystemCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_FILESYSTEM) {
    static const CmdHandler kFsHandlers[] PROGMEM = {
        &BridgeClass::_handleFileWrite, // 144
        &BridgeClass::_handleFileRead, // 145
        &BridgeClass::_handleFileRemove, // 146
        &BridgeClass::_handleFileReadResp // 147
    };
    _dispatchJumpTable(ctx, rpc::RPC_FILESYSTEM_COMMAND_MIN, kFsHandlers, ETL_ARRAY_SIZE(kFsHandlers));
  } else {
    onUnknownCommand(ctx);
  }
}

void BridgeClass::onProcessCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_PROCESS) {
    static const CmdHandler kProcessHandlers[] PROGMEM = {
        &BridgeClass::_unusedCommandSlot, // 160
        &BridgeClass::_unusedCommandSlot, // 161
        &BridgeClass::_handleProcessKill, // 162
        &BridgeClass::_unusedCommandSlot, // 163
        &BridgeClass::_unusedCommandSlot, // 164
        &BridgeClass::_handleProcessRunAsyncResp, // 165
        &BridgeClass::_handleProcessPollResp // 166
    };
    _dispatchJumpTable(ctx, rpc::RPC_PROCESS_COMMAND_MIN, kProcessHandlers, ETL_ARRAY_SIZE(kProcessHandlers));
  } else {
    onUnknownCommand(ctx);
  }
}

void BridgeClass::onSpiCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_SPI) {
    static const CmdHandler kSpiHandlers[] PROGMEM = {
        &BridgeClass::_handleSpiBegin,
        &BridgeClass::_handleSpiTransfer,
        &BridgeClass::_unusedCommandSlot,
        &BridgeClass::_handleSpiEnd,
        &BridgeClass::_handleSpiSetConfig,
    };
    _dispatchJumpTable(ctx, rpc::RPC_SPI_COMMAND_MIN, kSpiHandlers, ETL_ARRAY_SIZE(kSpiHandlers));
  } else {
    onUnknownCommand(ctx);
  }
}

void BridgeClass::_handleSpiBegin(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_SPI
    _withAck(ctx, []() { SPIService.begin(); });
#endif
}

void BridgeClass::_handleSpiEnd(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_SPI
    _withAck(ctx, []() { SPIService.end(); });
#endif
}

void BridgeClass::_handleSpiSetConfig(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_SPI
    _withPayloadAck<rpc::payload::SpiConfig>(ctx, [](const rpc::payload::SpiConfig& msg) {
      uint8_t bitOrder = (msg.bit_order == 0) ? 0 : 1;
      uint8_t dataMode = static_cast<uint8_t>(msg.data_mode);
      SPIService.setConfig(msg.frequency, bitOrder, dataMode);
    });
#endif
}

void BridgeClass::_handleSpiTransfer(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_SPI
    if (ctx.is_duplicate) return;
    rpc::payload::SpiTransfer req = {};
    static etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buffer;
    etl::span<uint8_t> decode_span(buffer.data(), buffer.size());
    rpc::util::pb_setup_decode_span(req.data, decode_span);
    auto res = rpc::Payload::parse<rpc::payload::SpiTransfer>(*ctx.frame, req);
    if (res.has_value()) {
      if (SPIService.isInitialized()) {
        size_t len = decode_span.size();
        if (len > 0) {
          size_t xferred = SPIService.transfer(buffer.data(), len);
          if (xferred < len) {
            enterSafeState();
            _sendError(rpc::StatusCode::STATUS_ERROR, ctx.raw_command, ctx.sequence_id);
            return;
          }
        }
        rpc::payload::SpiTransferResponse resp = {};
        etl::span<const uint8_t> out_span(buffer.data(), len);
        rpc::util::pb_setup_encode_span(resp.data, out_span);
        _sendPbResponse(rpc::CommandId::CMD_SPI_TRANSFER_RESP, ctx.sequence_id, resp);
      }
    }
#endif
}

void BridgeClass::_handleEnterBootloader(const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::EnterBootloader>(ctx, [this](const rpc::payload::EnterBootloader& msg) {
    if (msg.magic == rpc::RPC_BOOTLOADER_MAGIC) {
      this->flushStream();
      delay(bridge::config::BOOTLOADER_DELAY_MS);
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
    resp.major = rpc::FIRMWARE_VERSION_MAJOR;
    resp.minor = rpc::FIRMWARE_VERSION_MINOR;
    resp.patch = rpc::FIRMWARE_VERSION_PATCH;
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
  _withPayload<rpc::payload::HandshakeConfig>(ctx, [this, &ctx](const rpc::payload::HandshakeConfig& msg) {
    _applyTimingConfig(msg);
    enterSafeState();
    (void)sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP, ctx.sequence_id);
  });
}

void BridgeClass::_applyTimingConfig(const rpc::payload::HandshakeConfig& msg) {
  if (msg.ack_timeout_ms > 0) {
    _ack_timeout_ms = msg.ack_timeout_ms;
    _timers.set_period(bridge::scheduler::TIMER_ACK_TIMEOUT, _ack_timeout_ms);
  }
  if (msg.ack_retry_limit > 0) _ack_retry_limit = msg.ack_retry_limit;
  if (msg.response_timeout_ms > 0) _response_timeout_ms = msg.response_timeout_ms;
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
#endif
}

void BridgeClass::_handleMailboxPush(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_MAILBOX
  _dispatchWithBytes<rpc::payload::MailboxPush>(ctx, &rpc::payload::MailboxPush::data, [](auto s) { Mailbox._onIncomingData(s); }, true);
#endif
}

void BridgeClass::_handleMailboxReadResp(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_MAILBOX
  _dispatchWithBytes<rpc::payload::MailboxReadResponse>(ctx, &rpc::payload::MailboxReadResponse::content, [](auto s) { Mailbox._onIncomingData(s); });
#endif
}

void BridgeClass::_handleMailboxAvailableResp(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_MAILBOX
  _withPayload<rpc::payload::MailboxAvailableResponse>(ctx, [](const auto& msg) { Mailbox._onAvailableResponse(msg); });
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
#endif
}

void BridgeClass::_handleFileRead(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_FILESYSTEM
  _withPayload<rpc::payload::FileRead>(ctx, [](const rpc::payload::FileRead& msg) { FileSystem._onRead(msg); });
#endif
}

void BridgeClass::_handleFileRemove(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_FILESYSTEM
  _withPayload<rpc::payload::FileRemove>(ctx, [](const rpc::payload::FileRemove& msg) { FileSystem._onRemove(msg); });
#endif
}

void BridgeClass::_handleFileReadResp(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_FILESYSTEM
  _dispatchWithBytes<rpc::payload::FileReadResponse>(ctx, &rpc::payload::FileReadResponse::content, [](const auto& s) { FileSystem._onResponse(s); });
#endif
}

void BridgeClass::_handleProcessKill(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_PROCESS
  _withPayloadAck<rpc::payload::ProcessKill>(ctx, [this](const rpc::payload::ProcessKill& msg) {
    if (!Process._kill(msg.pid)) emitStatus(rpc::StatusCode::STATUS_ERROR);
  });
#endif
}

void BridgeClass::_handleProcessRunAsyncResp(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_PROCESS
  _withPayload<rpc::payload::ProcessRunAsyncResponse>(ctx, [](const auto& msg) { Process._onRunAsyncResponse(msg); });
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
  if (index < count) {
    CmdHandler handler;
    bridge::hal::copy_from_progmem(&handler, &handlers[index]);
    if (handler) (this->*handler)(ctx);
  }
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
  _rx_history.clear(); _consecutive_crc_errors = 0;
  
  // [MIL-SPEC] Securely zero sensitive data on fault (HKDF/Shared Secret)
  rpc::security::secure_zero(etl::span<uint8_t>(_shared_secret.data(), _shared_secret.size()));
  _shared_secret.clear();

#if BRIDGE_ENABLE_PROCESS
    Process.reset();
#endif
  // [SIL-2] Force physical pins to high-impedance safe state
  forceSafeState(); 
  notify_observers(MsgBridgeLost());
}

void BridgeClass::emitStatus(rpc::StatusCode status_code, etl::span<const uint8_t> payload) {
  (void)sendFrame(status_code, 0, payload);
  if (_status_handler.is_valid()) _status_handler(status_code, payload);
  notify_observers(MsgBridgeError{status_code});
}

void BridgeClass::emitStatus(rpc::StatusCode status_code, etl::string_view message) {
  if (message.empty()) {
    emitStatus(status_code, etl::span<const uint8_t>());
    return;
  }
  const size_t max_len = etl::min(message.length(), rpc::MAX_PAYLOAD_SIZE - 1U);
  etl::copy_n(message.data(), max_len, _transient_buffer.data());
  _transient_buffer[max_len] = rpc::RPC_NULL_TERMINATOR;
  emitStatus(status_code, etl::span<const uint8_t>(_transient_buffer.data(), max_len));
}

void BridgeClass::emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message) {
  if (message == nullptr) { emitStatus(status_code, etl::span<const uint8_t>()); return; }
  constexpr size_t max_len = rpc::MAX_PAYLOAD_SIZE - 1U;
  bridge::hal::copy_string(reinterpret_cast<char*>(_transient_buffer.data()), reinterpret_cast<const char*>(message), max_len);
  _transient_buffer[max_len] = rpc::RPC_NULL_TERMINATOR;
  
  const size_t actual_len = etl::string_view(reinterpret_cast<const char*>(_transient_buffer.data())).length();
  emitStatus(status_code, etl::span<const uint8_t>(_transient_buffer.data(), actual_len));
}

bool BridgeClass::sendFrame(rpc::StatusCode status_code, uint16_t sequence_id, etl::span<const uint8_t> payload) { return _sendFrame(rpc::to_underlying(status_code), sequence_id, payload); }
bool BridgeClass::sendFrame(rpc::CommandId command_id, uint16_t sequence_id, etl::span<const uint8_t> payload) { return _sendFrame(rpc::to_underlying(command_id), sequence_id, payload); }

void BridgeClass::_sendRawFrame(uint16_t command_id, uint16_t sequence_id, etl::span<const uint8_t> payload) {
  etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE> raw_buffer;
  size_t raw_len = _frame_builder.build(etl::span<uint8_t>(raw_buffer.data(), raw_buffer.size()), command_id, sequence_id, payload);
  if (raw_len > 0) {
    auto res = _packet_serial.send(_stream, etl::span<const uint8_t>(raw_buffer.data(), raw_len));
    if (!res.has_value()) {
        _last_parse_error = rpc::FrameError::OVERFLOW;
    }
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

void BridgeClass::_clearPendingTxQueue() { BRIDGE_ATOMIC_BLOCK { _pending_tx_queue.clear(); _tx_pool_head = 0; } }
void BridgeClass::_clearAckState() {
  BRIDGE_ATOMIC_BLOCK { if (_fsm.isAwaitingAck()) { _fsm.ackReceived(); if (!_pending_tx_queue.empty()) { _pending_tx_queue.pop(); if (_pending_tx_queue.empty()) _tx_pool_head = 0; } } }
  _retry_count = 0;
}

void BridgeClass::_sendAckAndFlush(uint16_t command_id, uint16_t sequence_id) {
  rpc::payload::AckPacket msg = {}; msg.command_id = command_id;
  _sendPbResponse(rpc::StatusCode::STATUS_ACK, sequence_id, msg); flushStream();
}

bool BridgeClass::_sendFrame(uint16_t command_id, uint16_t sequence_id, etl::span<const uint8_t> payload) {
  bool fault, operational; BRIDGE_ATOMIC_BLOCK { fault = _fsm.isFault(); operational = _fsm.isSynchronized(); }
  if (fault) return false;
  if (!operational && !_isHandshakeCommand(command_id)) return false;
  if (rpc::requires_ack(command_id)) {
    if (_isQueueFull() || (_tx_pool_head + payload.size() > _tx_payload_pool.size())) return false;
    PendingTxFrame f; f.command_id = command_id; f.payload_length = static_cast<uint16_t>(payload.size()); f.buffer_offset = _tx_pool_head;
    etl::copy_n(payload.data(), f.payload_length, _tx_payload_pool.data() + _tx_pool_head); _tx_pool_head += f.payload_length;
    BRIDGE_ATOMIC_BLOCK { _pending_tx_queue.push(f); } _flushPendingTxQueue(); return true;
  }
  _sendRawFrame(command_id, sequence_id, payload); return true;
}

bool BridgeClass::_isHandshakeCommand(uint16_t cmd) const { return (cmd >= rpc::RPC_STATUS_CODE_MIN && cmd <= rpc::RPC_STATUS_CODE_MAX) || (cmd >= rpc::RPC_SYSTEM_COMMAND_MIN && cmd <= rpc::RPC_SYSTEM_COMMAND_MAX); }
void BridgeClass::_markRxProcessed(const rpc::Frame& frame) { _rx_history.push(frame.header.sequence_id); }

etl::expected<void, rpc::FrameError> BridgeClass::_decompressFrame(const rpc::Frame& org, rpc::Frame& eff) {
  eff.header = org.header; eff.crc = org.crc;
  if (!bitRead(org.header.command_id, kCompressedCommandBit)) { eff.payload = org.payload; return {}; }
  bitWrite(eff.header.command_id, kCompressedCommandBit, 0);
  size_t decoded_len = rle::decode(org.payload, etl::span<uint8_t>(_decompression_buffer.data(), _decompression_buffer.size()));
  if (decoded_len == 0 && org.header.payload_length > 0) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);
  eff.header.payload_length = static_cast<uint16_t>(decoded_len); eff.payload = etl::span<const uint8_t>(_decompression_buffer.data(), decoded_len);
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

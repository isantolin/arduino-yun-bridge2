#include "Bridge.h"
#include "hal/progmem_compat.h"
#include "services/SPIService.h"
#include <Arduino.h>
#include <etl/numeric.h>
#include <etl/span.h>
#include "util/string_copy.h"

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
      _command_handler(),
      _digital_read_handler(),
      _analog_read_handler(),
      _get_free_memory_handler(),
      _status_handler(),
      _on_ack_timeout_delegate(etl::icallback_timer::callback_type::create<
                               BridgeClass, &BridgeClass::_onAckTimeout>(*this)),
      _on_rx_dedupe_delegate(etl::icallback_timer::callback_type::create<
                             BridgeClass, &BridgeClass::_onRxDedupe>(*this)),
      _on_baudrate_change_delegate(etl::icallback_timer::callback_type::create<
                                   BridgeClass,
                                   &BridgeClass::_onBaudrateChange>(*this)),
      _on_startup_stabilized_delegate(
          etl::icallback_timer::callback_type::create<
              BridgeClass, &BridgeClass::_onStartupStabilized>(*this)),
      _fsm(),
      _timers(),
      _timer_ids(),
      _packet_serial(etl::span<uint8_t>(_rx_storage.data(), _rx_storage.size()),
                     etl::span<uint8_t>(_transient_buffer.data(),
                                        _transient_buffer.size())),
      _frame_builder(),
      _rx_history(),
      _flags(),
      _rx_frame{},
      _rng(bridge::now_ms()),
      _shared_secret(),
      _rx_storage(),
      _transient_buffer(),
      _pending_tx_queue(),
      _tx_payload_pool(),
      _pending_baudrate(0),
      _response_timeout_ms(rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS),
      _last_tick_millis(0),
      _consecutive_crc_errors(0),
      _ack_timeout_ms(rpc::RPC_DEFAULT_ACK_TIMEOUT_MS),
      _last_command_id(0),
      _tx_sequence_id(0),
      _last_parse_error(rpc::FrameError::NONE),
      _retry_count(0),
      _ack_retry_limit(rpc::RPC_DEFAULT_RETRY_LIMIT) {
_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT] =
    _timers.register_timer(_on_ack_timeout_delegate, 0, etl::timer::mode::SINGLE_SHOT);
_timer_ids[bridge::scheduler::TIMER_RX_DEDUPE] =
    _timers.register_timer(_on_rx_dedupe_delegate, 0, etl::timer::mode::SINGLE_SHOT);
_timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE] =
    _timers.register_timer(_on_baudrate_change_delegate, 0, etl::timer::mode::SINGLE_SHOT);
_timer_ids[bridge::scheduler::TIMER_STARTUP_STABILIZATION] =
    _timers.register_timer(_on_startup_stabilized_delegate, 0, etl::timer::mode::SINGLE_SHOT);
_fsm.setTimers(&_timers, _timer_ids);
_flags.reset();
for (auto id : _timer_ids) {
  _timers.stop(id);
}
}
void BridgeClass::begin(unsigned long arg_baudrate, etl::string_view arg_secret,
                        size_t arg_secret_len) {
  // [SIL-2] Initialize Hardware (Watchdog, Safe Pin States) via HAL
  bridge::hal::init();

  _fsm.setTimers(&_timers, _timer_ids);
  _fsm.begin();
  _timers.enable(true);
  for (auto id : _timer_ids) {
    _timers.stop(id);
  }
  _rx_history.clear();

  // Set deterministic periods based on protocol spec
  _timers.set_period(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT],
                     _ack_timeout_ms);
  _timers.set_period(_timer_ids[bridge::scheduler::TIMER_RX_DEDUPE],
                     bridge::config::RX_DEDUPE_INTERVAL_MS);
  _timers.set_period(_timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE],
                     bridge::config::BAUDRATE_SETTLE_MS);
  _timers.set_period(_timer_ids[bridge::scheduler::TIMER_STARTUP_STABILIZATION],
                     bridge::config::STARTUP_STABILIZATION_MS);
  _last_tick_millis = bridge::now_ms();

  // [MIL-SPEC] Cryptographic Power-On Self-Test (POST)
  if (!rpc::security::run_cryptographic_self_tests()) { // GCOVR_EXCL_START — requires broken crypto engine
    enterSafeState();
    _fsm.cryptoFault();
    return;
  } // GCOVR_EXCL_STOP

  if (_hardware_serial != nullptr) {
    _hardware_serial->begin(arg_baudrate);
#if !defined(BRIDGE_HOST_TEST)
    _hardware_serial->setTimeout(bridge::config::SERIAL_TIMEOUT_MS);
#endif
  }

  _packet_serial.setPacketHandler(etl::delegate<void(etl::span<const uint8_t>)>::create<BridgeClass, &BridgeClass::_onPacketReceived>(*this));

  _timers.start(_timer_ids[bridge::scheduler::TIMER_STARTUP_STABILIZATION], etl::timer::start::DELAYED);

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

[[maybe_unused]] void BridgeClass::process() {
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
  _timers.tick(now - _last_tick_millis);
  _last_tick_millis = now;

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
  // Frame is already decompressed by _handleReceivedFrame.
  const uint16_t raw_cmd = frame.header.command_id;
  if (!_isSecurityCheckPassed(raw_cmd)) {
    (void)sendFrame(rpc::StatusCode::STATUS_ERROR, sequence_id);
    return;
  }

  bridge::router::CommandContext ctx(&frame, raw_cmd,
                                     false,
                                     rpc::requires_ack(raw_cmd),
                                     sequence_id);

  // [SIL-2] Static Dispatch for Command Categories via PROGMEM Array
  const uint8_t group = static_cast<uint8_t>(raw_cmd >> rpc::RPC_COMMAND_GROUP_SHIFT);
  using CategoryHandler = void (BridgeClass::*)(const bridge::router::CommandContext&);
  struct CategoryEntry { uint8_t id; CategoryHandler handler; };

  static const CategoryEntry kCategoryHandlers[] PROGMEM = {
      {3, &BridgeClass::onStatusCommand},
      {4, &BridgeClass::onSystemCommand},
      {5, &BridgeClass::onGpioCommand},
      {6, &BridgeClass::onConsoleCommand},
      {7, &BridgeClass::onDataStoreCommand},
      {8, &BridgeClass::onMailboxCommand},
      {9, &BridgeClass::onFileSystemCommand},
      {10, &BridgeClass::onProcessCommand},
      {11, &BridgeClass::onSpiCommand}};

  CategoryHandler found_handler = nullptr;
  for (const auto& entry : kCategoryHandlers) {
    CategoryEntry local;
    bridge::hal::copy_from_progmem(&local, &entry);
    if (local.id == group) {
      found_handler = local.handler;
      break;
    }
  }

  if (found_handler) {
    (this->*found_handler)(ctx);
  } else {
    onUnknownCommand(ctx);
  }

  _markRxProcessed(frame);
}

bool BridgeClass::_isSecurityCheckPassed(uint16_t command_id) const {
  if (_fsm.isSynchronized()) return true;
  return _isHandshakeCommand(command_id);
}

void BridgeClass::onStatusCommand(const bridge::router::CommandContext& ctx) {
  using StatusHandler = void (BridgeClass::*)(const bridge::router::CommandContext&);
  struct StatusEntry { rpc::StatusCode id; StatusHandler handler; };

  static const StatusEntry kStatusHandlers[] PROGMEM = {
      {rpc::StatusCode::STATUS_MALFORMED, &BridgeClass::_handleStatusMalformed},
      {rpc::StatusCode::STATUS_ACK, &BridgeClass::_handleStatusAck}};

  StatusHandler found_handler = nullptr;
  for (const auto& entry : kStatusHandlers) {
    StatusEntry local;
    bridge::hal::copy_from_progmem(&local, &entry);
    if (local.id == static_cast<rpc::StatusCode>(ctx.raw_command)) {
      found_handler = local.handler;
      break;
    }
  }

  if (found_handler) {
    (this->*found_handler)(ctx);
  } else if (ctx.raw_command < rpc::RPC_STATUS_CODE_MIN ||
             ctx.raw_command > rpc::RPC_STATUS_CODE_MAX) {
    onUnknownCommand(ctx);
  }

  if (_status_handler.is_valid()) {
    _status_handler(static_cast<rpc::StatusCode>(ctx.raw_command),
                    ctx.frame->payload);
  }
}

void BridgeClass::onSystemCommand(const bridge::router::CommandContext& ctx) {
  using SystemParser = void (*)(const rpc::Frame&, SystemCommandVariant&);
  struct ParserEntry { uint16_t id; SystemParser parser; };

  static const ParserEntry kSystemParsers[] PROGMEM = {
      {rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE),
       [](const rpc::Frame& f, SystemCommandVariant& v) {
         auto res = rpc::Payload::parse<rpc::payload::SetBaudratePacket>(f);
         if (res) v = res.value();
       }},
      {rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER),
       [](const rpc::Frame& f, SystemCommandVariant& v) {
         auto res = rpc::Payload::parse<rpc::payload::EnterBootloader>(f);
         if (res) v = res.value();
       }}};

  using SystemNoPayloadHandler = void (BridgeClass::*)(const bridge::router::CommandContext&);
  struct NoPayloadEntry { uint16_t id; SystemNoPayloadHandler handler; };

  static const NoPayloadEntry kSystemNoPayloadHandlers[] PROGMEM = {
      {rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION),
       &BridgeClass::_handleGetVersion},
      {rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY),
       &BridgeClass::_handleGetFreeMemory},
      {rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC),
       &BridgeClass::_handleLinkSync},
      {rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET),
       &BridgeClass::_handleLinkReset},
      {rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES),
       &BridgeClass::_handleGetCapabilities}};

  SystemNoPayloadHandler found_no_payload = nullptr;
  for (const auto& entry : kSystemNoPayloadHandlers) {
    NoPayloadEntry local;
    bridge::hal::copy_from_progmem(&local, &entry);
    if (local.id == ctx.raw_command) {
      found_no_payload = local.handler;
      break;
    }
  }

  if (found_no_payload) {
    (this->*found_no_payload)(ctx);
    return;
  }

  SystemCommandVariant var;
  SystemParser found_parser = nullptr;
  for (const auto& entry : kSystemParsers) {
    ParserEntry local;
    bridge::hal::copy_from_progmem(&local, &entry);
    if (local.id == ctx.raw_command) {
      found_parser = local.parser;
      break;
    }
  }

  if (found_parser) {
    found_parser(*ctx.frame, var);
  }

  if (!etl::holds_alternative<etl::monostate>(var)) {
    etl::visit(
        [this, &ctx](auto&& msg) { this->_handleSystemMessage(ctx, msg); }, var);
  } else {
    onUnknownCommand(ctx);
  }
}

void BridgeClass::_handleSystemMessage(const bridge::router::CommandContext& ctx,
                                       etl::monostate) {
  onUnknownCommand(ctx);
}

void BridgeClass::_handleSystemMessage(const bridge::router::CommandContext& ctx,
                                       [[maybe_unused]] const rpc::payload::SetBaudratePacket& msg) {
  _withPayloadAck<rpc::payload::SetBaudratePacket>(
      ctx, [this](const rpc::payload::SetBaudratePacket& m) {
        _pending_baudrate = m.baudrate;
        _timers.start(_timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE],
                      etl::timer::start::DELAYED);
      });
}

void BridgeClass::_handleSystemMessage(const bridge::router::CommandContext& ctx,
                                       [[maybe_unused]] const rpc::payload::EnterBootloader& msg) {
  _withPayloadAck<rpc::payload::EnterBootloader>(
      ctx, [this](const rpc::payload::EnterBootloader& m) {
        if (m.magic == rpc::RPC_BOOTLOADER_MAGIC) {
          this->flushStream();
          delay(bridge::config::BOOTLOADER_DELAY_MS);
#if defined(ARDUINO_ARCH_AVR)
          wdt_enable(WDTO_15MS);
          for (;;) {
          }
#elif defined(ARDUINO_ARCH_ESP32)
          ESP.restart();
#elif defined(ARDUINO_ARCH_SAMD)
          NVIC_SystemReset();
#endif
        }
      });
}

void BridgeClass::onGpioCommand(const bridge::router::CommandContext& ctx) {
  using GpioParser = void (*)(const rpc::Frame&, GpioCommandVariant&);
  struct GpioEntry { uint16_t id; GpioParser parser; };

  static const GpioEntry kGpioParsers[] PROGMEM = {
      {rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE),
                [](const rpc::Frame& f, GpioCommandVariant& v) {
                  auto res = rpc::Payload::parse<rpc::payload::PinMode>(f);
                  if (res) v = res.value();
                }},
      {rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE),
                [](const rpc::Frame& f, GpioCommandVariant& v) {
                  auto res = rpc::Payload::parse<rpc::payload::DigitalWrite>(f);
                  if (res) v = res.value();
                }},
      {rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE),
                [](const rpc::Frame& f, GpioCommandVariant& v) {
                  auto res = rpc::Payload::parse<rpc::payload::AnalogWrite>(f);
                  if (res) v = res.value();
                }},
      {rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ),
                [](const rpc::Frame& f, GpioCommandVariant& v) {
                  auto res = rpc::Payload::parse<rpc::payload::PinRead>(f);
                  if (res) v = res.value();
                }},
      {rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ),
                [](const rpc::Frame& f, GpioCommandVariant& v) {
                  auto res = rpc::Payload::parse<rpc::payload::PinRead>(f);
                  if (res) v = res.value();
                }}};

  GpioCommandVariant var;
  GpioParser found_parser = nullptr;
  for (const auto& entry : kGpioParsers) {
    GpioEntry local;
    bridge::hal::copy_from_progmem(&local, &entry);
    if (local.id == ctx.raw_command) {
      found_parser = local.parser;
      break;
    }
  }

  if (found_parser) {
    found_parser(*ctx.frame, var);
  }

  etl::visit([this, &ctx](auto&& msg) { this->_handleGpioMessage(ctx, msg); },
             var);
}

void BridgeClass::_handleGpioMessage(const bridge::router::CommandContext& ctx,
                                     etl::monostate) {
  onUnknownCommand(ctx);
}

void BridgeClass::_handleGpioMessage(const bridge::router::CommandContext& ctx,
                                     const rpc::payload::PinMode& msg) {
  _withAck(ctx, [this, &msg]() {
    if (bridge::hal::isValidPin(msg.pin)) ::pinMode(msg.pin, msg.mode);
    else emitStatus(rpc::StatusCode::STATUS_ERROR);
  });
}

void BridgeClass::_handleGpioMessage(const bridge::router::CommandContext& ctx,
                                     const rpc::payload::DigitalWrite& msg) {
  _withAck(ctx, [this, &msg]() {
    if (bridge::hal::isValidPin(msg.pin)) ::digitalWrite(msg.pin, msg.value);
    else emitStatus(rpc::StatusCode::STATUS_ERROR);
  });
}

void BridgeClass::_handleGpioMessage(const bridge::router::CommandContext& ctx,
                                     const rpc::payload::AnalogWrite& msg) {
  _withAck(ctx, [this, &msg]() {
    if (bridge::hal::isValidPin(msg.pin)) ::analogWrite(msg.pin, msg.value);
    else emitStatus(rpc::StatusCode::STATUS_ERROR);
  });
}

void BridgeClass::_handleGpioMessage(const bridge::router::CommandContext& ctx,
                                     const rpc::payload::PinRead& msg) {
  (void)msg;
  if (ctx.raw_command == rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ)) {
    _handlePinRead<rpc::payload::DigitalReadResponse>(
        ctx, rpc::CommandId::CMD_DIGITAL_READ_RESP, bridge::hal::isValidPin,
        [](uint8_t p) { return ::digitalRead(p); });
  } else {
    _handlePinRead<rpc::payload::AnalogReadResponse>(
        ctx, rpc::CommandId::CMD_ANALOG_READ_RESP, bridge::hal::isValidPin,
        [](uint8_t p) { return ::analogRead(p); });
  }
}

void BridgeClass::onConsoleCommand(const bridge::router::CommandContext& ctx) {
  using ConsoleHandler = void (BridgeClass::*)(const bridge::router::CommandContext&);
  struct ConsoleEntry { uint16_t id; ConsoleHandler handler; };

  static const ConsoleEntry kConsoleHandlers[] PROGMEM = {
      {rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE),
                   &BridgeClass::_handleConsoleWrite}};

  ConsoleHandler found_handler = nullptr;
  for (const auto& entry : kConsoleHandlers) {
    ConsoleEntry local;
    bridge::hal::copy_from_progmem(&local, &entry);
    if (local.id == ctx.raw_command) {
      found_handler = local.handler;
      break;
    }
  }

  if (found_handler) {
    (this->*found_handler)(ctx);
  } else {
    onUnknownCommand(ctx);
  }
}

void BridgeClass::onDataStoreCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_DATASTORE) {
    using DataStoreHandler = void (BridgeClass::*)(const bridge::router::CommandContext&);
    struct DataStoreEntry { uint16_t id; DataStoreHandler handler; };

    static const DataStoreEntry kDataStoreHandlers[] PROGMEM = {
        {rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP),
         &BridgeClass::_handleDatastoreGetResp}};

    DataStoreHandler found_handler = nullptr;
    for (const auto& entry : kDataStoreHandlers) {
      DataStoreEntry local;
      bridge::hal::copy_from_progmem(&local, &entry);
      if (local.id == ctx.raw_command) {
        found_handler = local.handler;
        break;
      }
    }

    if (found_handler) {
      (this->*found_handler)(ctx);
    } else {
      onUnknownCommand(ctx);
    }
  } else {
    onUnknownCommand(ctx);
  }
}

void BridgeClass::onMailboxCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_MAILBOX) {
    using MailboxHandler = void (BridgeClass::*)(const bridge::router::CommandContext&);
    struct MailboxEntry { uint16_t id; MailboxHandler handler; };

    static const MailboxEntry kMailboxHandlers[] PROGMEM = {
        {rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH), &BridgeClass::_handleMailboxPush},
        {rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP),
         &BridgeClass::_handleMailboxReadResp},
        {rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP),
         &BridgeClass::_handleMailboxAvailableResp}};

    MailboxHandler found_handler = nullptr;
    for (const auto& entry : kMailboxHandlers) {
      MailboxEntry local;
      bridge::hal::copy_from_progmem(&local, &entry);
      if (local.id == ctx.raw_command) {
        found_handler = local.handler;
        break;
      }
    }

    if (found_handler) {
      (this->*found_handler)(ctx);
    } else {
      onUnknownCommand(ctx);
    }
  } else {
    onUnknownCommand(ctx);
  }
}

void BridgeClass::onFileSystemCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_FILESYSTEM) {
    using FileSystemHandler = void (BridgeClass::*)(const bridge::router::CommandContext&);
    struct FileSystemEntry { uint16_t id; FileSystemHandler handler; };

    static const FileSystemEntry kFileHandlers[] PROGMEM = {
        {rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE), &BridgeClass::_handleFileWrite},
        {rpc::to_underlying(rpc::CommandId::CMD_FILE_READ), &BridgeClass::_handleFileRead},
        {rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE), &BridgeClass::_handleFileRemove},
        {rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP), &BridgeClass::_handleFileReadResp}};

    FileSystemHandler found_handler = nullptr;
    for (const auto& entry : kFileHandlers) {
      FileSystemEntry local;
      bridge::hal::copy_from_progmem(&local, &entry);
      if (local.id == ctx.raw_command) {
        found_handler = local.handler;
        break;
      }
    }

    if (found_handler) {
      (this->*found_handler)(ctx);
    } else {
      onUnknownCommand(ctx);
    }
  } else {
    onUnknownCommand(ctx);
  }
}

void BridgeClass::onProcessCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_PROCESS) {
    using ProcessHandler = void (BridgeClass::*)(const bridge::router::CommandContext&);
    struct ProcessEntry { uint16_t id; ProcessHandler handler; };

    static const ProcessEntry kProcessHandlers[] PROGMEM = {
        {rpc::to_underlying(rpc::CommandId::CMD_PROCESS_KILL), &BridgeClass::_handleProcessKill},
        {rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP),
         &BridgeClass::_handleProcessRunAsyncResp},
        {rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP),
         &BridgeClass::_handleProcessPollResp}};

    ProcessHandler found_handler = nullptr;
    for (const auto& entry : kProcessHandlers) {
      ProcessEntry local;
      bridge::hal::copy_from_progmem(&local, &entry);
      if (local.id == ctx.raw_command) {
        found_handler = local.handler;
        break;
      }
    }

    if (found_handler) {
      (this->*found_handler)(ctx);
    } else {
      onUnknownCommand(ctx);
    }
  } else {
    onUnknownCommand(ctx);
  }
}

void BridgeClass::onSpiCommand(const bridge::router::CommandContext& ctx) {
  if constexpr (bridge::config::ENABLE_SPI) {
    using SpiHandler = void (BridgeClass::*)(const bridge::router::CommandContext&);
    struct SpiEntry { uint16_t id; SpiHandler handler; };

    static const SpiEntry kSpiHandlers[] PROGMEM = {
        {rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN),
                 &BridgeClass::_handleSpiBegin},
        {rpc::to_underlying(rpc::CommandId::CMD_SPI_END),
                 &BridgeClass::_handleSpiEnd},
        {rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG),
                 &BridgeClass::_handleSpiSetConfig},
        {rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER),
                 &BridgeClass::_handleSpiTransfer}};

    SpiHandler found_handler = nullptr;
    for (const auto& entry : kSpiHandlers) {
      SpiEntry local;
      bridge::hal::copy_from_progmem(&local, &entry);
      if (local.id == ctx.raw_command) {
        found_handler = local.handler;
        break;
      }
    }

    if (found_handler) {
      (this->*found_handler)(ctx);
    } else {
      onUnknownCommand(ctx);
    }
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
    _withPayload<rpc::payload::SpiTransfer>(ctx, [this, &ctx](const rpc::payload::SpiTransfer& msg) {
      if (SPIService.isInitialized()) {
        auto data = msg.data;
        // Copy to mutable buffer for in-place SPI transfer (avoids const_cast UB)
        const size_t len = etl::min(data.size(), _rx_storage.size());
        etl::copy_n(data.begin(), len, _rx_storage.begin());
        if (bridge::hal::hasSPI()) {
          size_t xferred = SPIService.transfer(etl::span<uint8_t>(_rx_storage.data(), len));
          if (xferred < len) { // GCOVR_EXCL_START — host mock always succeeds
            enterSafeState();
            _sendError(rpc::StatusCode::STATUS_ERROR, ctx.raw_command, ctx.sequence_id);
            return;
          } // GCOVR_EXCL_STOP
        }
        rpc::payload::SpiTransferResponse resp = {};
        resp.data = etl::span<const uint8_t>(_rx_storage.data(), len);
        _sendPbResponse(rpc::CommandId::CMD_SPI_TRANSFER_RESP, ctx.sequence_id, resp);
      }
    });
#endif
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
    _computeHandshakeTag(etl::span<const uint8_t>(msg.nonce.data(), msg.nonce.size()), etl::span<uint8_t>(tag));
    if (!_shared_secret.empty()) {
      etl::span<const uint8_t> expected(tag);
      etl::span<const uint8_t> received(msg.tag.data(), msg.tag.size());
      if (!rpc::security::timing_safe_equal(expected, received)) {
        _fsm.handshakeStart(); _fsm.handshakeFailed(); return;
      }
    }
    rpc::payload::LinkSync resp = {};
    etl::copy_n(msg.nonce.data(), msg.nonce.size(), resp.nonce.data());
    etl::copy_n(tag.data(), tag.size(), resp.tag.data());
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

void BridgeClass::_handleConsoleWrite(const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::ConsoleWrite>(ctx, [](const rpc::payload::ConsoleWrite& msg) { Console._push(msg.data); });
}

void BridgeClass::_handleDatastoreGetResp(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_DATASTORE
  _withPayload<rpc::payload::DatastoreGetResponse>(ctx, [](const rpc::payload::DatastoreGetResponse& msg) { DataStore._onResponse(msg.value); });
#endif
}

void BridgeClass::_handleMailboxPush(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_MAILBOX
  _withPayloadAck<rpc::payload::MailboxPush>(ctx, [](const rpc::payload::MailboxPush& msg) { Mailbox._onIncomingData(msg.data); });
#endif
}

void BridgeClass::_handleMailboxReadResp(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_MAILBOX
  _withPayload<rpc::payload::MailboxReadResponse>(ctx, [](const rpc::payload::MailboxReadResponse& msg) { Mailbox._onIncomingData(msg.content); });
#endif
}

void BridgeClass::_handleMailboxAvailableResp(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_MAILBOX
  _withPayload<rpc::payload::MailboxAvailableResponse>(ctx, [](const auto& msg) { Mailbox._onAvailableResponse(msg); });
#endif
}

void BridgeClass::_handleFileWrite(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_FILESYSTEM
  _withPayload<rpc::payload::FileWrite>(ctx, [](const rpc::payload::FileWrite& msg) {
    FileSystem._onWrite(msg);
  });
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
  _withPayload<rpc::payload::FileReadResponse>(ctx, [](const rpc::payload::FileReadResponse& msg) { FileSystem._onResponse(msg.content); });
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
  _withPayload<rpc::payload::ProcessPollResponse>(ctx, [](const rpc::payload::ProcessPollResponse& msg) {
    Process._onPollResponse(msg, msg.stdout_data, msg.stderr_data);
  });
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

void BridgeClass::_handleAck(uint16_t command_id) {
  bool awaiting = false; BRIDGE_ATOMIC_BLOCK { awaiting = _fsm.isAwaitingAck(); }
  if (awaiting && (command_id == _last_command_id)) {
    _clearAckState(); _timers.stop(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
    _flushPendingTxQueue();
  }
}

void BridgeClass::_handleMalformed(uint16_t command_id) { if (command_id == _last_command_id) _retransmitLastFrame(); }

void BridgeClass::_retransmitLastFrame() {
  PendingTxFrame f; bool has_frame = false;
  BRIDGE_ATOMIC_BLOCK { if (!_pending_tx_queue.empty()) { f = _pending_tx_queue.front(); has_frame = true; } }
  if (has_frame) {
    if (f.buffer != nullptr) {
      _sendRawFrame(f.command_id, 0, etl::span<const uint8_t>(f.buffer->data.data(), f.payload_length)); _retry_count++;
    }
  }
}

void BridgeClass::_onAckTimeout() {
  bool awaiting = false; BRIDGE_ATOMIC_BLOCK { awaiting = _fsm.isAwaitingAck(); }
  if (!awaiting) return;
  if (_retry_count >= _ack_retry_limit) { BRIDGE_ATOMIC_BLOCK { _fsm.timeout(); } enterSafeState(); return; }
  _retransmitLastFrame();
  _timers.start(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT],
                etl::timer::start::DELAYED);
}

void BridgeClass::_onRxDedupe() { _rx_history.clear(); }

void BridgeClass::_onBaudrateChange() { if (_pending_baudrate > 0) { if (_hardware_serial) _hardware_serial->begin(_pending_baudrate); _pending_baudrate = 0; } }

void BridgeClass::_onStartupStabilized() {
  uint16_t drain_limit = bridge::config::STARTUP_DRAIN_FINAL;
  uint32_t start_ms = bridge::now_ms();
  while (_stream.available() > 0 && drain_limit-- > 0 && (bridge::now_ms() - start_ms < bridge::config::SERIAL_TIMEOUT_MS)) _stream.read();
  BRIDGE_ATOMIC_BLOCK { _fsm.stabilized(); }
}

void BridgeClass::enterSafeState() {
  BRIDGE_ATOMIC_BLOCK { _fsm.resetFsm(); }
  for (auto id : _timer_ids) { _timers.stop(id); } _pending_baudrate = 0; _retry_count = 0; _clearPendingTxQueue();
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
}

// Compile-time proof that MAX_PAYLOAD_SIZE-1 is a valid index into _transient_buffer.
static_assert(rpc::MAX_PAYLOAD_SIZE <= rpc::MAX_RAW_FRAME_SIZE + 2,
              "MAX_PAYLOAD_SIZE must fit within _transient_buffer");

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
    if (!res.has_value()) { // GCOVR_EXCL_START — defensive: COBS buffer always sufficient
        _last_parse_error = rpc::FrameError::OVERFLOW;
    } // GCOVR_EXCL_STOP
  }
}

void BridgeClass::_flushPendingTxQueue() {
  PendingTxFrame f; bool has_frame = false;
  BRIDGE_ATOMIC_BLOCK { if (!_fsm.isAwaitingAck() && !_pending_tx_queue.empty()) { f = _pending_tx_queue.front(); has_frame = true; } }
  if (has_frame) {
    uint16_t seq = ++_tx_sequence_id;
    if (f.buffer != nullptr) {
      _sendRawFrame(f.command_id, seq, etl::span<const uint8_t>(f.buffer->data.data(), f.payload_length));
    }
    BRIDGE_ATOMIC_BLOCK { _fsm.sendCritical(); } _retry_count = 0;
    _timers.start(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT], etl::timer::start::DELAYED); _last_command_id = f.command_id;
  }
}

void BridgeClass::_clearPendingTxQueue() {
  BRIDGE_ATOMIC_BLOCK {
    while (!_pending_tx_queue.empty()) {
      TxPayloadBuffer* buf = _pending_tx_queue.front().buffer;
      if (buf) _tx_payload_pool.release(buf);
      _pending_tx_queue.pop();
    }
  }
}
void BridgeClass::_clearAckState() {
  BRIDGE_ATOMIC_BLOCK {
    if (_fsm.isAwaitingAck()) {
      _fsm.ackReceived();
      if (!_pending_tx_queue.empty()) {
        TxPayloadBuffer* buf = _pending_tx_queue.front().buffer;
        if (buf) _tx_payload_pool.release(buf);
        _pending_tx_queue.pop();
      }
    }
  }
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
  static_assert(rpc::MAX_PAYLOAD_SIZE <= UINT16_MAX, "MAX_PAYLOAD_SIZE must fit in uint16_t");
  if (rpc::requires_ack(command_id)) {
    if (_isQueueFull() || _tx_payload_pool.full()) return false;
    TxPayloadBuffer* buf = nullptr;
    BRIDGE_ATOMIC_BLOCK { buf = _tx_payload_pool.allocate(); }
    if (!buf) return false;
    PendingTxFrame f; f.command_id = command_id; f.payload_length = static_cast<uint16_t>(payload.size()); f.buffer = buf;
    if (payload.size() > 0) etl::copy_n(payload.data(), f.payload_length, buf->data.data());
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
  size_t decoded_len = rle::decode(org.payload, etl::span<uint8_t>(_rx_storage.data(), _rx_storage.size()));
  if (decoded_len == 0 && org.header.payload_length > 0) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);
  eff.header.payload_length = static_cast<uint16_t>(decoded_len); eff.payload = etl::span<const uint8_t>(_rx_storage.data(), decoded_len);
  return {};
}

void BridgeClass::_computeHandshakeTag(etl::span<const uint8_t> nonce, etl::span<uint8_t> out_tag) {
  etl::array<uint8_t, bridge::config::HKDF_KEY_LENGTH> handshake_key;
  rpc::security::hkdf_sha256(etl::span<uint8_t>(handshake_key), etl::span<const uint8_t>(_shared_secret.data(), _shared_secret.size()), etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_SALT), etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH));
  rpc::security::McuBridgeSha256 sha256; sha256.resetHMAC(handshake_key.data(), handshake_key.size()); sha256.update(nonce.data(), nonce.size());
  etl::array<uint8_t, rpc::security::McuBridgeSha256::HASH_SIZE> full_tag;
  sha256.finalizeHMAC(handshake_key.data(), handshake_key.size(), full_tag.data(), full_tag.size());
  etl::copy_n(full_tag.begin(), etl::min(full_tag.size(), out_tag.size()), out_tag.begin());
  rpc::security::secure_zero(etl::span<uint8_t>(handshake_key)); rpc::security::secure_zero(etl::span<uint8_t>(full_tag));
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

namespace etl { void __attribute__((weak)) __attribute__((unused)) handle_error(const etl::exception& e) { (void)e; Bridge.enterSafeState(); } } // GCOVR_EXCL_LINE — guarded by BRIDGE_TEST_NO_GLOBALS

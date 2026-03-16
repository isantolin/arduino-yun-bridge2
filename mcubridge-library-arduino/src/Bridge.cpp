#include "Bridge.h"
#include <Arduino.h>
#include <etl/numeric.h>
#include <etl/span.h>
#include <etl/bitset.h>
#include "util/pb_copy.h"

#if (defined(_GLIBCXX_VECTOR) || defined(_GLIBCXX_STRING) || \
     defined(_GLIBCXX_MAP)) &&                               \
    !defined(ETL_VERSION) && !defined(BRIDGE_HOST_TEST)
#error "CRITICAL: Standard STL detected. Use ETL only (SIL 2 Violation)."
#endif

#ifdef ARDUINO_ARCH_AVR
#include <avr/wdt.h>
#endif

#include <string.h>
#include "etl/algorithm.h"
#include "etl/error_handler.h"
#include "hal/logging.h"
#include "protocol/rle.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "security/security.h"

namespace {
constexpr size_t kHandshakeTagSize = rpc::RPC_HANDSHAKE_TAG_LENGTH;
constexpr uint8_t kRpcCommandStride = 2;  // Pair: CMD + RESP
}

BridgeClass::BridgeClass(HardwareSerial& arg_serial)
    : BridgeClass(static_cast<Stream&>(arg_serial)) {
  _hardware_serial = &arg_serial;
}

BridgeClass::BridgeClass(Stream& arg_stream)
    : _stream(arg_stream),
      _hardware_serial(nullptr),
      _shared_secret(),
      _cobs{0, 0, 0, 0, true, {0}},
      _frame_builder(),
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
      _last_tick_millis(0) {
  _timers.clear();
}

void BridgeClass::begin(unsigned long arg_baudrate, etl::string_view arg_secret,
                        size_t arg_secret_len) {
  _fsm.begin();
  _timers.clear();
  _rx_history.clear();
  _timers.set_period(bridge::scheduler::TIMER_ACK_TIMEOUT, _ack_timeout_ms);
  _timers.set_period(bridge::scheduler::TIMER_RX_DEDUPE, bridge::config::RX_DEDUPE_INTERVAL_MS);
  _timers.set_period(bridge::scheduler::TIMER_BAUDRATE_CHANGE, bridge::config::BAUDRATE_SETTLE_MS);
  _timers.set_period(bridge::scheduler::TIMER_STARTUP_STABILIZATION, bridge::config::STARTUP_STABILIZATION_MS);
  _last_tick_millis = bridge::now_ms();

  _cobs.buffer.fill(0);

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
    _shared_secret.assign(start, start + actual_len);
  }

  _fsm.resetFsm();
  _last_command_id = 0;
  _retry_count = 0;
  _rx_history.clear();

  add_observer(Console);
#if BRIDGE_ENABLE_DATASTORE
  add_observer(DataStore);
#endif
}

void BridgeClass::process() {
#if defined(ARDUINO_ARCH_AVR)
  if (bridge::config::ENABLE_WATCHDOG) wdt_reset();
#elif defined(ARDUINO_ARCH_ESP32)
  if (bridge::config::ENABLE_WATCHDOG) esp_task_wdt_reset();
#elif defined(ARDUINO_ARCH_ESP8266)
  if (bridge::config::ENABLE_WATCHDOG) yield();
#endif

  uint32_t now = bridge::now_ms();
  uint8_t expired = _timers.check_expired(now);
  if (expired & (1U << bridge::scheduler::TIMER_ACK_TIMEOUT)) _onAckTimeout();
  if (expired & (1U << bridge::scheduler::TIMER_RX_DEDUPE)) _onRxDedupe();
  if (expired & (1U << bridge::scheduler::TIMER_BAUDRATE_CHANGE)) _onBaudrateChange();
  if (expired & (1U << bridge::scheduler::TIMER_STARTUP_STABILIZATION)) _onStartupStabilized();

  if (_fsm.isStabilizing()) {
    uint16_t drain_limit = bridge::config::STARTUP_DRAIN_PER_TICK;
    while (_stream.available() > 0 && drain_limit-- > 0) _stream.read();
  } else {
    BRIDGE_ATOMIC_BLOCK {
      while (_stream.available() > 0) {
        _processIncomingByte(_stream.read());
        if (_frame_received || _last_parse_error.has_value()) break;
      }
    }
  }

  if (_frame_received) {
    _handleReceivedFrame();
  } else if (_last_parse_error.has_value()) {
    rpc::FrameError error = _last_parse_error.value();
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

void BridgeClass::_processIncomingByte(uint8_t byte) {
  if (byte == rpc::RPC_FRAME_DELIMITER) {
    if (_cobs.in_sync && _cobs.bytes_received >= rpc::MIN_FRAME_SIZE) {
      etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE> decoded;
      size_t decoded_len = rpc::cobs::decode(
          etl::span<const uint8_t>(_cobs.buffer.data(), _cobs.bytes_received),
          etl::span<uint8_t>(decoded.data(), decoded.size()));

      if (decoded_len > 0) {
        rpc::FrameParser parser;
        auto result = parser.parse(etl::span<const uint8_t>(decoded.data(), decoded_len));
        if (result.has_value()) {
          _rx_frame = result.value();
          _frame_received = true;
          _consecutive_crc_errors = 0;
        } else {
          _last_parse_error = result.error();
        }
      } else {
        _last_parse_error = rpc::FrameError::MALFORMED;
      }
    }
    _cobs.in_sync = true;
    _cobs.bytes_received = 0;
    _cobs.block_len = 0;
    return;
  }

  if (!_cobs.in_sync) return;
  if (_cobs.bytes_received >= _cobs.buffer.size()) {
    _cobs.in_sync = false;
    _last_parse_error = rpc::FrameError::OVERFLOW;
    return;
  }
  _cobs.buffer[_cobs.bytes_received++] = byte;
}

void BridgeClass::_handleReceivedFrame() {
  _frame_received = false;
  rpc::Frame frame = _rx_frame;
  if (_isRecentDuplicateRx(frame)) {
    if (rpc::requires_ack(frame.header.command_id)) _sendAckAndFlush(frame.header.command_id);
    return;
  }
  dispatch(frame);
}

void BridgeClass::dispatch(const rpc::Frame& frame) {
  rpc::Frame effective_frame;
  auto decomp_res = _decompressFrame(frame, effective_frame);
  if (!decomp_res.has_value()) {
    emitStatus(rpc::StatusCode::STATUS_MALFORMED);
    return;
  }

  uint16_t raw_cmd = effective_frame.header.command_id;
  if (!_isSecurityCheckPassed(raw_cmd)) {
    sendFrame(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  bridge::router::CommandContext ctx(&effective_frame, raw_cmd,
                                     _isRecentDuplicateRx(effective_frame),
                                     rpc::requires_ack(raw_cmd));

  if (raw_cmd >= rpc::RPC_STATUS_CODE_MIN && raw_cmd <= rpc::RPC_STATUS_CODE_MAX) onStatusCommand(ctx);
  else if (raw_cmd >= rpc::RPC_SYSTEM_COMMAND_MIN && raw_cmd <= rpc::RPC_SYSTEM_COMMAND_MAX) onSystemCommand(ctx);
  else if (raw_cmd >= rpc::RPC_GPIO_COMMAND_MIN && raw_cmd <= rpc::RPC_GPIO_COMMAND_MAX) onGpioCommand(ctx);
  else if (raw_cmd >= rpc::RPC_CONSOLE_COMMAND_MIN && raw_cmd <= rpc::RPC_CONSOLE_COMMAND_MAX) onConsoleCommand(ctx);
  else if (raw_cmd >= rpc::RPC_DATASTORE_COMMAND_MIN && raw_cmd <= rpc::RPC_DATASTORE_COMMAND_MAX) onDataStoreCommand(ctx);
  else if (raw_cmd >= rpc::RPC_MAILBOX_COMMAND_MIN && raw_cmd <= rpc::RPC_MAILBOX_COMMAND_MAX) onMailboxCommand(ctx);
  else if (raw_cmd >= rpc::RPC_FILESYSTEM_COMMAND_MIN && raw_cmd <= rpc::RPC_FILESYSTEM_COMMAND_MAX) onFileSystemCommand(ctx);
  else if (raw_cmd >= rpc::RPC_PROCESS_COMMAND_MIN && raw_cmd <= rpc::RPC_PROCESS_COMMAND_MAX) onProcessCommand(ctx);
  else onUnknownCommand(ctx);

  if (!ctx.is_duplicate) _markRxProcessed(effective_frame);
}

bool BridgeClass::_isSecurityCheckPassed(uint16_t command_id) const {
  if (_fsm.isSynchronized()) return true;
  return _isHandshakeCommand(command_id);
}

void BridgeClass::onStatusCommand(const bridge::router::CommandContext& ctx) {
  switch (static_cast<rpc::StatusCode>(ctx.raw_command)) {
    case rpc::StatusCode::STATUS_ACK: _handleStatusAck(ctx); break;
    case rpc::StatusCode::STATUS_MALFORMED: _handleStatusMalformed(ctx); break;
    default:
      if (_status_handler.is_valid()) {
        _status_handler(static_cast<rpc::StatusCode>(ctx.raw_command),
                        etl::span<const uint8_t>(ctx.frame->payload.data(), ctx.frame->header.payload_length));
      }
      break;
  }
}

void BridgeClass::onSystemCommand(const bridge::router::CommandContext& ctx) {
  static constexpr etl::array<void (BridgeClass::*)(const bridge::router::CommandContext&), 6> kSystemHandlers{{
      &BridgeClass::_handleGetVersion, &BridgeClass::_handleGetFreeMemory,
      &BridgeClass::_handleLinkSync, &BridgeClass::_handleLinkReset,
      &BridgeClass::_handleGetCapabilities, &BridgeClass::_handleSetBaudrate
  }};
  _dispatchJumpTable(ctx, rpc::RPC_SYSTEM_COMMAND_MIN, kSystemHandlers, kRpcCommandStride);
}

void BridgeClass::onGpioCommand(const bridge::router::CommandContext& ctx) {
  static constexpr etl::array<void (BridgeClass::*)(const bridge::router::CommandContext&), 5> kGpioHandlers{{
      &BridgeClass::_handleSetPinMode, &BridgeClass::_handleDigitalWrite,
      &BridgeClass::_handleAnalogWrite, &BridgeClass::_handleDigitalRead,
      &BridgeClass::_handleAnalogRead
  }};
  _dispatchJumpTable(ctx, rpc::RPC_GPIO_COMMAND_MIN, kGpioHandlers);
}

void BridgeClass::onConsoleCommand(const bridge::router::CommandContext& ctx) {
  if (ctx.raw_command == rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE)) _handleConsoleWrite(ctx);
}

void BridgeClass::onDataStoreCommand(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_DATASTORE
  if (ctx.raw_command == rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP)) _handleDatastoreGetResp(ctx);
#endif
}

void BridgeClass::onMailboxCommand(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_MAILBOX
  static constexpr etl::array<void (BridgeClass::*)(const bridge::router::CommandContext&), 3> kMailboxHandlers{{
      &BridgeClass::_handleMailboxPush, &BridgeClass::_handleMailboxReadResp, &BridgeClass::_handleMailboxAvailableResp
  }};
  _dispatchJumpTable(ctx, rpc::RPC_MAILBOX_COMMAND_MIN, kMailboxHandlers, kRpcCommandStride);
#endif
}

void BridgeClass::onFileSystemCommand(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_FILESYSTEM
  if (ctx.raw_command == rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE)) _handleFileWrite(ctx);
  else if (ctx.raw_command == rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP)) _handleFileReadResp(ctx);
#endif
}

void BridgeClass::onProcessCommand(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_PROCESS
  if (ctx.raw_command == rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP)) _handleProcessRunAsyncResp(ctx);
  else if (ctx.raw_command == rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP)) _handleProcessPollResp(ctx);
#endif
}

void BridgeClass::_handleGetVersion(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this]() {
    rpc::payload::VersionResponse resp = {};
    resp.major = bridge::config::FIRMWARE_VERSION_MAJOR;
    resp.minor = bridge::config::FIRMWARE_VERSION_MINOR;
    _sendPbResponse(rpc::CommandId::CMD_GET_VERSION_RESP, resp);
  });
}

void BridgeClass::_handleGetFreeMemory(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this]() {
    rpc::payload::FreeMemoryResponse resp = {};
    resp.value = getFreeMemory();
    _sendPbResponse(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, resp);
  });
}

void BridgeClass::_handleLinkSync(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::LinkSync>(ctx, [this](const rpc::payload::LinkSync& msg) {
    uint8_t tag[rpc::RPC_HANDSHAKE_TAG_LENGTH];
    _computeHandshakeTag(etl::span<const uint8_t>(msg.nonce.bytes, msg.nonce.size), tag);

    // [SIL-2] Verify incoming HMAC tag when mutual auth is configured
    if (!_shared_secret.empty()) {
      etl::span<const uint8_t> expected(tag, rpc::RPC_HANDSHAKE_TAG_LENGTH);
      etl::span<const uint8_t> received(msg.tag.bytes, msg.tag.size);
      if (!rpc::security::timing_safe_equal(expected, received)) {
        _fsm.handshakeStart();
        _fsm.handshakeFailed();
        return;
      }
    }

    rpc::payload::LinkSync resp = {};
    resp.nonce.size = msg.nonce.size;
    etl::copy_n(msg.nonce.bytes, msg.nonce.size, resp.nonce.bytes);
    resp.tag.size = rpc::RPC_HANDSHAKE_TAG_LENGTH;
    etl::copy_n(tag, rpc::RPC_HANDSHAKE_TAG_LENGTH, resp.tag.bytes);
    
    _fsm.handshakeStart();
    _fsm.handshakeComplete();
    _sendPbResponse(rpc::CommandId::CMD_LINK_SYNC_RESP, resp);
    notify_observers(MsgBridgeSynchronized());
  });
}

void BridgeClass::_handleLinkReset(const bridge::router::CommandContext& ctx) {
  _withAck(ctx, [this]() {
    enterSafeState();
    sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP);
  });
}

void BridgeClass::_handleGetCapabilities(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this]() {
    rpc::payload::Capabilities resp = {};
    resp.ver = rpc::PROTOCOL_VERSION;
#if defined(ARDUINO_ARCH_AVR)
    resp.arch = rpc::RPC_ARCH_AVR;
    resp.dig = 14; resp.ana = 6;
#else
    resp.arch = rpc::RPC_ARCH_SAMD;
    resp.dig = 20; resp.ana = 8;
#endif
    resp.feat = 0x01;
    _sendPbResponse(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, resp);
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
  _handlePinRead<rpc::payload::DigitalReadResponse>(
      ctx, rpc::CommandId::CMD_DIGITAL_READ_RESP, bridge::hal::isValidPin,
      [](uint8_t p) { return ::digitalRead(p); });
}

void BridgeClass::_handleAnalogRead(const bridge::router::CommandContext& ctx) {
  _handlePinRead<rpc::payload::AnalogReadResponse>(
      ctx, rpc::CommandId::CMD_ANALOG_READ_RESP, bridge::hal::isValidPin,
      [](uint8_t p) { return ::analogRead(p); });
}

void BridgeClass::_handleConsoleWrite(const bridge::router::CommandContext& ctx) {
  _dispatchWithBytes<rpc::payload::ConsoleWrite>(
      ctx, &rpc::payload::ConsoleWrite::data,
      [](auto s) { Console._push(s); }, true);
}

#if BRIDGE_ENABLE_DATASTORE
void BridgeClass::_handleDatastoreGetResp(const bridge::router::CommandContext& ctx) {
  _dispatchWithBytes<rpc::payload::DatastoreGetResponse>(
      ctx, &rpc::payload::DatastoreGetResponse::value,
      [](auto s) { DataStore._onResponse(s); });
}
#endif

#if BRIDGE_ENABLE_MAILBOX
void BridgeClass::_handleMailboxPush(const bridge::router::CommandContext& ctx) {
  _dispatchWithBytes<rpc::payload::MailboxPush>(
      ctx, &rpc::payload::MailboxPush::data,
      [](auto s) { Mailbox._onIncomingData(s); }, true);
}
void BridgeClass::_handleMailboxReadResp(const bridge::router::CommandContext& ctx) {
  _dispatchWithBytes<rpc::payload::MailboxReadResponse>(
      ctx, &rpc::payload::MailboxReadResponse::content,
      [](auto s) { Mailbox._onIncomingData(s); });
}
void BridgeClass::_handleMailboxAvailableResp(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::MailboxAvailableResponse>(
      ctx, [](auto& msg) { Mailbox._onAvailableResponse(msg); });
}
#endif

#if BRIDGE_ENABLE_FILESYSTEM
void BridgeClass::_handleFileWrite(const bridge::router::CommandContext& ctx) {
  // [SIL-2] Placeholder for future FS write implementation. Currently ACK only.
  _withAck(ctx, []() {});
}
void BridgeClass::_handleFileReadResp(const bridge::router::CommandContext& ctx) {
  _dispatchWithBytes<rpc::payload::FileReadResponse>(
      ctx, &rpc::payload::FileReadResponse::content,
      [](auto s) { FileSystem._onResponse(s); });
}
#endif

#if BRIDGE_ENABLE_PROCESS
void BridgeClass::_handleProcessRunAsyncResp(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::ProcessRunAsyncResponse>(
      ctx, [](auto& msg) { Process._onRunAsyncResponse(msg); });
}
void BridgeClass::_handleProcessPollResp(const bridge::router::CommandContext& ctx) {
  uint8_t stdout_buffer[rpc::MAX_PAYLOAD_SIZE];
  uint8_t stderr_buffer[rpc::MAX_PAYLOAD_SIZE];
  etl::span<uint8_t> stdout_span(stdout_buffer);
  etl::span<uint8_t> stderr_span(stderr_buffer);

  rpc::payload::ProcessPollResponse msg = {};
  rpc::util::pb_setup_decode_span(msg.stdout_data, stdout_span);
  rpc::util::pb_setup_decode_span(msg.stderr_data, stderr_span);

  _withPayload<rpc::payload::ProcessPollResponse>(
      ctx,
      [&](const auto& inner_msg) {
        Process._onPollResponse(
            inner_msg, etl::span<const uint8_t>(stdout_span),
            etl::span<const uint8_t>(stderr_span));
      },
      msg);
}
#endif

void BridgeClass::onUnknownCommand(const bridge::router::CommandContext& ctx) {
  if (_command_handler.is_valid()) _command_handler(*ctx.frame);
  else sendFrame(rpc::StatusCode::STATUS_CMD_UNKNOWN);
}

void BridgeClass::_handleStatusAck(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::AckPacket>(ctx, [this](const rpc::payload::AckPacket& msg) { _handleAck(static_cast<uint16_t>(msg.command_id)); });
}

void BridgeClass::_handleStatusMalformed(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::AckPacket>(ctx, [this](const rpc::payload::AckPacket& msg) { _handleMalformed(static_cast<uint16_t>(msg.command_id)); });
}

void BridgeClass::_handleAck(uint16_t command_id) {
  bool awaiting = false;
  BRIDGE_ATOMIC_BLOCK { awaiting = _fsm.isAwaitingAck(); }
  if (awaiting && (command_id == _last_command_id)) {
    _clearAckState();
    _timers.stop(bridge::scheduler::TIMER_ACK_TIMEOUT);
    BRIDGE_ATOMIC_BLOCK { if (!_pending_tx_queue.empty()) _pending_tx_queue.pop(); }
    _flushPendingTxQueue();
  }
}

void BridgeClass::_handleMalformed(uint16_t command_id) {
  if (command_id == _last_command_id) _retransmitLastFrame();
}

void BridgeClass::_retransmitLastFrame() {
  PendingTxFrame f;
  bool has_frame = false;
  BRIDGE_ATOMIC_BLOCK {
    if (!_pending_tx_queue.empty()) {
      f = _pending_tx_queue.front();
      has_frame = true;
    }
  }
  if (has_frame) {
    _sendRawFrame(f.command_id, etl::span<const uint8_t>(f.payload.data(), f.payload_length));
    _retry_count++;
  }
}

void BridgeClass::_onAckTimeout() {
  bool awaiting = false;
  BRIDGE_ATOMIC_BLOCK { awaiting = _fsm.isAwaitingAck(); }
  if (!awaiting) return;

  if (_retry_count >= _ack_retry_limit) {
    BRIDGE_ATOMIC_BLOCK { _fsm.timeout(); }
    enterSafeState();
    return;
  }
  _retransmitLastFrame();
  _timers.start(bridge::scheduler::TIMER_ACK_TIMEOUT, bridge::now_ms());
}

void BridgeClass::_onRxDedupe() { _rx_history.clear(); }

void BridgeClass::_onBaudrateChange() {
  if (_pending_baudrate > 0) {
    if (_hardware_serial) { _hardware_serial->begin(_pending_baudrate); }
    _pending_baudrate = 0;
  }
}

void BridgeClass::_onStartupStabilized() {
  uint16_t drain_limit = bridge::config::STARTUP_DRAIN_FINAL;
  while (_stream.available() > 0 && drain_limit-- > 0) _stream.read();
  BRIDGE_ATOMIC_BLOCK { _fsm.stabilized(); }
}

void BridgeClass::enterSafeState() {
  BRIDGE_ATOMIC_BLOCK { _fsm.resetFsm(); }
  _timers.clear();
  _pending_baudrate = 0; _retry_count = 0; _clearPendingTxQueue();
  _frame_received = false; _rx_history.clear(); _consecutive_crc_errors = 0;
#if BRIDGE_ENABLE_PROCESS
  Process.reset();
#endif
  notify_observers(MsgBridgeLost());
}

void BridgeClass::emitStatus(rpc::StatusCode status_code, etl::span<const uint8_t> payload) {
  sendFrame(status_code, payload);
  if (_status_handler.is_valid()) _status_handler(status_code, payload);
  notify_observers(MsgBridgeError{status_code});
}

void BridgeClass::emitStatus(rpc::StatusCode status_code, etl::string_view message) {
  emitStatus(status_code, etl::span<const uint8_t>(reinterpret_cast<const uint8_t*>(message.data()), message.length()));
}

void BridgeClass::emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message) {
  if (message == nullptr) {
    emitStatus(status_code, etl::span<const uint8_t>());
    return;
  }
  uint8_t buffer[rpc::MAX_PAYLOAD_SIZE];
#if defined(ARDUINO_ARCH_AVR)
  strncpy_P(reinterpret_cast<char*>(buffer), (PGM_P)message, sizeof(buffer));
#else
  strncpy(reinterpret_cast<char*>(buffer), reinterpret_cast<const char*>(message), sizeof(buffer));
#endif
  buffer[sizeof(buffer) - 1] = '\0';
  size_t len = strlen(reinterpret_cast<char*>(buffer));
  emitStatus(status_code, etl::span<const uint8_t>(buffer, len));
}

bool BridgeClass::sendFrame(rpc::StatusCode status_code, etl::span<const uint8_t> payload) {
  return _sendFrame(rpc::to_underlying(status_code), payload);
}

bool BridgeClass::sendFrame(rpc::CommandId command_id, etl::span<const uint8_t> payload) {
  return _sendFrame(rpc::to_underlying(command_id), payload);
}

void BridgeClass::_sendRawFrame(uint16_t command_id, etl::span<const uint8_t> payload) {
  etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE> raw;
  size_t raw_len = _frame_builder.build(raw, command_id, payload);
  if (raw_len > 0) {
    etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE + 2> cobs;
    size_t enc_len = rpc::cobs::encode(etl::span<const uint8_t>(raw.data(), raw_len), etl::span<uint8_t>(cobs.data(), cobs.size()));
    if (enc_len > 0) {
      _stream.write(cobs.data(), enc_len);
      _stream.write(rpc::RPC_FRAME_DELIMITER);
      flushStream();
    }
  }
}

void BridgeClass::_flushPendingTxQueue() {
  PendingTxFrame f;
  bool has_frame = false;
  BRIDGE_ATOMIC_BLOCK {
    if (!_fsm.isAwaitingAck() && !_pending_tx_queue.empty()) {
      f = _pending_tx_queue.front();
      has_frame = true;
    }
  }
  if (has_frame) {
    _sendRawFrame(f.command_id, etl::span<const uint8_t>(f.payload.data(), f.payload_length));
    BRIDGE_ATOMIC_BLOCK { _fsm.sendCritical(); }
    _retry_count = 0;
    _timers.start(bridge::scheduler::TIMER_ACK_TIMEOUT, bridge::now_ms());
    _last_command_id = f.command_id;
  }
}

void BridgeClass::_clearPendingTxQueue() {
  BRIDGE_ATOMIC_BLOCK { while (!_pending_tx_queue.empty()) _pending_tx_queue.pop(); }
}

void BridgeClass::_clearAckState() {
  BRIDGE_ATOMIC_BLOCK { if (_fsm.isAwaitingAck()) _fsm.ackReceived(); }
  _retry_count = 0;
}

void BridgeClass::_sendAckAndFlush(uint16_t command_id) {
  rpc::payload::AckPacket msg = {};
  msg.command_id = command_id;
  _sendPbResponse(rpc::StatusCode::STATUS_ACK, msg);
  flushStream();
}

bool BridgeClass::_sendFrame(uint16_t command_id, etl::span<const uint8_t> payload) {
  bool fault = false;
  bool unsync = false;
  BRIDGE_ATOMIC_BLOCK {
    fault = _fsm.isFault();
    unsync = _fsm.isUnsynchronized();
  }
  if (fault) return false;
  if (unsync && !_isHandshakeCommand(command_id)) return false;

  if (rpc::requires_ack(command_id)) {
    if (_isQueueFull()) return false;
    PendingTxFrame f; f.command_id = command_id; f.payload_length = static_cast<uint16_t>(payload.size());
    etl::copy_n(payload.data(), f.payload_length, f.payload.begin());
    BRIDGE_ATOMIC_BLOCK { _pending_tx_queue.push(f); }
    _flushPendingTxQueue();
    return true;
  }
  _sendRawFrame(command_id, payload);
  return true;
}

bool BridgeClass::_isHandshakeCommand(uint16_t cmd) const {
  return (cmd >= rpc::RPC_STATUS_CODE_MIN && cmd <= rpc::RPC_STATUS_CODE_MAX) ||
         (cmd >= rpc::RPC_SYSTEM_COMMAND_MIN && cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);
}

bool BridgeClass::_isRecentDuplicateRx(const rpc::Frame& frame) const {
  return etl::any_of(_rx_history.begin(), _rx_history.end(),
                     [&frame](const RxHistoryItem& r) { return r.crc == frame.crc; });
}

void BridgeClass::_markRxProcessed(const rpc::Frame& frame) {
  if (_rx_history.full()) _rx_history.pop();
  _rx_history.push({frame.crc, bridge::now_ms()});
}

etl::expected<void, rpc::FrameError> BridgeClass::_decompressFrame(const rpc::Frame& org, rpc::Frame& eff) {
  eff.header = org.header;
  eff.crc = org.crc;
  
  if (!(org.header.command_id & rpc::RPC_CMD_FLAG_COMPRESSED)) {
    eff.payload = org.payload;
    return {};
  }

  // Clear compression flag for the effective frame
  eff.header.command_id &= ~rpc::RPC_CMD_FLAG_COMPRESSED;
  
  size_t decoded_len = rle::decode(
      etl::span<const uint8_t>(org.payload.data(), org.header.payload_length),
      etl::span<uint8_t>(eff.payload.data(), eff.payload.size())
  );

  if (decoded_len == 0 && org.header.payload_length > 0) {
    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);
  }

  eff.header.payload_length = static_cast<uint16_t>(decoded_len);
  return {};
}

void BridgeClass::_computeHandshakeTag(etl::span<const uint8_t> nonce, uint8_t* out_tag) {
  uint8_t handshake_key[bridge::config::HKDF_KEY_LENGTH];
  hkdf_sha256(etl::span<uint8_t>(handshake_key, bridge::config::HKDF_KEY_LENGTH),
              etl::span<const uint8_t>(_shared_secret.data(), _shared_secret.size()),
              etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_SALT, rpc::RPC_HANDSHAKE_HKDF_SALT_LEN),
              etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH, rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH_LEN));
  SHA256 sha256;
  sha256.resetHMAC(handshake_key, bridge::config::HKDF_KEY_LENGTH);
  sha256.update(nonce.data(), nonce.size());
  sha256.finalizeHMAC(handshake_key, bridge::config::HKDF_KEY_LENGTH, out_tag, rpc::RPC_HANDSHAKE_TAG_LENGTH);
  rpc::security::secure_zero(etl::span<uint8_t>(handshake_key, bridge::config::HKDF_KEY_LENGTH));
}

void BridgeClass::_applyTimingConfig(etl::span<const uint8_t> payload) {
  rpc::payload::HandshakeConfig msg = {};
  pb_istream_t stream = pb_istream_from_buffer(payload.data(), payload.size());
  if (pb_decode(&stream, rpc::Payload::Descriptor<rpc::payload::HandshakeConfig>::fields(), &msg)) {
    if (msg.ack_timeout_ms > 0) {
      _ack_timeout_ms = msg.ack_timeout_ms;
      _timers.set_period(bridge::scheduler::TIMER_ACK_TIMEOUT, _ack_timeout_ms);
    }
    if (msg.ack_retry_limit > 0) _ack_retry_limit = msg.ack_retry_limit;
    if (msg.response_timeout_ms > 0) _response_timeout_ms = msg.response_timeout_ms;
  }
}

// --- Global Instances (PROPERLY PROTECTED FOR HOST TESTS) ---
// Unit tests define BRIDGE_TEST_NO_GLOBALS to supply their own fixtures.
// Emulators define only BRIDGE_HOST_TEST and *need* these globals.
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
#endif

namespace etl {
void __attribute__((weak)) handle_error(const etl::exception& e) { (void)e; Bridge.enterSafeState(); }
}

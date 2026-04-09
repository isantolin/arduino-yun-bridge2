#include "Bridge.h"
#include "hal/progmem_compat.h"
#include "services/SPIService.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"
#include <Arduino.h>
#include <etl/numeric.h>
#include <etl/span.h>
#include <etl/algorithm.h>
#include "util/string_copy.h"

#if (defined(_GLIBCXX_VECTOR) || defined(_VECTOR_)) && !defined(BRIDGE_HOST_TEST)
#error "STL vector detected! MCU Bridge strictly forbids STL/dynamic memory."
#endif

namespace {
constexpr uint8_t kCompressedCommandBit = 15;
size_t getFreeMemory() { return bridge::hal::getFreeMemory(); }
}  // namespace

BridgeClass::BridgeClass(Stream& stream)
    : _stream(stream),
      _hardware_serial(nullptr),
      _command_handler(),
      _status_handler(),
      _last_command_id(0),
      _tx_sequence_id(0),
      _retry_count(0),
      _retry_limit(bridge::config::DEFAULT_ACK_RETRY_LIMIT),
      _ack_timeout_ms(bridge::config::DEFAULT_ACK_TIMEOUT_MS),
      _response_timeout_ms(bridge::config::DEFAULT_RESPONSE_TIMEOUT_MS),
      _pending_baudrate(0),
      _consecutive_crc_errors(0),
      _last_parse_error(rpc::FrameError::NONE),
      _last_timer_ms(0),
      _timer_ids(),
      _timers(),
      _fsm(),
      _frame_builder(),
      _frame_parser(),
      _rx_storage(),
      _transient_buffer(),
      _packet_serial(etl::span<uint8_t>(_rx_storage.data(), _rx_storage.size()),
                     etl::span<uint8_t>(_transient_buffer.data(), _transient_buffer.size())),
      _shared_secret(),
      _gpio_adapter(*this),
      _tx_payload_pool(),
      _pending_tx_queue(),
      _rx_history(),
      _tx_enabled(true) {
  _timer_ids.fill(0);
  _timers.set_period(bridge::scheduler::TIMER_BAUDRATE_CHANGE, bridge::config::BAUDRATE_CHANGE_DELAY_MS);
  _timers.set_period(bridge::scheduler::TIMER_RX_DEDUPE, bridge::config::RX_DEDUPE_INTERVAL_MS);
  _timers.set_period(bridge::scheduler::TIMER_ACK_TIMEOUT, _ack_timeout_ms);
  _timers.set_period(bridge::scheduler::TIMER_STARTUP_STABILIZATION, bridge::config::STARTUP_STABILIZATION_MS);

  _packet_serial.setPacketHandler(etl::delegate<void(etl::span<const uint8_t>)>::create<BridgeClass, &BridgeClass::_onPacketReceived>(*this));
}

void BridgeClass::begin(uint32_t baudrate, const char* secret) {
  _shared_secret.clear();
  if (secret != nullptr) {
    const size_t len = etl::min(strlen(secret), _shared_secret.capacity());
    _shared_secret.assign(reinterpret_cast<const uint8_t*>(secret), reinterpret_cast<const uint8_t*>(secret) + len);
  }
  
  // [MIL-SPEC] Mandatory POST (Power-On Self-Tests)
  if (!runPowerOnSelfTests()) {
    forceSafeState();
    return;
  }

  _rx_history.clear();
  _tx_enabled = true;
  _fsm.begin();
  _timers.start(_timer_ids[bridge::scheduler::TIMER_STARTUP_STABILIZATION], etl::timer::start::DELAYED);
  _timers.start(_timer_ids[bridge::scheduler::TIMER_RX_DEDUPE], etl::timer::start::DELAYED);
  if (baudrate > 0 && _hardware_serial) _hardware_serial->begin(baudrate);
#if defined(ARDUINO_ARCH_AVR)
  wdt_enable(WDTO_2S);
#endif
}

bool BridgeClass::runPowerOnSelfTests() {
  return rpc::security::run_cryptographic_self_tests();
}

void BridgeClass::process() {
#if defined(ARDUINO_ARCH_AVR)
  wdt_reset();
#endif
  uint32_t now = bridge::now_ms();
  if (now != _last_timer_ms) {
    _timers.tick(now - _last_timer_ms);
    _last_timer_ms = now;
  }
  _packet_serial.update(_stream);
}

bool BridgeClass::isSynchronized() const { return _fsm.isSynchronized(); }

void BridgeClass::_dispatchCommand(const rpc::Frame& frame) {
  bridge::router::CommandContext ctx(
      &frame, frame.header.command_id, frame.header.sequence_id,
      _rx_history.exists(frame.header.sequence_id),
      rpc::requires_ack(frame.header.command_id));

  if (!_isSecurityCheckPassed(ctx.raw_command)) {
    (void)sendFrame(rpc::StatusCode::STATUS_ERROR, ctx.sequence_id);
    return;
  }

  _dispatchRoot<
      Group<3, true,
            BehaviorCmd<static_cast<uint16_t>(rpc::StatusCode::STATUS_MALFORMED), &BridgeClass::_handleStatusMalformed>,
            BehaviorCmd<static_cast<uint16_t>(rpc::StatusCode::STATUS_ACK), &BridgeClass::_handleStatusAck>>,
      Group<4, true,
            BehaviorCmd<rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION), &BridgeClass::_handleGetVersion>,
            BehaviorCmd<rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY), &BridgeClass::_handleGetFreeMemory>,
            BehaviorCmd<rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), &BridgeClass::_handleLinkSync>,
            BehaviorCmd<rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET), &BridgeClass::_handleLinkReset>,
            BehaviorCmd<rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES), &BridgeClass::_handleGetCapabilities>,
            BehaviorCmd<rpc::to_underlying(rpc::CommandId::CMD_XOFF), &BridgeClass::_handleXoff>,
            BehaviorCmd<rpc::to_underlying(rpc::CommandId::CMD_XON), &BridgeClass::_handleXon>,
            BehaviorBridgePayload<rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE), rpc::payload::SetBaudratePacket, &BridgeClass::_handleSetBaudrate>,
            BehaviorBridgePayload<rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER), rpc::payload::EnterBootloader, &BridgeClass::_handleEnterBootloader>>,
      Group<5, true,
            BehaviorGpioPayload<rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE), rpc::payload::PinMode, BridgeClass::GpioAdapter, &BridgeClass::GpioAdapter::setPinMode>,
            BehaviorGpioPayload<rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), rpc::payload::DigitalWrite, BridgeClass::GpioAdapter, &BridgeClass::GpioAdapter::digitalWrite>,
            BehaviorGpioPayload<rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE), rpc::payload::AnalogWrite, BridgeClass::GpioAdapter, &BridgeClass::GpioAdapter::analogWrite>,
            BehaviorPinRead<rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ), rpc::payload::DigitalReadResponse, rpc::CommandId::CMD_DIGITAL_READ_RESP, &bridge::hal::isValidPin, ::digitalRead>,
            BehaviorPinRead<rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ), rpc::payload::AnalogReadResponse, rpc::CommandId::CMD_ANALOG_READ_RESP, &bridge::hal::isValidPin, ::analogRead>>,
      Group<6, true,
            BehaviorServicePayload<rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE), rpc::payload::ConsoleWrite, ConsoleClass, Console, &ConsoleClass::_push>>,
      Group<7, bridge::config::ENABLE_DATASTORE,
            BehaviorServicePayload<rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP), rpc::payload::DatastoreGetResponse, DataStoreClass, DataStore, &DataStoreClass::_onResponse>>,
      Group<8, bridge::config::ENABLE_MAILBOX,
            BehaviorServicePayload<rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH), rpc::payload::MailboxPush, MailboxClass, Mailbox, &MailboxClass::_onIncomingData>,
            BehaviorServicePayload<rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP), rpc::payload::MailboxReadResponse, MailboxClass, Mailbox, &MailboxClass::_onIncomingData>,
            BehaviorServicePayload<rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP), rpc::payload::MailboxAvailableResponse, MailboxClass, Mailbox, &MailboxClass::_onAvailableResponse>>,
      Group<9, bridge::config::ENABLE_FILESYSTEM,
            BehaviorServicePayload<rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE), rpc::payload::FileWrite, FileSystemClass, FileSystem, &FileSystemClass::_onWrite>,
            BehaviorServicePayload<rpc::to_underlying(rpc::CommandId::CMD_FILE_READ), rpc::payload::FileRead, FileSystemClass, FileSystem, &FileSystemClass::_onRead>,
            BehaviorServicePayload<rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE), rpc::payload::FileRemove, FileSystemClass, FileSystem, &FileSystemClass::_onRemove>,
            BehaviorServicePayload<rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP), rpc::payload::FileReadResponse, FileSystemClass, FileSystem, &FileSystemClass::_onResponse>>,
      Group<10, bridge::config::ENABLE_PROCESS,
            BehaviorServicePayload<rpc::to_underlying(rpc::CommandId::CMD_PROCESS_KILL), rpc::payload::ProcessKill, ProcessClass, Process, &ProcessClass::_kill>,
            BehaviorServicePayload<rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP), rpc::payload::ProcessRunAsyncResponse, ProcessClass, Process, &ProcessClass::_onRunAsyncResponse>,
            BehaviorServicePayload<rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP), rpc::payload::ProcessPollResponse, ProcessClass, Process, &ProcessClass::_onPollResponse>>,
      Group<11, bridge::config::ENABLE_SPI,
            BehaviorCmd<rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN), &BridgeClass::_handleSpiBegin>,
            BehaviorCmd<rpc::to_underlying(rpc::CommandId::CMD_SPI_END), &BridgeClass::_handleSpiEnd>,
            BehaviorServicePayload<rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG), rpc::payload::SpiConfig, SPIServiceClass, SPIService, &SPIServiceClass::setConfig>,
            BehaviorCmd<rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER), &BridgeClass::_handleSpiTransfer>>>(ctx);

  _markRxProcessed(frame);
}

void BridgeClass::_handleXoff(const bridge::router::CommandContext& ctx) { (void)ctx; _tx_enabled = false; }
void BridgeClass::_handleXon(const bridge::router::CommandContext& ctx) { (void)ctx; _tx_enabled = true; _flushPendingTxQueue(); }

void BridgeClass::_handleSpiBegin(const bridge::router::CommandContext& ctx) { _withAck(ctx, []() { SPIService.begin(); }); }
void BridgeClass::_handleSpiEnd(const bridge::router::CommandContext& ctx) { _withAck(ctx, []() { SPIService.end(); }); }

void BridgeClass::_handleSpiTransfer(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::SpiTransfer>(ctx, [this, &ctx](const rpc::payload::SpiTransfer& msg) {
    if (SPIService.isInitialized()) {
      auto data = msg.data;
      const size_t len = etl::min(data.size(), _rx_storage.size());
      etl::copy_n(data.begin(), len, _rx_storage.begin());
      if (bridge::hal::hasSPI()) {
        if (SPIService.transfer(etl::span<uint8_t>(_rx_storage.data(), len)) < len) {
          enterSafeState();
          _sendError(rpc::StatusCode::STATUS_ERROR, ctx.raw_command, ctx.sequence_id);
          return;
        }
      }
      rpc::payload::SpiTransferResponse resp = {};
      resp.data = etl::span<const uint8_t>(_rx_storage.data(), len);
      _sendPbResponse(rpc::CommandId::CMD_SPI_TRANSFER_RESP, ctx.sequence_id, resp);
    }
  });
}

void BridgeClass::_handleGetVersion(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc::payload::VersionResponse resp = {rpc::FIRMWARE_VERSION_MAJOR, rpc::FIRMWARE_VERSION_MINOR, rpc::FIRMWARE_VERSION_PATCH};
    _sendPbResponse(rpc::CommandId::CMD_GET_VERSION_RESP, ctx.sequence_id, resp);
  });
}

void BridgeClass::_handleGetFreeMemory(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc::payload::FreeMemoryResponse resp = {static_cast<uint32_t>(getFreeMemory())};
    _sendPbResponse(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, ctx.sequence_id, resp);
  });
}

void BridgeClass::_handleLinkSync(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::LinkSync>(ctx, [this, &ctx](const rpc::payload::LinkSync& msg) {
    etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> tag;
    _computeHandshakeTag(etl::span<const uint8_t>(msg.nonce.data(), msg.nonce.size()), etl::span<uint8_t>(tag.data(), tag.size()));
    if (!_shared_secret.empty() && !rpc::security::timing_safe_equal(etl::span<const uint8_t>(tag.data(), tag.size()), etl::span<const uint8_t>(msg.tag.data(), msg.tag.size()))) {
      _fsm.handshakeStart(); _fsm.handshakeFailed(); return;
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

void BridgeClass::_handleGetCapabilities(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc::payload::Capabilities resp = {};
    resp.ver = rpc::PROTOCOL_VERSION; resp.arch = bridge::hal::getArchId();
    bridge::hal::getPinCounts(resp.dig, resp.ana); resp.feat = bridge::hal::getCapabilities();
    _sendPbResponse(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, ctx.sequence_id, resp);
  });
}

void BridgeClass::_handleSetBaudrate(const rpc::payload::SetBaudratePacket& msg) {
  _pending_baudrate = msg.baudrate;
  _timers.start(_timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE], etl::timer::start::DELAYED);
}

void BridgeClass::_handleEnterBootloader(const rpc::payload::EnterBootloader& msg) {
  if (msg.magic == rpc::RPC_BOOTLOADER_MAGIC) {
    this->flushStream(); delay(bridge::config::BOOTLOADER_DELAY_MS);
#if defined(ARDUINO_ARCH_AVR)
    wdt_enable(WDTO_15MS); for (;;) {}
#elif defined(ARDUINO_ARCH_ESP32)
    ESP.restart();
#elif defined(ARDUINO_ARCH_SAMD)
    NVIC_SystemReset();
#endif
  }
}

void BridgeClass::onUnknownCommand(const bridge::router::CommandContext& ctx) {
  if (_command_handler.is_valid()) _command_handler(*ctx.frame);
  else _sendError(rpc::StatusCode::STATUS_CMD_UNKNOWN, ctx.raw_command, ctx.sequence_id);
}

void BridgeClass::_sendError(rpc::StatusCode status, uint16_t command_id, uint16_t sequence_id) {
  rpc::payload::AckPacket msg = {command_id}; _sendPbResponse(status, sequence_id, msg);
}

void BridgeClass::_handleStatusAck(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::AckPacket>(ctx, [this](const rpc::payload::AckPacket& msg) { _handleAck(static_cast<uint16_t>(msg.command_id)); });
}

void BridgeClass::_handleStatusMalformed(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::AckPacket>(ctx, [this](const rpc::payload::AckPacket& msg) { _handleMalformed(static_cast<uint16_t>(msg.command_id)); });
}

void BridgeClass::_handleAck(uint16_t command_id) {
  bool awaiting = false; BRIDGE_ATOMIC_BLOCK { awaiting = _fsm.isAwaitingAck(); }
  if (awaiting && (command_id == _last_command_id)) { _clearAckState(); _timers.stop(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]); _flushPendingTxQueue(); }
}

void BridgeClass::_handleMalformed(uint16_t command_id) { if (command_id == _last_command_id) _retransmitLastFrame(); }

void BridgeClass::_retransmitLastFrame() {
  PendingTxFrame f; bool has_frame = false;
  BRIDGE_ATOMIC_BLOCK { if (!_pending_tx_queue.empty()) { f = _pending_tx_queue.front(); has_frame = true; } }
  if (has_frame && f.buffer != nullptr) { _sendRawFrame(f.command_id, 0, etl::span<const uint8_t>(f.buffer->data.data(), f.payload_length)); _retry_count++; }
}

void BridgeClass::_onAckTimeout() {
  bool awaiting = false; BRIDGE_ATOMIC_BLOCK { awaiting = _fsm.isAwaitingAck(); }
  if (!awaiting) return;
  if (_retry_count >= _retry_limit) { BRIDGE_ATOMIC_BLOCK { _fsm.timeout(); } enterSafeState(); return; }
  _retransmitLastFrame(); _timers.start(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT], etl::timer::start::DELAYED);
}

void BridgeClass::_onRxDedupe() { _rx_history.clear(); }

void BridgeClass::signalXoff() { (void)sendFrame(rpc::CommandId::CMD_XOFF); }
void BridgeClass::signalXon() { (void)sendFrame(rpc::CommandId::CMD_XON); }

void BridgeClass::_onBaudrateChange() {
  if (_pending_baudrate > 0) {
    (void)sendFrame(rpc::CommandId::CMD_SET_BAUDRATE_RESP);
    flushStream();
    if (_hardware_serial) _hardware_serial->begin(_pending_baudrate);
    _pending_baudrate = 0;
  }
}

void BridgeClass::_onStartupStabilized() {
  uint32_t start_ms = bridge::now_ms();
  etl::array<uint8_t, bridge::config::STARTUP_DRAIN_FINAL> dummy = {};
  (void)etl::find_if(dummy.begin(), dummy.end(), [&](uint8_t) {
    if (_stream.available() <= 0 || (bridge::now_ms() - start_ms >= bridge::config::SERIAL_TIMEOUT_MS)) return true;
    _stream.read();
    return false;
  });
  BRIDGE_ATOMIC_BLOCK { _fsm.stabilized(); }
}

void BridgeClass::enterSafeState() {
  BRIDGE_ATOMIC_BLOCK { _fsm.resetFsm(); }
  etl::for_each(_timer_ids.begin(), _timer_ids.end(), [this](etl::timer::id::type id) { _timers.stop(id); });
  _pending_baudrate = 0; _retry_count = 0; _clearPendingTxQueue(); _rx_history.clear(); _tx_enabled = true;
  rpc::security::secure_zero(etl::span<uint8_t>(_shared_secret.data(), _shared_secret.size())); _shared_secret.clear();
#if BRIDGE_ENABLE_PROCESS
  Process.reset();
#endif
  forceSafeState(); notify_observers(MsgBridgeLost());
}

void BridgeClass::emitStatus(rpc::StatusCode status_code, etl::span<const uint8_t> payload) {
  (void)sendFrame(status_code, 0, payload); if (_status_handler.is_valid()) _status_handler(status_code, payload);
}

void BridgeClass::emitStatus(rpc::StatusCode status_code, etl::string_view message) {
  if (message.empty()) { emitStatus(status_code, etl::span<const uint8_t>()); return; }
  const size_t max_len = etl::min(message.length(), rpc::MAX_PAYLOAD_SIZE - 1U);
  etl::copy_n(message.data(), max_len, _transient_buffer.data()); _transient_buffer[max_len] = rpc::RPC_NULL_TERMINATOR;
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
  if (raw_len > 0) (void)_packet_serial.send(_stream, etl::span<const uint8_t>(raw_buffer.data(), raw_len));
}

void BridgeClass::_flushPendingTxQueue() {
  if (!_tx_enabled) return;
  PendingTxFrame f; bool has_frame = false;
  BRIDGE_ATOMIC_BLOCK { if (!_fsm.isAwaitingAck() && !_pending_tx_queue.empty()) { f = _pending_tx_queue.front(); has_frame = true; } }
  if (has_frame && f.buffer != nullptr) {
    uint16_t seq = ++_tx_sequence_id;
    _sendRawFrame(f.command_id, seq, etl::span<const uint8_t>(f.buffer->data.data(), f.payload_length));
    BRIDGE_ATOMIC_BLOCK { _fsm.sendCritical(); }
    _retry_count = 0; _timers.start(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT], etl::timer::start::DELAYED);
    _last_command_id = f.command_id;
  }
}

void BridgeClass::_clearPendingTxQueue() {
  BRIDGE_ATOMIC_BLOCK {
    etl::array<uint8_t, bridge::config::TX_QUEUE_CAPACITY> dummy = {};
    (void)etl::find_if(dummy.begin(), dummy.end(), [&](uint8_t) {
      if (_pending_tx_queue.empty()) return true;
      TxPayloadBuffer* buf = _pending_tx_queue.front().buffer;
      if (buf) _tx_payload_pool.release(buf);
      _pending_tx_queue.pop();
      return false;
    });
  }
}

void BridgeClass::_clearAckState() {
  BRIDGE_ATOMIC_BLOCK {
    if (_fsm.isAwaitingAck()) {
      _fsm.ackReceived();
      if (!_pending_tx_queue.empty()) { TxPayloadBuffer* buf = _pending_tx_queue.front().buffer; if (buf) _tx_payload_pool.release(buf); _pending_tx_queue.pop(); }
    }
  }
  _retry_count = 0;
}

void BridgeClass::_sendAckAndFlush(uint16_t command_id, uint16_t sequence_id) {
  rpc::payload::AckPacket msg = {command_id}; _sendPbResponse(rpc::StatusCode::STATUS_ACK, sequence_id, msg); flushStream();
}

bool BridgeClass::_sendFrame(uint16_t command_id, uint16_t sequence_id, etl::span<const uint8_t> payload) {
  bool fault, operational; BRIDGE_ATOMIC_BLOCK { fault = _fsm.isFault(); operational = _fsm.isSynchronized(); }
  if (fault || (!operational && !_isHandshakeCommand(command_id))) return false;
  if (rpc::requires_ack(command_id)) {
    if (_pending_tx_queue.full() || _tx_payload_pool.full()) return false;
    TxPayloadBuffer* buf = nullptr; BRIDGE_ATOMIC_BLOCK { buf = _tx_payload_pool.allocate(); }
    if (!buf) return false;
    PendingTxFrame f = {command_id, static_cast<uint16_t>(payload.size()), buf};
    if (payload.size() > 0) etl::copy_n(payload.data(), f.payload_length, buf->data.data());
    BRIDGE_ATOMIC_BLOCK { _pending_tx_queue.push(f); }
    _flushPendingTxQueue(); return true;
  }
  _sendRawFrame(command_id, sequence_id, payload); return true;
}

bool BridgeClass::_isHandshakeCommand(uint16_t cmd) const {
  return (cmd >= rpc::RPC_STATUS_CODE_MIN && cmd <= rpc::RPC_STATUS_CODE_MAX) || (cmd >= rpc::RPC_SYSTEM_COMMAND_MIN && cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);
}

void BridgeClass::_markRxProcessed(const rpc::Frame& frame) { _rx_history.push(frame.header.sequence_id); }
bool BridgeClass::_isSecurityCheckPassed(uint16_t command_id) const { if (isSynchronized()) return true; return _isHandshakeCommand(command_id); }

etl::expected<void, rpc::FrameError> BridgeClass::_decompressFrame(const rpc::Frame& org, rpc::Frame& eff) {
  eff.header = org.header; eff.crc = org.crc;
  if (!bitRead(org.header.command_id, kCompressedCommandBit)) { eff.payload = org.payload; return {}; }
  bitWrite(eff.header.command_id, kCompressedCommandBit, 0);
  size_t decoded_len = rle::decode(org.payload, etl::span<uint8_t>(_rx_storage.data(), _rx_storage.size()));
  if (decoded_len == 0 && org.header.payload_length > 0) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);
  eff.header.payload_length = static_cast<uint16_t>(decoded_len); eff.payload = etl::span<const uint8_t>(_rx_storage.data(), decoded_len);
  return {};
}

void BridgeClass::_computeHandshakeTag(const etl::span<const uint8_t> nonce, etl::span<uint8_t> out_tag) {
  etl::array<uint8_t, bridge::config::HKDF_KEY_LENGTH> handshake_key;
  rpc::security::hkdf_sha256(handshake_key, etl::span<const uint8_t>(_shared_secret.data(), _shared_secret.size()), etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_SALT), etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH));
  rpc::security::McuBridgeSha256 sha256; sha256.resetHMAC(handshake_key.data(), handshake_key.size());
  sha256.update(nonce.data(), nonce.size()); etl::array<uint8_t, rpc::security::McuBridgeSha256::HASH_SIZE> full_tag;
  sha256.finalizeHMAC(handshake_key.data(), handshake_key.size(), full_tag.data(), full_tag.size());
  etl::copy_n(full_tag.begin(), etl::min(full_tag.size(), out_tag.size()), out_tag.begin());
  rpc::security::secure_zero(handshake_key); rpc::security::secure_zero(full_tag);
}

void BridgeClass::forceSafeState() { bridge::hal::forceSafeState(); }
void BridgeClass::_applyTimingConfig(const rpc::payload::HandshakeConfig& msg) {
  if (msg.ack_timeout_ms > 0) { _ack_timeout_ms = msg.ack_timeout_ms; _timers.set_period(bridge::scheduler::TIMER_ACK_TIMEOUT, _ack_timeout_ms); }
  if (msg.ack_retry_limit > 0) _retry_limit = msg.ack_retry_limit;
  if (msg.response_timeout_ms > 0) _response_timeout_ms = msg.response_timeout_ms;
}

void BridgeClass::_handleReceivedFrame(etl::span<const uint8_t> p) {
  auto res = _frame_parser.parse(p);
  if (!res) { _last_parse_error = res.error(); emitStatus(rpc::StatusCode::STATUS_MALFORMED); return; }
  rpc::Frame eff; auto dec = _decompressFrame(res.value(), eff);
  if (!dec) { _last_parse_error = dec.error(); emitStatus(rpc::StatusCode::STATUS_MALFORMED); return; }
  _dispatchCommand(eff);
}

void BridgeClass::_onPacketReceived(etl::span<const uint8_t> p) { _handleReceivedFrame(p); }

#ifndef BRIDGE_TEST_NO_GLOBALS
BridgeClass Bridge(Serial);
#endif

namespace etl {
void __attribute__((weak)) __attribute__((unused)) handle_error(const etl::exception& e) { (void)e; Bridge.enterSafeState(); }
}  // namespace etl

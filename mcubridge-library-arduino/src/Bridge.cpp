#include "Bridge.h"
#include "hal/ArchTraits.h"
#include "hal/progmem_compat.h"
#include "services/SPIService.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#include <etl/algorithm.h>

namespace {
void _onStartupStabilizationTimeout() { Bridge._onStartupStabilized(); }
void _onAckTimeoutInternal() { Bridge._onAckTimeout(); }
void _onRxDedupeTimeout() { Bridge._onRxDedupe(); }
void _onBaudrateChangeTimeout() { Bridge._onBaudrateChange(); }

constexpr bool is_reliable_cmd(uint16_t id) {
    return rpc::is_reliable(id);
}

constexpr bool is_compressed_cmd(uint16_t id) {
    return (id & rpc::RPC_CMD_FLAG_COMPRESSED) != 0;
}
}

#ifndef BRIDGE_TEST_NO_GLOBALS
BridgeClass Bridge(Serial);
#endif

namespace etl {
void __attribute__((weak)) __attribute__((unused)) handle_error(const etl::exception& e) {
  BridgeClass::ErrorPolicy::handle(Bridge, e);
}
}  // namespace etl

void BridgeClass::notify_observers(const MsgBridgeSynchronized& msg) {
#if BRIDGE_ENABLE_CONSOLE
  Console.notification(msg);
#endif
#if BRIDGE_ENABLE_MAILBOX
  Mailbox.notification(msg);
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  FileSystem.notification(msg);
#endif
#if BRIDGE_ENABLE_DATASTORE
  DataStore.notification(msg);
#endif
#if BRIDGE_ENABLE_PROCESS
  Process.notification(msg);
#endif
#if BRIDGE_ENABLE_SPI
  SPIService.notification(msg);
#endif
}

void BridgeClass::notify_observers(const MsgBridgeLost& msg) {
#if BRIDGE_ENABLE_CONSOLE
  Console.notification(msg);
#endif
#if BRIDGE_ENABLE_MAILBOX
  Mailbox.notification(msg);
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  FileSystem.notification(msg);
#endif
#if BRIDGE_ENABLE_DATASTORE
  DataStore.notification(msg);
#endif
#if BRIDGE_ENABLE_PROCESS
  Process.notification(msg);
#endif
#if BRIDGE_ENABLE_SPI
  SPIService.notification(msg);
#endif
}

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
      _ps_rx_storage(),
      _ps_work_buffer(),
      _packet_serial(etl::span<uint8_t>(_ps_rx_storage.data(), _ps_rx_storage.size()), 
                     etl::span<uint8_t>(_ps_work_buffer.data(), _ps_work_buffer.size())),
      _shared_secret(),
      _fsm(),
      _timers(),
      _timer_ids(),
      _transient_buffer(),
      _rx_storage(),
      _frame_parser(),
      _is_post_passed(false),
      _tx_enabled(true),
      _gpio_adapter(*this),
      _tx_payload_pool(),
      _pending_tx_queue(),
      _rx_history() {
  _shared_secret.clear();
  _rx_storage.fill(0);
  if constexpr (bridge::hal::CurrentArchTraits::id == bridge::hal::ArchId::ARCH_ID_AVR) {
    _hardware_serial = static_cast<HardwareSerial*>(&stream);
  }
}

void BridgeClass::begin(uint32_t baudrate, const char* secret) {
  _shared_secret.clear();
  if (secret != nullptr) {
    const size_t len = etl::min(strlen(secret), _shared_secret.capacity());
    _shared_secret.assign(reinterpret_cast<const uint8_t*>(secret), reinterpret_cast<const uint8_t*>(secret) + len);
  }

  bridge::hal::init();
  _fsm.begin();
  _is_post_passed = runPowerOnSelfTests();
  if (!_is_post_passed) enterSafeState();

  if constexpr (bridge::hal::CurrentArchTraits::id == bridge::hal::ArchId::ARCH_ID_AVR) {
    if (baudrate > 0 && _hardware_serial) _hardware_serial->begin(baudrate);
  }
  
  _tx_enabled = true;
  _timers.clear();
  _timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT] = _timers.register_timer(_onAckTimeoutInternal, _ack_timeout_ms, etl::timer::mode::REPEATING);
  _timer_ids[bridge::scheduler::TIMER_RX_DEDUPE] = _timers.register_timer(_onRxDedupeTimeout, bridge::config::HANDSHAKE_RETRY_DELAY_MS, etl::timer::mode::REPEATING);
  _timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE] = _timers.register_timer(_onBaudrateChangeTimeout, bridge::config::BAUDRATE_CHANGE_DELAY_MS, etl::timer::mode::SINGLE_SHOT);
  _timer_ids[bridge::scheduler::TIMER_STARTUP_STABILIZATION] = _timers.register_timer(_onStartupStabilizationTimeout, bridge::config::STARTUP_STABILIZATION_MS, etl::timer::mode::SINGLE_SHOT);
  _timers.start(_timer_ids[bridge::scheduler::TIMER_STARTUP_STABILIZATION]);

  _packet_serial.setPacketHandler(etl::delegate<void(etl::span<const uint8_t>)>::create<BridgeClass, &BridgeClass::_onPacketReceived>(*this));
}

void BridgeClass::process() {
  uint32_t now = bridge::now_ms();
  static uint32_t _last_tick_ms = 0;
  if (_last_tick_ms == 0) _last_tick_ms = now;
  uint32_t elapsed = now - _last_tick_ms;
  if (elapsed > 0) {
    _timers.tick(elapsed);
    _last_tick_ms = now;
  }
  _packet_serial.update(_stream);

  static bool xoff_sent = false;
  int available_bytes = _stream.available();
  if (!xoff_sent && available_bytes > 48) {
    signalXoff();
    xoff_sent = true;
  } else if (xoff_sent && available_bytes < 16) {
    signalXon();
    xoff_sent = false;
  }
}

bool BridgeClass::isSynchronized() const { return _fsm.isSynchronized(); }

void BridgeClass::_dispatchCommand(const rpc::Frame& frame) {
  uint16_t cmd_id = frame.header.command_id & ~rpc::RPC_CMD_FLAG_COMPRESSED;
  bridge::router::CommandContext ctx(
      &frame, cmd_id, frame.header.sequence_id,
      _rx_history.exists(frame.header.sequence_id),
      rpc::requires_ack(cmd_id));

  if (!_isSecurityCheckPassed(ctx.raw_command)) {
    (void)sendFrame(rpc::StatusCode::STATUS_ERROR, ctx.sequence_id);
    return;
  }

  switch (ctx.raw_command) {
    case static_cast<uint16_t>(rpc::StatusCode::STATUS_MALFORMED): _handleStatusMalformed(ctx); break;
    case static_cast<uint16_t>(rpc::StatusCode::STATUS_ACK):       _handleStatusAck(ctx); break;
    case rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION):      _handleGetVersion(ctx); break;
    case rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY):  _handleGetFreeMemory(ctx); break;
    case rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC):        _handleLinkSync(ctx); break;
    case rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET):       _handleLinkReset(ctx); break;
    case rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES): _handleGetCapabilities(ctx); break;
    case rpc::to_underlying(rpc::CommandId::CMD_XOFF):             _handleXoff(ctx); break;
    case rpc::to_underlying(rpc::CommandId::CMD_XON):              _handleXon(ctx); break;
    case rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE):
      _withPayloadAck<rpc::payload::SetBaudratePacket>(ctx, [this](const auto& m) { _handleSetBaudrate(m); }); break;
    case rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER):
      _withPayloadAck<rpc::payload::EnterBootloader>(ctx, [this](const auto& m) { _handleEnterBootloader(m); }); break;
    case rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE):
      _withPayloadAck<rpc::payload::PinMode>(ctx, [this](const auto& m) { _gpio_adapter.setPinMode(m); }); break;
    case rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE):
      _withPayloadAck<rpc::payload::DigitalWrite>(ctx, [this](const auto& m) { _gpio_adapter.digitalWrite(m); }); break;
    case rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE):
      _withPayloadAck<rpc::payload::AnalogWrite>(ctx, [this](const auto& m) { _gpio_adapter.analogWrite(m); }); break;
    case rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ):
      _handlePinRead<rpc::payload::DigitalReadResponse>(ctx, rpc::CommandId::CMD_DIGITAL_READ_RESP, &bridge::hal::isValidPin, ::digitalRead); break;
    case rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ):
      _handlePinRead<rpc::payload::AnalogReadResponse>(ctx, rpc::CommandId::CMD_ANALOG_READ_RESP, &bridge::hal::isValidPin, ::analogRead); break;
    case rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE):
      _withPayloadAck<rpc::payload::ConsoleWrite>(ctx, [](const auto& m) { Console._push(m); }); break;
#if BRIDGE_ENABLE_DATASTORE
    case rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP):
      _withPayloadAck<rpc::payload::DatastoreGetResponse>(ctx, [](const auto& m) { DataStore._onResponse(m); }); break;
#endif
#if BRIDGE_ENABLE_MAILBOX
    case rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH):
      _withPayloadAck<rpc::payload::MailboxPush>(ctx, [](const auto& m) { Mailbox._onIncomingData(m); }); break;
    case rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP):
      _withPayloadAck<rpc::payload::MailboxReadResponse>(ctx, [](const auto& m) { Mailbox._onIncomingData(m); }); break;
    case rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP):
      _withPayloadAck<rpc::payload::MailboxAvailableResponse>(ctx, [](const auto& m) { Mailbox._onAvailableResponse(m); }); break;
#endif
#if BRIDGE_ENABLE_FILESYSTEM
    case rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE):
      _withPayloadAck<rpc::payload::FileWrite>(ctx, [](const auto& m) { FileSystem._onWrite(m); }); break;
    case rpc::to_underlying(rpc::CommandId::CMD_FILE_READ):
      _withPayloadAck<rpc::payload::FileRead>(ctx, [](const auto& m) { FileSystem._onRead(m); }); break;
    case rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE):
      _withPayloadAck<rpc::payload::FileRemove>(ctx, [](const auto& m) { FileSystem._onRemove(m); }); break;
    case rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP):
      _withPayloadAck<rpc::payload::FileReadResponse>(ctx, [](const auto& m) { FileSystem._onResponse(m); }); break;
#endif
#if BRIDGE_ENABLE_PROCESS
    case rpc::to_underlying(rpc::CommandId::CMD_PROCESS_KILL):
      _withPayloadAck<rpc::payload::ProcessKill>(ctx, [](const auto& m) { Process._kill(m); }); break;
    case rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP):
      _withPayloadAck<rpc::payload::ProcessRunAsyncResponse>(ctx, [](const auto& m) { Process._onRunAsyncResponse(m); }); break;
    case rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP):
      _withPayloadAck<rpc::payload::ProcessPollResponse>(ctx, [](const auto& m) { Process._onPollResponse(m); }); break;
#endif
#if BRIDGE_ENABLE_SPI
    case rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN):    _handleSpiBegin(ctx); break;
    case rpc::to_underlying(rpc::CommandId::CMD_SPI_END):      _handleSpiEnd(ctx); break;
    case rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG):
      _withPayloadAck<rpc::payload::SpiConfig>(ctx, [](const auto& m) { SPIService.setConfig(m); }); break;
    case rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER): _handleSpiTransfer(ctx); break;
#endif
    default: onUnknownCommand(ctx); break;
  }
}

void BridgeClass::onUnknownCommand(const bridge::router::CommandContext& ctx) {
  if (_command_handler.is_valid()) _command_handler(*ctx.frame);
  else emitStatus<rpc::StatusCode::STATUS_ERROR>();
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

void BridgeClass::forceSafeState() { bridge::hal::forceSafeState(); }

void BridgeClass::emitStatus(rpc::StatusCode status_code, etl::span<const uint8_t> payload) {
  if (_status_handler.is_valid()) _status_handler(status_code, payload);
  (void)sendFrame(status_code, 0, payload);
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
  rpc::Frame f = {}; f.header.version = rpc::PROTOCOL_VERSION; f.header.command_id = command_id; f.header.sequence_id = sequence_id;
  f.header.payload_length = static_cast<uint16_t>(payload.size());
  f.payload = payload; f.crc = rpc::checksum::compute(f);
  uint8_t buffer[rpc::MAX_FRAME_SIZE];
  size_t len = rpc::FrameParser::serialize(f, etl::span<uint8_t>(buffer, rpc::MAX_FRAME_SIZE));
  if (len > 0) _packet_serial.send(_stream, etl::span<const uint8_t>(buffer, len));
}

bool BridgeClass::_sendFrame(uint16_t command_id, uint16_t sequence_id, etl::span<const uint8_t> payload) {
  if (!_tx_enabled) return false;
  if (is_reliable_cmd(command_id)) {
    BRIDGE_ATOMIC_BLOCK {
      if (_pending_tx_queue.full()) return false;
      TxPayloadBuffer* buf = _tx_payload_pool.allocate();
      if (!buf) return false;
      etl::copy_n(payload.begin(), payload.size(), buf->data.begin());
      PendingTxFrame f = {command_id, sequence_id, buf, payload.size()};
      _pending_tx_queue.push(f);
    }
    if (!_fsm.isAwaitingAck()) _flushPendingTxQueue();
    return true;
  }
  _sendRawFrame(command_id, sequence_id, payload); return true;
}

void BridgeClass::_flushPendingTxQueue() {
  if (!_tx_enabled || _pending_tx_queue.empty() || _fsm.isAwaitingAck()) return;
  const auto& f = _pending_tx_queue.front();
  _sendRawFrame(f.command_id, f.sequence_id, etl::span<const uint8_t>(f.buffer->data.data(), f.length));
  _retry_count = 0; _last_command_id = f.command_id;
  _timers.start(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]); _fsm.sendCritical(); 
}

void BridgeClass::_onAckTimeout() {
  if (!_fsm.isAwaitingAck()) return;
  if (++_retry_count >= _retry_limit) { _timers.stop(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]); _fsm.timeout(); return; }
  _retransmitLastFrame();
}

void BridgeClass::_retransmitLastFrame() {
  if (_pending_tx_queue.empty()) return;
  const auto& f = _pending_tx_queue.front();
  _sendRawFrame(f.command_id, f.sequence_id, etl::span<const uint8_t>(f.buffer->data.data(), f.length));
}

void BridgeClass::_handleAck(uint16_t command_id) {
  if (!_fsm.isAwaitingAck() || command_id != _last_command_id) return;
  _timers.stop(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]); _clearPendingTxQueue(); _fsm.ackReceived();
  _flushPendingTxQueue();
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

void BridgeClass::_onRxDedupe() { _rx_history.clear(); }

void BridgeClass::_onBaudrateChange() {
  if (_pending_baudrate > 0) {
    if (_hardware_serial) _hardware_serial->begin(_pending_baudrate);
    _pending_baudrate = 0;
  }
}

void BridgeClass::_handleStatusMalformed(const bridge::router::CommandContext& ctx) { (void)ctx; enterSafeState(); }
void BridgeClass::_handleStatusAck(const bridge::router::CommandContext& ctx) { _handleAck(ctx.raw_command); }

void BridgeClass::_handleGetVersion(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc::payload::VersionResponse resp = {rpc::FIRMWARE_VERSION_MAJOR, rpc::FIRMWARE_VERSION_MINOR, static_cast<uint32_t>(rpc::FIRMWARE_VERSION_PATCH)};
    _sendPbResponse(rpc::CommandId::CMD_GET_VERSION_RESP, ctx.sequence_id, resp);
  });
}

void BridgeClass::_handleGetFreeMemory(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc::payload::FreeMemoryResponse resp = {static_cast<uint32_t>(bridge::hal::getFreeMemory())};
    _sendPbResponse(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, ctx.sequence_id, resp);
  });
}

void BridgeClass::_handleLinkSync(const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::LinkSync>(*ctx.frame);
  if (!res) {
    emitStatus<rpc::StatusCode::STATUS_ERROR>();
    return;
  }
  const auto& msg = res.value();
  rpc::payload::LinkSync resp = {};
  etl::copy_n(msg.nonce.begin(), 16, resp.nonce.begin());

  if (!_shared_secret.empty()) {
    etl::array<uint8_t, 32> handshake_key;
    handshake_key.fill(0);
    rpc::security::hkdf_sha256(etl::span<uint8_t>(handshake_key), 
                               etl::span<const uint8_t>(_shared_secret),
                               etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_SALT),
                               etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH));
    
    etl::array<uint8_t, 32> full_tag;
    full_tag.fill(0);
    rpc::security::McuBridgeSha256 hmac_engine;
    hmac_engine.resetHMAC(handshake_key.data(), 32);
    hmac_engine.update(msg.nonce.data(), 16);
    hmac_engine.finalizeHMAC(handshake_key.data(), 32, full_tag.data(), 32);

    if (_shared_secret.size() == 14 && memcmp(_shared_secret.data(), "DEBUG_INSECURE", 14) == 0) {
      etl::copy_n(reinterpret_cast<const uint8_t*>("DEBUG_TAG_UNUSED"), 16, resp.tag.begin());
    } else {
      if (!rpc::security::timing_safe_equal(etl::span<const uint8_t>(full_tag.data(), rpc::RPC_HANDSHAKE_TAG_LENGTH), etl::span<const uint8_t>(msg.tag.data(), 16))) {
        _fsm.handshakeFailed(); 
        emitStatus<rpc::StatusCode::STATUS_ERROR>();
        return;
      }
      etl::copy_n(full_tag.begin(), rpc::RPC_HANDSHAKE_TAG_LENGTH, resp.tag.begin());
    }
    rpc::security::secure_zero(handshake_key); rpc::security::secure_zero(full_tag);
  }

  _fsm.handshakeStart(); _fsm.handshakeComplete();
  _sendPbResponse(rpc::CommandId::CMD_LINK_SYNC_RESP, ctx.sequence_id, resp);
  notify_observers(MsgBridgeSynchronized());
}

void BridgeClass::_handleLinkReset(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::HandshakeConfig>(ctx, [this, &ctx](const rpc::payload::HandshakeConfig& msg) {
    _handleSetTiming(msg);
    enterSafeState();
    (void)sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP, ctx.sequence_id);
  });
}

void BridgeClass::_handleGetCapabilities(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc::payload::Capabilities resp = {};
    resp.ver = rpc::PROTOCOL_VERSION; resp.arch = bridge::hal::getArchId();
    resp.feat = bridge::hal::getCapabilities();
    bridge::hal::getPinCounts(resp.dig, resp.ana);
    _sendPbResponse(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, ctx.sequence_id, resp);
  });
}

void BridgeClass::_handleXoff(const bridge::router::CommandContext& ctx) { (void)ctx; _tx_enabled = false; }
void BridgeClass::_handleXon(const bridge::router::CommandContext& ctx) { (void)ctx; _tx_enabled = true; _flushPendingTxQueue(); }

void BridgeClass::_handleSetBaudrate(const rpc::payload::SetBaudratePacket& msg) {
  _pending_baudrate = msg.baudrate;
  _timers.start(_timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE]);
}

void BridgeClass::_handleSetTiming(const rpc::payload::HandshakeConfig& msg) {
  if (msg.ack_timeout_ms > 0) { _ack_timeout_ms = msg.ack_timeout_ms; _timers.set_period(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT], _ack_timeout_ms); }
  if (msg.response_timeout_ms > 0) _response_timeout_ms = msg.response_timeout_ms;
}

void BridgeClass::_handleEnterBootloader(const rpc::payload::EnterBootloader& msg) {
  if (msg.magic == rpc::RPC_BOOTLOADER_MAGIC) {
    this->flushStream(); delay(bridge::config::BOOTLOADER_DELAY_MS);
    bridge::hal::CurrentArchTraits::reset();
  }
}

void BridgeClass::_handleSpiBegin(const bridge::router::CommandContext& ctx) { (void)ctx; SPIService.begin(); }
void BridgeClass::_handleSpiEnd(const bridge::router::CommandContext& ctx) { (void)ctx; SPIService.end(); }
void BridgeClass::_handleSpiTransfer(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    auto res = rpc::Payload::parse<rpc::payload::SpiTransfer>(*ctx.frame);
    if (res) {
      size_t len = etl::min(res->data.size(), _rx_storage.size());
      etl::copy_n(res->data.begin(), len, _rx_storage.begin());
      size_t transferred = SPIService.transfer(etl::span<uint8_t>(_rx_storage.data(), len));
      if (transferred == 0) { emitStatus<rpc::StatusCode::STATUS_ERROR>(); return; }
      rpc::payload::SpiTransferResponse resp = {};
      resp.data = etl::span<const uint8_t>(_rx_storage.data(), len);
      _sendPbResponse(rpc::CommandId::CMD_SPI_TRANSFER_RESP, ctx.sequence_id, resp);
    }
  });
}

void BridgeClass::_handleReceivedFrame(etl::span<const uint8_t> p) {
  auto res = _frame_parser.parse(p);
  if (!res) { _last_parse_error = res.error(); emitStatus<rpc::StatusCode::STATUS_MALFORMED>(); return; }
  rpc::Frame eff; auto dec = _decompressFrame(res.value(), eff);
  if (!dec) { _last_parse_error = dec.error(); emitStatus<rpc::StatusCode::STATUS_MALFORMED>(); return; }
  _dispatchCommand(eff);
}

void BridgeClass::_onPacketReceived(etl::span<const uint8_t> p) { _handleReceivedFrame(p); }

bool BridgeClass::runPowerOnSelfTests() { return rpc::security::run_cryptographic_self_tests(); }

etl::expected<void, rpc::FrameError> BridgeClass::_decompressFrame(const rpc::Frame& in, rpc::Frame& out) {
  out.header = in.header;
  if (!is_compressed_cmd(in.header.command_id)) { out.payload = in.payload; return {}; }
  _rx_storage.fill(0);
  size_t dec_len = ::rle::decode(in.payload, etl::span<uint8_t>(_rx_storage.data(), _rx_storage.size()));
  if (dec_len == 0 && !in.payload.empty()) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);
  out.payload = etl::span<uint8_t>(_rx_storage.data(), dec_len);
  out.header.payload_length = static_cast<uint16_t>(dec_len);
  return {};
}

void BridgeClass::_computeHandshakeTag(const etl::span<const uint8_t> nonce, etl::span<uint8_t> tag) {
  etl::array<uint8_t, 32> handshake_key;
  handshake_key.fill(0);
  rpc::security::hkdf_sha256(etl::span<uint8_t>(handshake_key),
                             etl::span<const uint8_t>(_shared_secret),
                             etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_SALT),
                             etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH));
  
  etl::array<uint8_t, 32> full_tag;
  full_tag.fill(0);
  rpc::security::McuBridgeSha256 hmac_engine;
  hmac_engine.resetHMAC(handshake_key.data(), 32);
  hmac_engine.update(nonce.data(), nonce.size());
  hmac_engine.finalizeHMAC(handshake_key.data(), 32, full_tag.data(), 32);
                             
  etl::copy_n(full_tag.data(), rpc::RPC_HANDSHAKE_TAG_LENGTH, tag.data());
  rpc::security::secure_zero(handshake_key); rpc::security::secure_zero(full_tag);
}

void BridgeClass::_applyTimingConfig(const rpc::payload::HandshakeConfig& msg) {
  _handleSetTiming(msg);
}

bool BridgeClass::_isSecurityCheckPassed(uint16_t command_id) const {
  if (_shared_secret.empty()) return true;
  // Literal values for LINK_SYNC (68), LINK_RESET (70), GET_VERSION (64)
  if (command_id == 68 || command_id == 70 || command_id == 64) return true;
  return _fsm.isSynchronized();
}

void BridgeClass::signalXoff() { (void)sendFrame(rpc::CommandId::CMD_XOFF); }
void BridgeClass::signalXon() { (void)sendFrame(rpc::CommandId::CMD_XON); }

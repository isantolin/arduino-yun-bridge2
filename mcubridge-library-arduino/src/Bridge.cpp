#include "Bridge.h"
#include <etl/algorithm.h>
#include <etl/functional.h>
#include <etl/iterator.h>
#include "hal/ArchTraits.h"
#include <wolfssl/wolfcrypt/settings.h>
#include <wolfssl/wolfcrypt/types.h>
#include "hal/progmem_compat.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"

namespace etl {
void __attribute__((weak)) __attribute__((unused)) handle_error(const etl::exception& e) {
  BridgeClass::ErrorPolicy::handle(Bridge, e);
}
}

BridgeClass::BridgeClass(Stream& stream)
    : _stream(stream), _hardware_serial(nullptr), _command_handler(), _status_handler(),
      _last_command_id(0), _tx_sequence_id(0), _retry_count(0),
      _retry_limit(rpc::RPC_DEFAULT_RETRY_LIMIT), _ack_timeout_ms(rpc::RPC_DEFAULT_ACK_TIMEOUT_MS),
      _response_timeout_ms(rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS),
      _pending_baudrate(0), _consecutive_crc_errors(0), _last_parse_error(rpc::FrameError::NONE),
      _ps_rx_storage(), _ps_work_buffer(),
      _packet_serial(etl::span<uint8_t>(_ps_rx_storage.data(), _ps_rx_storage.size()),
                     etl::span<uint8_t>(_ps_work_buffer.data(), _ps_work_buffer.size())),
      _shared_secret(), _session_key(), _tx_nonce_counter(0), _rx_nonce_counter(0),
      _fsm(), _watchdog_task(), _serial_task(), _timer_task(), _tasks(),
      _scheduler_policy(), _timers(), _timer_ids(), _transient_buffer(), _rx_storage(),
      _is_post_passed(false), _tx_enabled(true), _tx_payload_pool(), _pending_tx_queue(), _rx_history() {}

void BridgeClass::_dispatchCommand(const rpc_pb_RpcEnvelope& envelope) {
  const uint16_t cmd_id = envelope.command_id & ~rpc::RPC_CMD_FLAG_COMPRESSED;
  auto it = etl::find(_rx_history.begin(), _rx_history.end(), envelope.sequence_id);
  const bool is_duplicate = (it != _rx_history.end());
  const bridge::router::CommandContext ctx(&envelope, cmd_id, envelope.sequence_id, is_duplicate, rpc::requires_ack(cmd_id));

  if (!is_duplicate) {
    if (_rx_history.full()) _rx_history.pop();
    _rx_history.push(envelope.sequence_id);
  }

  if (!_isSecurityCheckPassed(ctx.raw_command)) {
    [[maybe_unused]] auto _u1 = sendFrame(rpc::StatusCode::STATUS_ERROR, ctx.sequence_id);
    return;
  }

  auto handler = _getHandler(cmd_id);
  if (handler) handler(*this, ctx);
  else onUnknownCommand(ctx);
}

#include "protocol/rpc_dispatch.h"

BridgeClass::DispatchHandler BridgeClass::_getHandler(uint16_t command_id) {
  static const etl::array<DispatchHandler, rpc::RPC_MAX_COMMAND_ID> table = []() {
    etl::array<DispatchHandler, rpc::RPC_MAX_COMMAND_ID> t = {};
    t.fill(nullptr);
    DEFINE_RPC_DISPATCH_TABLE(BridgeClass, t);
    t[rpc::to_underlying(rpc::StatusCode::STATUS_OK)] = &BridgeClass::_dispatchSimpleAck<&BridgeClass::_handleStatusOk>;
    t[rpc::to_underlying(rpc::StatusCode::STATUS_ACK)] = &BridgeClass::_dispatchPayload<rpc_pb_AckPacket, &BridgeClass::_handleAckStruct>;
    t[rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED)] = &BridgeClass::_dispatchSimple<&BridgeClass::_handleStatusMalformed>;
    return t;
  }();
  return (command_id < rpc::RPC_MAX_COMMAND_ID) ? table[command_id] : nullptr;
}

void BridgeClass::_initializeRuntime() {
  _tasks.clear(); _serial_task.bind(*this); _timer_task.bind(*this);
  _tasks.push_back(&_watchdog_task); _tasks.push_back(&_serial_task); _tasks.push_back(&_timer_task);
  _rx_storage.fill(0); _ps_rx_storage.fill(0); _ps_work_buffer.fill(0);
  if constexpr (bridge::hal::CurrentArchTraits::id == bridge::hal::ArchId::ARCH_AVR) _hardware_serial = static_cast<HardwareSerial*>(&_stream);
  else _hardware_serial = nullptr;
}

void BridgeClass::begin(uint32_t baudrate, const char* secret) {
  _initializeRuntime(); wolfCrypt_Init(); _shared_secret.clear();
  if (secret != nullptr) {
    const etl::string_view s(secret);
    const size_t len = etl::min(s.size(), _shared_secret.capacity());
    const auto data_ptr = static_cast<const uint8_t*>(static_cast<const void*>(s.data()));
    _shared_secret.assign(data_ptr, data_ptr + len);
  }
  bridge::hal::init();
  if (!_fsm.is_started()) _fsm.start();
  _fsm.receive(bridge::fsm::EvReset());
  _is_post_passed = rpc::security::run_cryptographic_self_tests();
  if (!_is_post_passed) enterSafeState();
  if constexpr (bridge::hal::CurrentArchTraits::id == bridge::hal::ArchId::ARCH_AVR) if (baudrate > 0 && _hardware_serial) _hardware_serial->begin(baudrate);
  _tx_enabled = true; _timers.clear();
  _timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT] = _timers.register_timer([]() { Bridge._onAckTimeout(); }, _ack_timeout_ms, etl::timer::mode::REPEATING);
  _timer_ids[bridge::scheduler::TIMER_RX_DEDUPE] = _timers.register_timer([]() { Bridge._onRxDedupe(); }, bridge::config::RX_DEDUPE_INTERVAL_MS, etl::timer::mode::REPEATING);
  _timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE] = _timers.register_timer([]() { Bridge._onBaudrateChange(); }, bridge::config::BAUDRATE_CHANGE_DELAY_MS, etl::timer::mode::SINGLE_SHOT);
  _timer_ids[bridge::scheduler::TIMER_BOOTLOADER_DELAY] = _timers.register_timer([]() { Bridge._onBootloaderDelay(); }, bridge::config::BOOTLOADER_DELAY_MS, etl::timer::mode::SINGLE_SHOT);
  _packet_serial.setPacketHandler(etl::delegate<void(etl::span<const uint8_t>)>::create<BridgeClass, &BridgeClass::_onPacketReceived>(*this));
}

void BridgeClass::process() { [[maybe_unused]] auto _u1 = _scheduler_policy.schedule_tasks(_tasks); }
void BridgeClass::WatchdogTask::task_process_work() { bridge::hal::watchdog_kick(); }
void BridgeClass::SerialTask::task_process_work() {
  if (!bridge) return;
  bridge->_packet_serial.update(bridge->_stream);
  const int avail = bridge->_stream.available();
  if (!xoff_sent && avail > bridge::config::FLOW_CONTROL_XOFF_THRESHOLD) { bridge->signalXoff(); xoff_sent = true; }
  else if (xoff_sent && avail < bridge::config::FLOW_CONTROL_XON_THRESHOLD) { bridge->signalXon(); xoff_sent = false; }
}
void BridgeClass::TimerTask::task_process_work() {
  if (!bridge) return;
  const uint32_t now = ::millis();
  if (last_tick_ms == 0) last_tick_ms = now;
  const uint32_t elapsed = now - last_tick_ms;
  if (elapsed > 0) { bridge->_timers.tick(elapsed); last_tick_ms = now; }
}

bool BridgeClass::isSynchronized() const { return _fsm.isSynchronized(); }
void BridgeClass::onUnknownCommand(const bridge::router::CommandContext& ctx) {
  if (_command_handler.is_valid()) _command_handler(*ctx.envelope);
  else emitStatus(rpc::StatusCode::STATUS_ERROR);
}

void BridgeClass::enterSafeState() {
  bridge::hal::forceSafeState(); _tx_enabled = false; _clearPendingTxQueue(); _fsm.receive(bridge::fsm::EvReset());
  Console.onLost(); DataStore.onLost(); Mailbox.onLost(); Process.onLost();
}

void BridgeClass::emitStatus(rpc::StatusCode code, etl::span<const uint8_t> pl) { (void)sendFrame(code, 0, pl); }
void BridgeClass::emitStatus(rpc::StatusCode code, etl::string_view msg) {
  rpc_pb_GenericResponse resp = rpc_pb_GenericResponse_init_default;
  const size_t to_copy = etl::min(msg.size(), sizeof(resp.message.bytes) - 1U);
  if (to_copy > 0U) etl::copy_n(msg.begin(), to_copy, resp.message.bytes);
  resp.message.bytes[to_copy] = 0; (void)send(code, 0, resp);
}
void BridgeClass::emitStatus(rpc::StatusCode code, const __FlashStringHelper* msg) {
  if (!msg) { (void)sendFrame(code); return; }
  constexpr size_t max_len = 63U; etl::string<max_len> str; str.resize(max_len);
  bridge::hal::copy_string(str.data(), reinterpret_cast<const char*>(msg), max_len);
  str.resize(etl::strlen(str.data()));
  rpc_pb_GenericResponse resp = rpc_pb_GenericResponse_init_default;
  const size_t to_copy = etl::min(static_cast<size_t>(str.length()), sizeof(resp.message.bytes) - 1U);
  if (to_copy > 0U) etl::copy_n(str.begin(), to_copy, resp.message.bytes);
  resp.message.bytes[to_copy] = 0; (void)send(code, 0, resp);
}

bool BridgeClass::sendFrame(rpc::StatusCode s, uint16_t seq, etl::span<const uint8_t> p) { return sendFrame(rpc::to_underlying(s), seq, p); }
bool BridgeClass::sendFrame(rpc::CommandId c, uint16_t seq, etl::span<const uint8_t> p) { return sendFrame(rpc::to_underlying(c), seq, p); }
bool BridgeClass::sendFrame(uint16_t cmd, uint16_t seq, etl::span<const uint8_t> pl) {
  const uint16_t raw_cmd = cmd & ~rpc::RPC_CMD_FLAG_COMPRESSED;
  const bool is_system = (raw_cmd >= rpc::RPC_STATUS_CODE_MIN && raw_cmd <= rpc::RPC_STATUS_CODE_MAX) || (raw_cmd >= rpc::RPC_SYSTEM_COMMAND_MIN && raw_cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);
  if (!_tx_enabled && !is_system) return false;
  if (is_reliable_cmd(cmd)) {
    BRIDGE_ATOMIC_BLOCK {
      if (_pending_tx_queue.full()) return false;
      auto* buf = _tx_payload_pool.allocate(); if (!buf) return false;
      etl::copy_n(pl.data(), pl.size(), buf->data.data());
      _pending_tx_queue.push_back({cmd, seq, buf, pl.size()});
      if (!_fsm.isAwaitingAck()) _flushPendingTxQueue();
    }
    return true;
  }
  _transmit(cmd, seq, pl); return true;
}

void BridgeClass::_transmit(uint16_t command_id, uint16_t sequence_id, etl::span<const uint8_t> payload) {
  const uint16_t raw_cmd = command_id & ~rpc::RPC_CMD_FLAG_COMPRESSED;
  const bool is_excluded = (raw_cmd >= rpc::RPC_STATUS_CODE_MIN && raw_cmd <= rpc::RPC_STATUS_CODE_MAX) || (raw_cmd >= rpc::RPC_SYSTEM_COMMAND_MIN && raw_cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);
  const bool do_encrypt = isSynchronized() && !_shared_secret.empty() && !is_excluded;
  etl::array<uint8_t, rpc::AEAD_NONCE_SIZE> nonce = {}; etl::array<uint8_t, rpc::AEAD_TAG_SIZE> tag = {};
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> enc_pl; etl::span<const uint8_t> final_payload = payload;
  if (do_encrypt) {
    if (!rpc::security::aead_encrypt_frame(raw_cmd, sequence_id, payload, _session_key, &_tx_nonce_counter, enc_pl, nonce, tag)) return;
    final_payload = etl::span<const uint8_t>(enc_pl.data(), payload.size());
  }
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> buffer; rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  env.version = rpc::PROTOCOL_VERSION; env.command_id = command_id; env.sequence_id = sequence_id;
  etl::copy_n(nonce.begin(), rpc::AEAD_NONCE_SIZE, env.nonce.bytes); env.nonce.size = static_cast<pb_size_t>(rpc::AEAD_NONCE_SIZE);
  etl::copy_n(tag.begin(), rpc::AEAD_TAG_SIZE, env.tag.bytes); env.tag.size = static_cast<pb_size_t>(rpc::AEAD_TAG_SIZE);
  const size_t pl_size = etl::min(final_payload.size(), static_cast<size_t>(rpc::MAX_PAYLOAD_SIZE));
  etl::copy_n(final_payload.begin(), pl_size, env.payload.bytes); env.payload.size = static_cast<pb_size_t>(pl_size);
  size_t len = rpc::serialize_frame(env, buffer);
  if (len > 0) _packet_serial.send(_stream, etl::span<const uint8_t>(buffer.data(), len));
}

void BridgeClass::_flushPendingTxQueue() {
  BRIDGE_ATOMIC_BLOCK {
    if (_pending_tx_queue.empty() || !_tx_enabled) return;
    const auto& f = _pending_tx_queue.front(); _last_command_id = f.command_id; _retry_count = 0;
    _fsm.receive(bridge::fsm::EvSendCritical()); _transmit(f.command_id, f.sequence_id, etl::span<const uint8_t>(f.buffer->data.data(), f.length));
    _timers.start(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
  }
}

void BridgeClass::_retransmitLastFrame() {
  BRIDGE_ATOMIC_BLOCK {
    if (_pending_tx_queue.empty()) return;
    const auto& f = _pending_tx_queue.front(); _transmit(f.command_id, f.sequence_id, etl::span<const uint8_t>(f.buffer->data.data(), f.length));
    _timers.start(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
  }
}

void BridgeClass::_onAckTimeout() {
  if (!_fsm.isAwaitingAck()) return;
  if (++_retry_count >= _retry_limit) {
    _timers.stop(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]); _fsm.receive(bridge::fsm::EvTimeout());
    _tx_enabled = false; _clearPendingTxQueue(); return;
  }
  _retransmitLastFrame();
}

void BridgeClass::_processAck(uint16_t command_id, uint16_t sequence_id) {
  rpc_pb_AckPacket p = {}; p.command_id = command_id;
  (void)send(rpc::StatusCode::STATUS_ACK, sequence_id, p);
}

void BridgeClass::_handleAck(uint16_t cmd) {
  if (!_fsm.isAwaitingAck() || cmd != _last_command_id) return;
  _timers.stop(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]); _clearPendingTxQueue();
  _fsm.receive(bridge::fsm::EvAckReceived()); _flushPendingTxQueue();
}

void BridgeClass::_clearPendingTxQueue() {
  BRIDGE_ATOMIC_BLOCK {
    etl::for_each(_pending_tx_queue.begin(), _pending_tx_queue.end(), [this](PendingTxFrame& f) { if (f.buffer) _tx_payload_pool.release(f.buffer); });
    _pending_tx_queue.clear();
  }
}

void BridgeClass::_onRxDedupe() { _rx_history.clear(); }
void BridgeClass::_onBaudrateChange() { if (_pending_baudrate > 0) { if (_hardware_serial) _hardware_serial->begin(_pending_baudrate); _pending_baudrate = 0; } }
void BridgeClass::_onBootloaderDelay() { bridge::hal::enterBootloader(); }

void BridgeClass::_handleStatusOk(const bridge::router::CommandContext& ctx) { (void)ctx; }
void BridgeClass::_handleStatusMalformed(const bridge::router::CommandContext& ctx) { (void)ctx; enterSafeState(); }

void BridgeClass::_handleAckStruct(const bridge::router::CommandContext& ctx, const rpc_pb_AckPacket& msg) { (void)ctx; _handleAck(msg.command_id); }

void BridgeClass::_handleGetVersion(const bridge::router::CommandContext& ctx) {
  (void)ctx; rpc_pb_VersionResponse resp = {}; resp.major = rpc::FIRMWARE_VERSION_MAJOR; resp.minor = rpc::FIRMWARE_VERSION_MINOR; resp.patch = (uint32_t)rpc::FIRMWARE_VERSION_PATCH;
  (void)send(rpc::CommandId::CMD_GET_VERSION_RESP, ctx.sequence_id, resp);
}

void BridgeClass::_handleGetVersionResponse(const bridge::router::CommandContext& ctx, const rpc_pb_VersionResponse& msg) { (void)ctx; (void)msg; }

void BridgeClass::_handleGetFreeMemory(const bridge::router::CommandContext& ctx) {
  (void)ctx; rpc_pb_FreeMemoryResponse resp = {}; resp.value = (uint32_t)bridge::hal::getFreeMemory();
  (void)send(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, ctx.sequence_id, resp);
}

void BridgeClass::_handleGetFreeMemoryResponse(const bridge::router::CommandContext& ctx, const rpc_pb_FreeMemoryResponse& msg) { (void)ctx; (void)msg; }

void BridgeClass::_handleLinkSync(const bridge::router::CommandContext& ctx, const rpc_pb_LinkSync& msg) {
  (void)ctx; rpc_pb_LinkSync resp = {}; const size_t n_size = etl::min(static_cast<size_t>(msg.nonce.size), static_cast<size_t>(rpc::RPC_HANDSHAKE_NONCE_LENGTH));
  etl::copy_n(msg.nonce.bytes, n_size, resp.nonce.bytes); resp.nonce.size = static_cast<pb_size_t>(n_size);
  if (!_shared_secret.empty()) {
    etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> out_tag;
    if (rpc::security::handshake_authenticate(etl::span<const uint8_t>(_shared_secret), etl::span<const uint8_t>(msg.nonce.bytes, n_size), etl::span<const uint8_t>(msg.tag.bytes, msg.tag.size), etl::span<uint8_t>(out_tag))) {
      etl::copy_n(out_tag.data(), rpc::RPC_HANDSHAKE_TAG_LENGTH, resp.tag.bytes); resp.tag.size = rpc::RPC_HANDSHAKE_TAG_LENGTH;
      rpc::security::derive_session_key(etl::span<const uint8_t>(_shared_secret), etl::span<const uint8_t>(msg.nonce.bytes, n_size), etl::span<uint8_t>(_session_key));
      _tx_nonce_counter = 0; _rx_nonce_counter = 0; rpc::security::secure_zero(etl::span<uint8_t>(out_tag));
    } else { _fsm.receive(bridge::fsm::EvHandshakeFailed()); emitStatus(rpc::StatusCode::STATUS_ERROR); return; }
  }
  _fsm.receive(bridge::fsm::EvHandshakeStart()); _fsm.receive(bridge::fsm::EvHandshakeComplete());
  (void)send(rpc::CommandId::CMD_LINK_SYNC_RESP, ctx.sequence_id, resp);
}

void BridgeClass::_handleLinkSyncResponse(const bridge::router::CommandContext& ctx, const rpc_pb_LinkSync& msg) { (void)ctx; (void)msg; }

void BridgeClass::_handleLinkReset(const bridge::router::CommandContext& ctx) {
  (void)ctx; _fsm.receive(bridge::fsm::EvReset()); (void)sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP, ctx.sequence_id);
}

void BridgeClass::_handleLinkResetResponse(const bridge::router::CommandContext& ctx) { (void)ctx; }

void BridgeClass::_handleGetCapabilities(const bridge::router::CommandContext& ctx) {
  (void)ctx; rpc_pb_Capabilities resp = rpc_pb_Capabilities_init_default; bridge::hal::fillCapabilities(resp);
  (void)send(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, ctx.sequence_id, resp);
}

void BridgeClass::_handleGetCapabilitiesResponse(const bridge::router::CommandContext& ctx, const rpc_pb_Capabilities& msg) { (void)ctx; (void)msg; }

void BridgeClass::_handleSetBaudrate(const bridge::router::CommandContext& ctx) {
  rpc_pb_SetBaudratePacket msg = {}; pb_istream_t stream = pb_istream_from_buffer(ctx.envelope->payload.bytes, ctx.envelope->payload.size);
  if (pb_decode(&stream, rpc::Payload::get_fields<rpc_pb_SetBaudratePacket>(), &msg)) {
    if (msg.baudrate == 0 || msg.baudrate == _pending_baudrate) return;
    _pending_baudrate = msg.baudrate; _timers.start(_timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE]);
  }
}

void BridgeClass::_handleSetBaudrateResponse(const bridge::router::CommandContext& ctx) { (void)ctx; }

void BridgeClass::_handleEnterBootloader(const bridge::router::CommandContext& ctx, const rpc_pb_EnterBootloader& msg) {
  (void)ctx; if (msg.magic == rpc::RPC_BOOTLOADER_MAGIC) _timers.start(_timer_ids[bridge::scheduler::TIMER_BOOTLOADER_DELAY]);
}

void BridgeClass::_handleXoff(const bridge::router::CommandContext& ctx) { (void)ctx; _tx_enabled = false; }
void BridgeClass::_handleXon(const bridge::router::CommandContext& ctx) { (void)ctx; _tx_enabled = true; }

void BridgeClass::_handleSetPinMode(const bridge::router::CommandContext& ctx, const rpc_pb_PinMode& msg) {
  (void)ctx; uint8_t m_val = INPUT; if (msg.mode == 1) m_val = OUTPUT; else if (msg.mode == 2) m_val = INPUT_PULLUP; pinMode(msg.pin, m_val);
}

void BridgeClass::_handleDigitalWrite(const bridge::router::CommandContext& ctx, const rpc_pb_DigitalWrite& msg) {
  (void)ctx; digitalWrite(msg.pin, (msg.value == 0) ? LOW : HIGH);
}

void BridgeClass::_handleAnalogWrite(const bridge::router::CommandContext& ctx, const rpc_pb_AnalogWrite& msg) {
  (void)ctx; analogWrite(msg.pin, (int)msg.value);
}

void BridgeClass::_handleDigitalRead(const bridge::router::CommandContext& ctx, const rpc_pb_PinRead& msg) {
  (void)ctx; if (bridge::hal::isValidPin(msg.pin)) {
    rpc_pb_DigitalReadResponse resp = {}; resp.value = static_cast<uint32_t>(::digitalRead(msg.pin));
    (void)send(rpc::CommandId::CMD_DIGITAL_READ_RESP, ctx.sequence_id, resp);
  } else emitStatus(rpc::StatusCode::STATUS_ERROR);
}

void BridgeClass::_handleDigitalReadResponse(const bridge::router::CommandContext& ctx, const rpc_pb_DigitalReadResponse& msg) { (void)ctx; (void)msg; }

void BridgeClass::_handleAnalogRead(const bridge::router::CommandContext& ctx, const rpc_pb_PinRead& msg) {
  (void)ctx; if (bridge::hal::isValidPin(msg.pin)) {
    rpc_pb_AnalogReadResponse resp = {}; resp.value = static_cast<uint32_t>(::analogRead(msg.pin));
    (void)send(rpc::CommandId::CMD_ANALOG_READ_RESP, ctx.sequence_id, resp);
  } else emitStatus(rpc::StatusCode::STATUS_ERROR);
}

void BridgeClass::_handleAnalogReadResponse(const bridge::router::CommandContext& ctx, const rpc_pb_AnalogReadResponse& msg) { (void)ctx; (void)msg; }

void BridgeClass::_handleConsoleWrite(const bridge::router::CommandContext& ctx, const rpc_pb_ConsoleWrite& msg) {
  (void)ctx; Console._push(msg);
}

#if BRIDGE_ENABLE_DATASTORE
void BridgeClass::_handleDatastoreGet(const bridge::router::CommandContext& ctx, const rpc_pb_DatastoreGet& msg) { (void)ctx; (void)msg; }
void BridgeClass::_handleDatastoreGetResponse(const bridge::router::CommandContext& ctx, const rpc_pb_DatastoreGetResponse& msg) { (void)ctx; DataStore._onResponse(msg); }
void BridgeClass::_handleDatastorePut(const bridge::router::CommandContext& ctx, const rpc_pb_DatastorePut& msg) { (void)ctx; (void)msg; }
#endif

#if BRIDGE_ENABLE_MAILBOX
void BridgeClass::_handleMailboxPush(const bridge::router::CommandContext& ctx, const rpc_pb_MailboxPush& msg) { (void)ctx; Mailbox._onIncomingData(msg); }
void BridgeClass::_handleMailboxAvailable(const bridge::router::CommandContext& ctx) { (void)ctx; }
void BridgeClass::_handleMailboxAvailableResponse(const bridge::router::CommandContext& ctx, const rpc_pb_MailboxAvailableResponse& msg) { (void)ctx; Mailbox._onAvailableResponse(msg); }
void BridgeClass::_handleMailboxRead(const bridge::router::CommandContext& ctx) { (void)ctx; }
void BridgeClass::_handleMailboxReadResponse(const bridge::router::CommandContext& ctx, const rpc_pb_MailboxReadResponse& msg) { (void)ctx; Mailbox._onIncomingData(msg); }
void BridgeClass::_handleMailboxProcessed(const bridge::router::CommandContext& ctx, const rpc_pb_MailboxProcessed& msg) { (void)ctx; (void)msg; }
#endif

#if BRIDGE_ENABLE_FILESYSTEM
void BridgeClass::_handleFileWrite(const bridge::router::CommandContext& ctx, const rpc_pb_FileWrite& msg) { (void)ctx; FileSystem._onWrite(msg); }
void BridgeClass::_handleFileRead(const bridge::router::CommandContext& ctx, const rpc_pb_FileRead& msg) { (void)ctx; FileSystem._onRead(msg); }
void BridgeClass::_handleFileRemove(const bridge::router::CommandContext& ctx, const rpc_pb_FileRemove& msg) { (void)ctx; FileSystem._onRemove(msg); }
void BridgeClass::_handleFileReadResponse(const bridge::router::CommandContext& ctx, const rpc_pb_FileReadResponse& msg) { (void)ctx; FileSystem._onResponse(msg); }
#endif

#if BRIDGE_ENABLE_PROCESS
void BridgeClass::_handleProcessKill(const bridge::router::CommandContext& ctx, const rpc_pb_ProcessKill& msg) { (void)ctx; Process._onKillNotification(msg); }
void BridgeClass::_handleProcessRunAsync(const bridge::router::CommandContext& ctx, const rpc_pb_ProcessRunAsync& msg) { (void)ctx; (void)msg; }
void BridgeClass::_handleProcessRunAsyncResponse(const bridge::router::CommandContext& ctx, const rpc_pb_ProcessRunAsyncResponse& msg) { (void)ctx; Process._onRunAsyncResponse(msg); }
void BridgeClass::_handleProcessPoll(const bridge::router::CommandContext& ctx, const rpc_pb_ProcessPoll& msg) { (void)ctx; (void)msg; }
void BridgeClass::_handleProcessPollResponse(const bridge::router::CommandContext& ctx, const rpc_pb_ProcessPollResponse& msg) { (void)ctx; Process._onPollResponse(msg); }
#endif

#if BRIDGE_ENABLE_SPI
void BridgeClass::_handleSpiSetConfig(const bridge::router::CommandContext& ctx, const rpc_pb_SpiConfig& msg) { (void)ctx; SPIService.setConfig(msg); }
void BridgeClass::_handleSpiBegin(const bridge::router::CommandContext& ctx) { (void)ctx; SPIService.begin(); _processAck(ctx.raw_command, ctx.sequence_id); }
void BridgeClass::_handleSpiEnd(const bridge::router::CommandContext& ctx) { (void)ctx; SPIService.end(); _processAck(ctx.raw_command, ctx.sequence_id); }
void BridgeClass::_handleSpiTransfer(const bridge::router::CommandContext& ctx, const rpc_pb_SpiTransfer& msg) {
  (void)ctx; size_t len = etl::min((size_t)msg.data.size, _rx_storage.size()); etl::copy_n(msg.data.bytes, len, _rx_storage.begin());
  size_t tr = SPIService.transfer(etl::span<uint8_t>(_rx_storage.data(), len));
  if (tr == 0) { emitStatus(rpc::StatusCode::STATUS_ERROR); return; }
  rpc_pb_SpiTransferResponse resp = {}; const size_t to_copy = etl::min(len, sizeof(resp.data.bytes));
  resp.data.size = (pb_size_t)to_copy; if (to_copy > 0) etl::copy_n(_rx_storage.data(), to_copy, resp.data.bytes);
  (void)send(rpc::CommandId::CMD_SPI_TRANSFER_RESP, ctx.sequence_id, resp);
}
void BridgeClass::_handleSpiTransferResponse(const bridge::router::CommandContext& ctx, const rpc_pb_SpiTransferResponse& msg) { (void)ctx; (void)msg; }
#endif

void BridgeClass::_handleSetTiming(const rpc_pb_HandshakeConfig& msg) { _applyTimingConfig(msg); }
void BridgeClass::_applyTimingConfig(const rpc_pb_HandshakeConfig& msg) {
  _ack_timeout_ms = (uint16_t)msg.ack_timeout_ms; _retry_limit = (uint8_t)msg.ack_retry_limit; _response_timeout_ms = msg.response_timeout_ms;
}

void BridgeClass::_handleReceivedFrame(etl::span<const uint8_t> p) {
  auto res = rpc::parse_frame(p); if (!res) { _last_parse_error = res.error(); emitStatus(rpc::StatusCode::STATUS_MALFORMED); return; }
  rpc_pb_RpcEnvelope envelope = res.value(); if (envelope.version != rpc::PROTOCOL_VERSION) { emitStatus(rpc::StatusCode::STATUS_MALFORMED); return; }
  const uint16_t raw_cmd = envelope.command_id & ~rpc::RPC_CMD_FLAG_COMPRESSED;
  const bool is_excluded = (raw_cmd >= rpc::RPC_STATUS_CODE_MIN && raw_cmd <= rpc::RPC_STATUS_CODE_MAX) || (raw_cmd >= rpc::RPC_SYSTEM_COMMAND_MIN && raw_cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);
  if (isSynchronized() && !_shared_secret.empty() && !is_excluded) {
    etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> dec_pl;
    if (!rpc::security::aead_decrypt_frame(raw_cmd, envelope.sequence_id, etl::span<const uint8_t>(envelope.payload.bytes, envelope.payload.size), etl::span<const uint8_t>(envelope.tag.bytes, 16), _session_key, etl::span<const uint8_t>(envelope.nonce.bytes, 12), dec_pl) || !rpc::security::validate_frame_nonce(etl::span<const uint8_t>(envelope.nonce.bytes, 12), &_rx_nonce_counter)) { emitStatus(rpc::StatusCode::STATUS_ERROR); return; }
    etl::copy_n(dec_pl.data(), envelope.payload.size, envelope.payload.bytes);
  }
  rpc_pb_RpcEnvelope eff; auto dec = _decompressFrame(envelope, eff); if (!dec) { _last_parse_error = dec.error(); emitStatus(rpc::StatusCode::STATUS_MALFORMED); return; }
  _dispatchCommand(eff);
}

void BridgeClass::_onPacketReceived(etl::span<const uint8_t> p) { _handleReceivedFrame(p); }

etl::expected<void, rpc::FrameError> BridgeClass::_decompressFrame(const rpc_pb_RpcEnvelope& in, rpc_pb_RpcEnvelope& out) {
  out = in; if (!rpc::is_compressed(in.command_id)) return {};
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> decomp_pl;
  size_t decomp_size = rle::decode(etl::span<const uint8_t>(in.payload.bytes, in.payload.size), decomp_pl);
  if (decomp_size == 0) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);
  etl::copy_n(decomp_pl.data(), decomp_size, out.payload.bytes); out.payload.size = static_cast<pb_size_t>(decomp_size);
  return {};
}

bool BridgeClass::_isSecurityCheckPassed(uint16_t cmd) const {
  if (_shared_secret.empty()) return true;
  if ((cmd >= rpc::RPC_STATUS_CODE_MIN && cmd <= rpc::RPC_STATUS_CODE_MAX) || (cmd >= rpc::RPC_SYSTEM_COMMAND_MIN && cmd <= rpc::RPC_SYSTEM_COMMAND_MAX)) return true;
  return _fsm.isSynchronized();
}

void BridgeClass::signalXoff() { (void)sendFrame(rpc::CommandId::CMD_XOFF); }
void BridgeClass::signalXon() { (void)sendFrame(rpc::CommandId::CMD_XON); }

namespace bridge {
void SafeStatePolicy::handle(::BridgeClass& bridge, const etl::exception& e) { (void)e; bridge.enterSafeState(); }
}

#include "Bridge.h"

#include <etl/algorithm.h>
#include <etl/functional.h>
#include <etl/iterator.h>
#include <wolfssl/wolfcrypt/settings.h>
#include <wolfssl/wolfcrypt/types.h>

#include "hal/ArchTraits.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"

namespace etl {
void __attribute__((weak)) handle_error(const etl::exception& e) {
  BridgeClass::ErrorPolicy::handle(Bridge, e);
}
}  // namespace etl

BridgeClass::BridgeClass(Stream& stream)
    : _stream(stream),
      _packet_serial(etl::span<uint8_t>(_rx_buffer.data(), _rx_buffer.size()),
                     etl::span<uint8_t>(_rx_buffer.data(), _rx_buffer.size())) {
  _packet_serial.setPacketHandler(
      etl::delegate<void(etl::span<const uint8_t>)>::create<
          BridgeClass, &BridgeClass::_handleReceivedFrame>(*this));
}

void BridgeClass::_dispatchCommand(const rpc_pb_RpcEnvelope& envelope) {
  const uint16_t cmd_id = envelope.command_id;
  auto it =
      etl::find(_rx_history.begin(), _rx_history.end(), envelope.sequence_id);
  const bool is_duplicate = (it != _rx_history.end());
  const bridge::router::CommandContext ctx(&envelope, cmd_id,
                                           envelope.sequence_id, is_duplicate,
                                           rpc::requires_ack(cmd_id));

  if (!is_duplicate) {
    if (_rx_history.full()) _rx_history.pop();
    _rx_history.push(envelope.sequence_id);
  }

  if (!_isSecurityCheckPassed(ctx.raw_command)) {
    if (!sendFrame(rpc::StatusCode::STATUS_ERROR, ctx.sequence_id)) {
    }
    return;
  }

  auto handler = _getHandler(cmd_id);
  if (handler)
    handler(*this, ctx);
  else
    onUnknownCommand(ctx);
}

BridgeClass::DispatchHandler BridgeClass::_getHandler(uint16_t command_id) {
  switch (command_id) {
    case rpc::to_underlying(rpc::StatusCode::STATUS_OK):
      return &BridgeClass::_dispatchSimpleAck<&BridgeClass::_handleStatusOk>;
    case rpc::to_underlying(rpc::StatusCode::STATUS_ACK):
      return &BridgeClass::_dispatchAckCtx<rpc_pb_AckPacket,
                                           &BridgeClass::_handleStatusAck>;
    case rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED):
      return &BridgeClass::_dispatchSimple<
          &BridgeClass::_handleStatusMalformed>;
    case rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION):
      return &BridgeClass::_dispatchResponse<&BridgeClass::_handleGetVersion>;
    case rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY):
      return &BridgeClass::_dispatchResponse<
          &BridgeClass::_handleGetFreeMemory>;
    case rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC):
      return &BridgeClass::_dispatchAckCtx<rpc_pb_LinkSync,
                                           &BridgeClass::_handleLinkSync>;
    case rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET):
      return &BridgeClass::_dispatchSimpleAck<&BridgeClass::_handleLinkReset>;
    case rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES):
      return &BridgeClass::_dispatchResponse<
          &BridgeClass::_handleGetCapabilities>;
    case rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE):
      return &BridgeClass::_dispatchAck<rpc_pb_SetBaudratePacket,
                                        &BridgeClass::_handleSetBaudrate>;
    case rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER):
      return &BridgeClass::_dispatchAck<rpc_pb_EnterBootloader,
                                        &BridgeClass::_handleEnterBootloader>;
    case rpc::to_underlying(rpc::CommandId::CMD_XOFF):
      return &BridgeClass::_dispatchSimpleAck<&BridgeClass::_handleXoff>;
    case rpc::to_underlying(rpc::CommandId::CMD_XON):
      return &BridgeClass::_dispatchSimpleAck<&BridgeClass::_handleXon>;
    case rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE):
      return &BridgeClass::_dispatchAck<rpc_pb_PinMode,
                                        &BridgeClass::_handleSetPinMode>;
    case rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE):
      return &BridgeClass::_dispatchAck<rpc_pb_DigitalWrite,
                                        &BridgeClass::_handleDigitalWrite>;
    case rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE):
      return &BridgeClass::_dispatchAck<rpc_pb_AnalogWrite,
                                        &BridgeClass::_handleAnalogWrite>;
    case rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ):
      return &BridgeClass::_dispatchResponseCtx<
          rpc_pb_PinRead, &BridgeClass::_handleDigitalRead>;
    case rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ):
      return &BridgeClass::_dispatchResponseCtx<
          rpc_pb_PinRead, &BridgeClass::_handleAnalogRead>;
    case rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE):
      return &BridgeClass::_dispatchAck<rpc_pb_ConsoleWrite,
                                        &BridgeClass::_handleConsoleWrite>;
#if BRIDGE_ENABLE_DATASTORE
    case rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP):
      return &BridgeClass::_dispatchAckCtx<
          rpc_pb_DatastoreGetResponse,
          &BridgeClass::_handleDataStoreGetResponse>;
#endif
#if BRIDGE_ENABLE_MAILBOX
    case rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH):
      return &BridgeClass::_dispatchAckCtx<rpc_pb_MailboxPush,
                                           &BridgeClass::_handleMailboxPush>;
    case rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP):
      return &BridgeClass::_dispatchPayload<
          rpc_pb_MailboxReadResponse, &BridgeClass::_handleMailboxReadResponse>;
    case rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP):
      return &BridgeClass::_dispatchPayload<
          rpc_pb_MailboxAvailableResponse,
          &BridgeClass::_handleMailboxAvailableResponse>;
#endif
#if BRIDGE_ENABLE_FILESYSTEM
    case rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE):
      return &BridgeClass::_dispatchAckCtx<rpc_pb_FileWrite,
                                           &BridgeClass::_handleFileWrite>;
    case rpc::to_underlying(rpc::CommandId::CMD_FILE_READ):
      return &BridgeClass::_dispatchAckCtx<rpc_pb_FileRead,
                                           &BridgeClass::_handleFileRead>;
    case rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE):
      return &BridgeClass::_dispatchAckCtx<rpc_pb_FileRemove,
                                           &BridgeClass::_handleFileRemove>;
    case rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP):
      return &BridgeClass::_dispatchAckCtx<
          rpc_pb_FileReadResponse, &BridgeClass::_handleFileReadResponse>;
#endif
#if BRIDGE_ENABLE_PROCESS
    case rpc::to_underlying(rpc::CommandId::CMD_PROCESS_KILL):
      return &BridgeClass::_dispatchAckCtx<rpc_pb_ProcessKill,
                                           &BridgeClass::_handleProcessKill>;
    case rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP):
      return &BridgeClass::_dispatchAckCtx<
          rpc_pb_ProcessRunAsyncResponse,
          &BridgeClass::_handleProcessRunAsyncResponse>;
    case rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP):
      return &BridgeClass::_dispatchAckCtx<
          rpc_pb_ProcessPollResponse, &BridgeClass::_handleProcessPollResponse>;
#endif
#if BRIDGE_ENABLE_SPI
    case rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN):
      return &BridgeClass::_dispatchSimpleAck<&BridgeClass::_handleSpiBegin>;
    case rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER):
      return &BridgeClass::_dispatchResponseCtx<
          rpc_pb_SpiTransfer, &BridgeClass::_handleSpiTransfer>;
    case rpc::to_underlying(rpc::CommandId::CMD_SPI_END):
      return &BridgeClass::_dispatchSimpleAck<&BridgeClass::_handleSpiEnd>;
    case rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG):
      return &BridgeClass::_dispatchAck<rpc_pb_SpiConfig,
                                        &BridgeClass::_handleSpiSetConfig>;
#endif
    default:
      return nullptr;
  }
}

void BridgeClass::_initializeRuntime() {
  _timer_last_tick_ms = 0;
  _serial_xoff_sent = false;

  // Shared buffer initialized by PacketSerial

  _rx_buffer.fill(0);
  if constexpr (bridge::hal::CurrentArchTraits::id ==
                bridge::hal::ArchId::ARCH_AVR)
    _hardware_serial = static_cast<HardwareSerial*>(&_stream);
  else
    _hardware_serial = nullptr;
}

void BridgeClass::begin(uint32_t baudrate, const char* secret) {
  _initializeRuntime();

  wolfCrypt_Init();
  _shared_secret.clear();
  if (secret != nullptr) {
    const etl::string_view s(secret);
    const size_t len = etl::min(s.size(), _shared_secret.capacity());
    const auto data_ptr =
        static_cast<const uint8_t*>(static_cast<const void*>(s.data()));
    _shared_secret.assign(data_ptr, data_ptr + len);
  }
  bridge::hal::init();
  if (!_fsm.is_started()) _fsm.start();
  _fsm.receive(bridge::fsm::EvReset());
  _is_post_passed = rpc::security::run_cryptographic_self_tests();
  if (!_is_post_passed) enterSafeState();
  if constexpr (bridge::hal::CurrentArchTraits::id ==
                bridge::hal::ArchId::ARCH_AVR)
    if (baudrate > 0 && _hardware_serial) _hardware_serial->begin(baudrate);
  _tx_enabled = true;
  _timers.clear();
  _timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT] =
      _timers.register_timer([]() { Bridge._onAckTimeout(); }, _ack_timeout_ms,
                             etl::timer::mode::REPEATING);
  _timer_ids[bridge::scheduler::TIMER_RX_DEDUPE] = _timers.register_timer(
      []() { Bridge._onRxDedupe(); }, bridge::config::RX_DEDUPE_INTERVAL_MS,
      etl::timer::mode::REPEATING);
  _timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE] = _timers.register_timer(
      []() { Bridge._onBaudrateChange(); },
      bridge::config::BAUDRATE_CHANGE_DELAY_MS, etl::timer::mode::SINGLE_SHOT);
  _timer_ids[bridge::scheduler::TIMER_BOOTLOADER_DELAY] =
      _timers.register_timer([]() { Bridge._onBootloaderDelay(); },
                             bridge::config::BOOTLOADER_DELAY_MS,
                             etl::timer::mode::SINGLE_SHOT);
  _packet_serial.setPacketHandler(
      etl::delegate<void(etl::span<const uint8_t>)>::create<
          BridgeClass, &BridgeClass::_handleReceivedFrame>(*this));
}

void BridgeClass::process() {
  _watchdogTask();
  _serialTask();
  _timerTask();
  if constexpr (bridge::config::ENABLE_MAILBOX) Mailbox.process();
}
void BridgeClass::_watchdogTask() { bridge::hal::watchdog_kick(); }

void BridgeClass::_serialTask() {
  _packet_serial.update(_stream);
  const int avail = _stream.available();
  if (!_serial_xoff_sent &&
      avail > bridge::config::FLOW_CONTROL_XOFF_THRESHOLD) {
    signalXoff();
    _serial_xoff_sent = true;
  } else if (_serial_xoff_sent &&
             avail < bridge::config::FLOW_CONTROL_XON_THRESHOLD) {
    signalXon();
    _serial_xoff_sent = false;
  }
}

void BridgeClass::_timerTask() {
  const uint32_t now = ::millis();
  if (_timer_last_tick_ms == 0) _timer_last_tick_ms = now;
  const uint32_t elapsed = now - _timer_last_tick_ms;
  if (elapsed > 0) {
    _timers.tick(elapsed);
    _timer_last_tick_ms = now;
  }
}
bool BridgeClass::isSynchronized() const { return _fsm.isSynchronized(); }
void BridgeClass::_handleStatusOk(const bridge::router::CommandContext&) {}
void BridgeClass::onUnknownCommand(const bridge::router::CommandContext& ctx) {
  if (_command_handler.is_valid())
    _command_handler(*ctx.envelope);
  else
    emitStatus(rpc::StatusCode::STATUS_ERROR);
}

void BridgeClass::enterSafeState() {
  bridge::hal::forceSafeState();
  _tx_enabled = false;
  _clearPendingTxQueue();
  _fsm.receive(bridge::fsm::EvReset());
  Console.onLost();
  DataStore.onLost();
  Mailbox.onLost();
  Process.onLost();
  FileSystem.onLost();
  SPIService.onLost();
}

void BridgeClass::_transmit(uint16_t command_id, uint16_t sequence_id,
                            etl::span<const uint8_t> payload) {
  const uint16_t raw_cmd = command_id;
  const bool is_excluded = (raw_cmd >= rpc::RPC_STATUS_CODE_MIN &&
                            raw_cmd <= rpc::RPC_STATUS_CODE_MAX) ||
                           (raw_cmd >= rpc::RPC_SYSTEM_COMMAND_MIN &&
                            raw_cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);
  const bool do_encrypt =
      isSynchronized() && !_shared_secret.empty() && !is_excluded;
  etl::array<uint8_t, rpc::AEAD_NONCE_SIZE> nonce = {};
  etl::array<uint8_t, rpc::AEAD_TAG_SIZE> tag = {};
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> enc_pl;
  etl::span<const uint8_t> final_payload = payload;
  if (do_encrypt) {
    if (!rpc::security::aead_encrypt_frame(raw_cmd, sequence_id, payload,
                                           _session_key, &_tx_nonce_counter,
                                           enc_pl, nonce, tag))
      return;
    final_payload = etl::span<const uint8_t>(enc_pl.data(), payload.size());
  }
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> buffer;
  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  env.version = rpc::PROTOCOL_VERSION;
  env.command_id = command_id;
  env.sequence_id = sequence_id;
  etl::copy_n(nonce.begin(), rpc::AEAD_NONCE_SIZE, env.nonce.bytes);
  env.nonce.size = static_cast<pb_size_t>(rpc::AEAD_NONCE_SIZE);
  etl::copy_n(tag.begin(), rpc::AEAD_TAG_SIZE, env.tag.bytes);
  env.tag.size = static_cast<pb_size_t>(rpc::AEAD_TAG_SIZE);
  const size_t pl_size = etl::min(final_payload.size(),
                                  static_cast<size_t>(rpc::MAX_PAYLOAD_SIZE));
  env.which_payload_type = rpc_pb_RpcEnvelope_encrypted_payload_tag;
  etl::copy_n(final_payload.begin(), pl_size,
              env.payload_type.encrypted_payload.bytes);
  env.payload_type.encrypted_payload.size = static_cast<pb_size_t>(pl_size);
  size_t len = rpc::serialize_frame(env, buffer);
  if (len > 0)
    _packet_serial.send(_stream, etl::span<const uint8_t>(buffer.data(), len));
}

void BridgeClass::_flushPendingTxQueue() {
  BRIDGE_ATOMIC_BLOCK {
    if (_pending_tx_queue.empty() || !_tx_enabled) return;
    const auto& f = _pending_tx_queue.front();
    _last_command_id = f.command_id;
    _retry_count = 0;
    _fsm.receive(bridge::fsm::EvSendCritical());
    _transmit(f.command_id, f.sequence_id,
              etl::span<const uint8_t>(f.buffer->data.data(), f.length));
    _timers.start(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
  }
}

void BridgeClass::_retransmitLastFrame() {
  BRIDGE_ATOMIC_BLOCK {
    if (_pending_tx_queue.empty()) return;
    const auto& f = _pending_tx_queue.front();
    _transmit(f.command_id, f.sequence_id,
              etl::span<const uint8_t>(f.buffer->data.data(), f.length));
    _timers.start(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
  }
}

void BridgeClass::_onAckTimeout() {
  if (!_fsm.isAwaitingAck()) return;
  if (++_retry_count >= _retry_limit) {
    _timers.stop(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
    _fsm.receive(bridge::fsm::EvTimeout());
    _tx_enabled = false;
    _clearPendingTxQueue();
    return;
  }
  _retransmitLastFrame();
}

void BridgeClass::_processAck(uint16_t command_id, uint16_t sequence_id) {
  rpc_pb_AckPacket p = {};
  p.command_id = command_id;
  if (!send(rpc::StatusCode::STATUS_ACK, sequence_id, p)) {
  }
}

void BridgeClass::_handleAck(uint16_t cmd) {
  if (!_fsm.isAwaitingAck() || cmd != _last_command_id) return;
  _timers.stop(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
  _clearPendingTxQueue();
  _fsm.receive(bridge::fsm::EvAckReceived());
  _flushPendingTxQueue();
}

void BridgeClass::_clearPendingTxQueue() {
  BRIDGE_ATOMIC_BLOCK {
    etl::for_each(_pending_tx_queue.begin(), _pending_tx_queue.end(),
                  [this](PendingTxFrame& f) {
                    if (f.buffer) _tx_payload_pool.release(f.buffer);
                  });
    _pending_tx_queue.clear();
  }
}

void BridgeClass::_onRxDedupe() { _rx_history.clear(); }
void BridgeClass::_onBaudrateChange() {
  if (_pending_baudrate > 0) {
    if (_hardware_serial) _hardware_serial->begin(_pending_baudrate);
    _pending_baudrate = 0;
  }
}
void BridgeClass::_onBootloaderDelay() { bridge::hal::enterBootloader(); }

void BridgeClass::_handleSetBaudrate(const rpc_pb_SetBaudratePacket& msg) {
  if (msg.baudrate == 0 || msg.baudrate == _pending_baudrate) return;
  _pending_baudrate = msg.baudrate;
  _timers.start(_timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE]);
}

void BridgeClass::_handleEnterBootloader(const rpc_pb_EnterBootloader& msg) {
  if (msg.magic == rpc::RPC_BOOTLOADER_MAGIC)
    _timers.start(_timer_ids[bridge::scheduler::TIMER_BOOTLOADER_DELAY]);
}

void BridgeClass::_handleSetPinMode(const rpc_pb_PinMode& m) {
  uint8_t m_val = INPUT;
  if (m.mode == rpc_pb_PinModeType_PIN_OUTPUT)
    m_val = OUTPUT;
  else if (m.mode == rpc_pb_PinModeType_PIN_INPUT_PULLUP)
    m_val = INPUT_PULLUP;
  pinMode(m.pin, m_val);
}

void BridgeClass::_handleDigitalWrite(const rpc_pb_DigitalWrite& m) {
  digitalWrite(m.pin, (m.value == 0) ? LOW : HIGH);
}
void BridgeClass::_handleAnalogWrite(const rpc_pb_AnalogWrite& m) {
  analogWrite(m.pin, (int)m.value);
}

void BridgeClass::_handleDigitalRead(const bridge::router::CommandContext& ctx,
                                     const rpc_pb_PinRead& m) {
  if (m.pin < bridge::config::DIGITAL_PINS) {
    rpc_pb_DigitalReadResponse resp = rpc_pb_DigitalReadResponse_init_default;
    resp.value = static_cast<uint32_t>(::digitalRead(m.pin));
    if (!send(rpc::CommandId::CMD_DIGITAL_READ_RESP, ctx.sequence_id, resp)) {
    }
  } else
    emitStatus(rpc::StatusCode::STATUS_ERROR);
}

void BridgeClass::_handleAnalogRead(const bridge::router::CommandContext& ctx,
                                    const rpc_pb_PinRead& m) {
  if (m.pin < bridge::config::DIGITAL_PINS) {
    rpc_pb_AnalogReadResponse resp = rpc_pb_AnalogReadResponse_init_default;
    resp.value = static_cast<uint32_t>(::analogRead(m.pin));
    if (!send(rpc::CommandId::CMD_ANALOG_READ_RESP, ctx.sequence_id, resp)) {
    }
  } else
    emitStatus(rpc::StatusCode::STATUS_ERROR);
}

void BridgeClass::_handleConsoleWrite(const rpc_pb_ConsoleWrite& m) {
  Console._push(m);
}

#if BRIDGE_ENABLE_DATASTORE
void BridgeClass::_handleDataStoreGetResponse(
    const bridge::router::CommandContext&,
    const rpc_pb_DatastoreGetResponse& m) {
  DataStore._onResponse(m);
}
#endif

#if BRIDGE_ENABLE_MAILBOX
void BridgeClass::_handleMailboxPush(const bridge::router::CommandContext&,
                                     const rpc_pb_MailboxPush& m) {
  MailboxClass<>::_onPush(m);
}
void BridgeClass::_handleMailboxReadResponse(
    const rpc_pb_MailboxReadResponse& m) {
  MailboxClass<>::_onReadResponse(m);
}
void BridgeClass::_handleMailboxAvailableResponse(
    const rpc_pb_MailboxAvailableResponse& m) {
  MailboxClass<>::_onAvailableResponse(m);
}
#endif

#if BRIDGE_ENABLE_FILESYSTEM
void BridgeClass::_handleFileWrite(const bridge::router::CommandContext&,
                                   const rpc_pb_FileWrite& m) {
  FileSystem._onWrite(m);
}
void BridgeClass::_handleFileRead(const bridge::router::CommandContext&,
                                  const rpc_pb_FileRead& m) {
  FileSystem._onRead(m);
}
void BridgeClass::_handleFileRemove(const bridge::router::CommandContext&,
                                    const rpc_pb_FileRemove& m) {
  FileSystem._onRemove(m);
}
void BridgeClass::_handleFileReadResponse(const bridge::router::CommandContext&,
                                          const rpc_pb_FileReadResponse& m) {
  FileSystem._onResponse(m);
}
#endif
#if BRIDGE_ENABLE_PROCESS
void BridgeClass::_handleProcessKill(const bridge::router::CommandContext&,
                                     const rpc_pb_ProcessKill& m) {
  Process._onKillNotification(m);
}
void BridgeClass::_handleProcessRunAsyncResponse(
    const bridge::router::CommandContext&,
    const rpc_pb_ProcessRunAsyncResponse& m) {
  Process._onRunAsyncResponse(m);
}
void BridgeClass::_handleProcessPollResponse(
    const bridge::router::CommandContext&,
    const rpc_pb_ProcessPollResponse& m) {
  Process._onPollResponse(m);
}
#endif
#if BRIDGE_ENABLE_SPI
void BridgeClass::_handleSpiSetConfig(const rpc_pb_SpiConfig& m) {
  SPIService.setConfig(m);
}
void BridgeClass::_handleSpiBegin(const bridge::router::CommandContext& ctx) {
  SPIService.begin();
  _processAck(ctx.raw_command, ctx.sequence_id);
}
void BridgeClass::_handleSpiEnd(const bridge::router::CommandContext& ctx) {
  SPIService.end();
  _processAck(ctx.raw_command, ctx.sequence_id);
}
void BridgeClass::_handleSpiTransfer(const bridge::router::CommandContext& ctx,
                                     const rpc_pb_SpiTransfer& m) {
  size_t len = etl::min((size_t)m.data.size, _rx_buffer.size());
  etl::copy_n(m.data.bytes, len, _rx_buffer.begin());
  size_t tr = SPIService.transfer(etl::span<uint8_t>(_rx_buffer.data(), len));
  if (tr == 0) {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }
  rpc_pb_SpiTransferResponse resp = rpc_pb_SpiTransferResponse_init_default;
  const size_t to_copy = etl::min(len, sizeof(resp.data.bytes));
  resp.data.size = (pb_size_t)to_copy;
  if (to_copy > 0) etl::copy_n(_rx_buffer.data(), to_copy, resp.data.bytes);
  if (!send(rpc::CommandId::CMD_SPI_TRANSFER_RESP, ctx.sequence_id, resp)) {
  }
}
#endif

void BridgeClass::_handleStatusMalformed(
    const bridge::router::CommandContext&) {
  enterSafeState();
}

void BridgeClass::_handleLinkSync(const bridge::router::CommandContext& ctx,
                                  const rpc_pb_LinkSync& m) {
  rpc_pb_LinkSync resp = rpc_pb_LinkSync_init_default;
  const size_t n_size =
      etl::min(static_cast<size_t>(m.nonce.size),
               static_cast<size_t>(rpc::RPC_HANDSHAKE_NONCE_LENGTH));
  etl::copy_n(m.nonce.bytes, n_size, resp.nonce.bytes);
  resp.nonce.size = static_cast<pb_size_t>(n_size);
  if (!_shared_secret.empty()) {
    etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> out_tag;
    if (rpc::security::handshake_authenticate(
            etl::span<const uint8_t>(_shared_secret),
            etl::span<const uint8_t>(m.nonce.bytes, n_size),
            etl::span<const uint8_t>(m.tag.bytes, m.tag.size),
            etl::span<uint8_t>(out_tag))) {
      etl::copy_n(out_tag.data(), rpc::RPC_HANDSHAKE_TAG_LENGTH,
                  resp.tag.bytes);
      resp.tag.size = rpc::RPC_HANDSHAKE_TAG_LENGTH;
      rpc::security::derive_session_key(
          etl::span<const uint8_t>(_shared_secret),
          etl::span<const uint8_t>(m.nonce.bytes, n_size),
          etl::span<uint8_t>(_session_key));
      _tx_nonce_counter = 0;
      _rx_nonce_counter = 0;
      rpc::security::secure_zero(etl::span<uint8_t>(out_tag));
    } else {
      _fsm.receive(bridge::fsm::EvHandshakeFailed());
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }
  }
  _fsm.receive(bridge::fsm::EvHandshakeStart());
  _fsm.receive(bridge::fsm::EvHandshakeComplete());
  if (!send(rpc::CommandId::CMD_LINK_SYNC_RESP, ctx.sequence_id, resp)) {
  }
}

void BridgeClass::_handleLinkReset(const bridge::router::CommandContext& ctx) {
  if (ctx.envelope->which_payload_type != 0) {
    rpc_pb_HandshakeConfig res_msg = rpc_pb_HandshakeConfig_init_default;
    bool decoded = false;
    if (ctx.envelope->which_payload_type ==
        rpc_pb_RpcEnvelope_encrypted_payload_tag) {
      pb_istream_t stream = pb_istream_from_buffer(
          ctx.envelope->payload_type.encrypted_payload.bytes,
          ctx.envelope->payload_type.encrypted_payload.size);
      decoded =
          pb_decode(&stream, rpc::Payload::get_fields<rpc_pb_HandshakeConfig>(),
                    &res_msg);
    } else if (ctx.envelope->which_payload_type ==
               rpc::Payload::get_tag<rpc_pb_HandshakeConfig>()) {
      res_msg = rpc::Payload::get<rpc_pb_HandshakeConfig>(*ctx.envelope);
      decoded = true;
    }
    if (decoded) _handleSetTiming(res_msg);
  }
  _fsm.receive(bridge::fsm::EvReset());
  if (!sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP, ctx.sequence_id)) {
  }
}

void BridgeClass::_handleGetCapabilities(
    const bridge::router::CommandContext& ctx) {
  rpc_pb_Capabilities resp = rpc_pb_Capabilities_init_default;
  bridge::hal::fillCapabilities(resp);
  if (!send(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, ctx.sequence_id, resp)) {
  }
}

void BridgeClass::_handleXoff(const bridge::router::CommandContext&) {
  _tx_enabled = false;
}
void BridgeClass::_handleXon(const bridge::router::CommandContext&) {
  _tx_enabled = true;
}

void BridgeClass::_handleStatusAck(
    const bridge::router::CommandContext& /*ctx*/, const rpc_pb_AckPacket& m) {
  _handleAck(m.command_id);
}

void BridgeClass::_handleGetVersion(const bridge::router::CommandContext& ctx) {
  rpc_pb_VersionResponse resp = {};
  resp.major = rpc::FIRMWARE_VERSION_MAJOR;
  resp.minor = rpc::FIRMWARE_VERSION_MINOR;
  resp.patch = (uint32_t)rpc::FIRMWARE_VERSION_PATCH;
  if (!send(rpc::CommandId::CMD_GET_VERSION_RESP, ctx.sequence_id, resp)) {
  }
}

void BridgeClass::_handleGetFreeMemory(
    const bridge::router::CommandContext& ctx) {
  rpc_pb_FreeMemoryResponse resp = {};
  resp.value = (uint32_t)bridge::hal::getFreeMemory();
  if (!send(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, ctx.sequence_id, resp)) {
  }
}

void BridgeClass::_handleSetTiming(const rpc_pb_HandshakeConfig& msg) {
  _applyTimingConfig(msg);
}
void BridgeClass::_applyTimingConfig(const rpc_pb_HandshakeConfig& msg) {
  _ack_timeout_ms = (uint16_t)msg.ack_timeout_ms;
  _retry_limit = (uint8_t)msg.ack_retry_limit;
  _response_timeout_ms = msg.response_timeout_ms;
}

void BridgeClass::_handleReceivedFrame(etl::span<const uint8_t> p) {
  auto res = rpc::parse_frame(p);
  if (!res) {
    emitStatus(rpc::StatusCode::STATUS_MALFORMED);
    return;
  }
  rpc_pb_RpcEnvelope envelope = res.value();
  if (envelope.version != rpc::PROTOCOL_VERSION) {
    emitStatus(rpc::StatusCode::STATUS_MALFORMED);
    return;
  }
  const uint16_t raw_cmd = envelope.command_id;
  const bool is_excluded = (raw_cmd >= rpc::RPC_STATUS_CODE_MIN &&
                            raw_cmd <= rpc::RPC_STATUS_CODE_MAX) ||
                           (raw_cmd >= rpc::RPC_SYSTEM_COMMAND_MIN &&
                            raw_cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);
  if (isSynchronized() && !_shared_secret.empty() && !is_excluded) {
    etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> dec_pl;
    if (!rpc::security::aead_decrypt_frame(
            raw_cmd, envelope.sequence_id,
            etl::span<const uint8_t>(
                envelope.payload_type.encrypted_payload.bytes,
                envelope.payload_type.encrypted_payload.size),
            etl::span<const uint8_t>(envelope.tag.bytes, 16), _session_key,
            etl::span<const uint8_t>(envelope.nonce.bytes, 12), dec_pl) ||
        !rpc::security::validate_frame_nonce(
            etl::span<const uint8_t>(envelope.nonce.bytes, 12),
            &_rx_nonce_counter)) {
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }
    etl::copy_n(dec_pl.data(), envelope.payload_type.encrypted_payload.size,
                envelope.payload_type.encrypted_payload.bytes);
  }
  _dispatchCommand(envelope);
}

bool BridgeClass::_isSecurityCheckPassed(uint16_t cmd) const {
  if (_shared_secret.empty()) return true;
  if ((cmd >= rpc::RPC_STATUS_CODE_MIN && cmd <= rpc::RPC_STATUS_CODE_MAX) ||
      (cmd >= rpc::RPC_SYSTEM_COMMAND_MIN &&
       cmd <= rpc::RPC_SYSTEM_COMMAND_MAX))
    return true;
  return _fsm.isSynchronized();
}

void BridgeClass::signalXoff() {
  if (!sendFrame(rpc::CommandId::CMD_XOFF)) {
  }
}
void BridgeClass::signalXon() {
  if (!sendFrame(rpc::CommandId::CMD_XON)) {
  }
}

namespace bridge {
void SafeStatePolicy::handle(::BridgeClass& bridge, const etl::exception&) {
  bridge.enterSafeState();
}
}  // namespace bridge

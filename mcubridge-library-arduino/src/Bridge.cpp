#include "Bridge.h"

#include <etl/algorithm.h>
#include <etl/functional.h>
#include <etl/iterator.h>

#include "hal/ArchTraits.h"
#if defined(BRIDGE_HOST_TEST) && defined(BRIDGE_FAULT_INJECTION)
#include "BridgeFaultInjection.h"
#endif
#include "hal/progmem_compat.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"

BridgeClass Bridge(Serial);


namespace etl {
void __attribute__((weak)) __attribute__((unused)) handle_error(
    const etl::exception& e) {
  BridgeClass::ErrorPolicy::handle(Bridge, e);
}
}  // namespace etl

BridgeClass::BridgeClass(Stream& stream)
    : _stream(stream),
      _hardware_serial(nullptr),
      _command_handler(),
      _status_handler(),
      _last_command_id(0),
      _tx_sequence_id(0),
      _retry_count(0),
      _retry_limit(rpc::RPC_DEFAULT_RETRY_LIMIT),
      _ack_timeout_ms(rpc::RPC_DEFAULT_ACK_TIMEOUT_MS),
      _response_timeout_ms(rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS),
      _pending_baudrate(0),
      _consecutive_crc_errors(0),
      _last_parse_error(rpc::FrameError::NONE),
      _ps_rx_storage(),
      _ps_work_buffer(),
      _packet_serial(
          etl::span<uint8_t>(_ps_rx_storage.data(), _ps_rx_storage.size()),
          etl::span<uint8_t>(_ps_work_buffer.data(), _ps_work_buffer.size())),
      _shared_secret(),
      _session_key(),
      _tx_nonce_counter(0),
      _rx_nonce_counter(0),
      _fsm(),
      _watchdog_task(),
      _serial_task(),
      _timer_task(),
      _tasks(),
      _scheduler_policy(),
      _timers(),
      _timer_ids(),
      _transient_buffer(),
      _rx_storage(),
      _frame_parser(),
      _is_post_passed(false),
      _tx_enabled(true),
      _tx_payload_pool(),
      _pending_tx_queue(),
      _rx_history() {
}

#include <wolfssl/wolfcrypt/settings.h>
#include <wolfssl/wolfcrypt/types.h>

const etl::array<BridgeClass::DispatchHandler, rpc::RPC_MAX_COMMAND_ID>&
BridgeClass::_dispatchTable() {
  static constexpr etl::array<DispatchHandler, rpc::RPC_MAX_COMMAND_ID> table =
      [] {
        etl::array<DispatchHandler, rpc::RPC_MAX_COMMAND_ID> t = {};
        t[rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION)] =
            &BridgeClass::_handleGetVersion;
        t[rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY)] =
            &BridgeClass::_handleGetFreeMemory;
        t[rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC)] =
            &BridgeClass::_handleLinkSync;
        t[rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET)] =
            &BridgeClass::_handleLinkReset;
        t[rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES)] =
            &BridgeClass::_handleGetCapabilities;
        t[rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE)] =
            &BridgeClass::_handleSetBaudrateCommand;
        t[rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER)] =
            &BridgeClass::_handleEnterBootloaderCommand;
        t[rpc::to_underlying(rpc::CommandId::CMD_XOFF)] = &BridgeClass::_handleXoff;
        t[rpc::to_underlying(rpc::CommandId::CMD_XON)] = &BridgeClass::_handleXon;
        t[rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE)] =
            &BridgeClass::_handleSetPinModeCommand;
        t[rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE)] =
            &BridgeClass::_handleDigitalWriteCommand;
        t[rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE)] =
            &BridgeClass::_handleAnalogWriteCommand;
        t[rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ)] =
            &BridgeClass::_handleDigitalReadCommand;
        t[rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ)] =
            &BridgeClass::_handleAnalogReadCommand;
        t[rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE)] =
            &BridgeClass::_handleConsoleWriteCommand;
        t[rpc::to_underlying(rpc::StatusCode::STATUS_OK)] =
            &BridgeClass::_handleStatusOk;
        t[rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED)] =
            &BridgeClass::_handleStatusMalformed;
        t[rpc::to_underlying(rpc::StatusCode::STATUS_ACK)] =
            &BridgeClass::_handleStatusAck;
#if BRIDGE_ENABLE_DATASTORE
        t[rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP)] =
            &BridgeClass::_handleDataStoreGetResponseCommand;
#endif
#if BRIDGE_ENABLE_MAILBOX
        t[rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH)] =
            &BridgeClass::_handleMailboxPushCommand;
        t[rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP)] =
            &BridgeClass::_handleMailboxReadResponseCommand;
        t[rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP)] =
            &BridgeClass::_handleMailboxAvailableResponseCommand;
#endif
#if BRIDGE_ENABLE_FILESYSTEM
        t[rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE)] =
            &BridgeClass::_handleFileWriteCommand;
        t[rpc::to_underlying(rpc::CommandId::CMD_FILE_READ)] =
            &BridgeClass::_handleFileReadCommand;
        t[rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE)] =
            &BridgeClass::_handleFileRemoveCommand;
        t[rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP)] =
            &BridgeClass::_handleFileReadResponseCommand;
#endif
#if BRIDGE_ENABLE_PROCESS
        t[rpc::to_underlying(rpc::CommandId::CMD_PROCESS_KILL)] =
            &BridgeClass::_handleProcessKillCommand;
        t[rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP)] =
            &BridgeClass::_handleProcessRunAsyncResponseCommand;
        t[rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP)] =
            &BridgeClass::_handleProcessPollResponseCommand;
#endif
#if BRIDGE_ENABLE_SPI
        t[rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN)] =
            &BridgeClass::_handleSpiBegin;
        t[rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER)] =
            &BridgeClass::_handleSpiTransfer;
        t[rpc::to_underlying(rpc::CommandId::CMD_SPI_END)] =
            &BridgeClass::_handleSpiEnd;
        t[rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG)] =
            &BridgeClass::_handleSpiSetConfigCommand;
#endif
        return t;
      }();
  return table;
}

void BridgeClass::_initializeRuntime() {

  _tasks.clear();
  _serial_task.bind(*this);
  _timer_task.bind(*this);
  _tasks.push_back(&_watchdog_task);
  _tasks.push_back(&_serial_task);
  _tasks.push_back(&_timer_task);

  _rx_storage.fill(0);
  _ps_rx_storage.fill(0);
  _ps_work_buffer.fill(0);

  if constexpr (bridge::hal::CurrentArchTraits::id ==
                bridge::hal::ArchId::ARCH_AVR) {
    _hardware_serial = static_cast<HardwareSerial*>(&_stream);
  } else {
    _hardware_serial = nullptr;
  }
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
#if defined(BRIDGE_HOST_TEST) && defined(BRIDGE_FAULT_INJECTION)
  if (bridge::test::fault::consume(
          bridge::test::fault::FaultPoint::BRIDGE_FORCE_POST_FAIL)) {
    _is_post_passed = false;
  }
#endif
  if (!_is_post_passed) enterSafeState();

  if constexpr (bridge::hal::CurrentArchTraits::id ==
                bridge::hal::ArchId::ARCH_AVR) {
    if (baudrate > 0 && _hardware_serial) _hardware_serial->begin(baudrate);
  }

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
          BridgeClass, &BridgeClass::_onPacketReceived>(*this));
}

void BridgeClass::process() { (void)_scheduler_policy.schedule_tasks(_tasks); }

void BridgeClass::WatchdogTask::task_process_work() {
  bridge::hal::watchdog_kick();
}

void BridgeClass::SerialTask::task_process_work() {
  if (bridge == nullptr) return;
  bridge->_packet_serial.update(bridge->_stream);
  const int avail = bridge->_stream.available();
  if (!xoff_sent && avail > bridge::config::FLOW_CONTROL_XOFF_THRESHOLD) {
    bridge->signalXoff();
    xoff_sent = true;
  } else if (xoff_sent && avail < bridge::config::FLOW_CONTROL_XON_THRESHOLD) {  // GCOVR_EXCL_BR_LINE
    bridge->signalXon();
    xoff_sent = false;
  }
}

void BridgeClass::TimerTask::task_process_work() {
  if (bridge == nullptr) return;
  const uint32_t now = millis();
  if (last_tick_ms == 0) last_tick_ms = now;
  const uint32_t elapsed = now - last_tick_ms;
  if (elapsed > 0) {
    bridge->_timers.tick(elapsed);
    last_tick_ms = now;
  }
}

bool BridgeClass::isSynchronized() const { return _fsm.isSynchronized(); }

void BridgeClass::_dispatchCommand(const rpc::Frame& frame) {
  const uint16_t cmd_id =
      frame.header.command_id & ~rpc::RPC_CMD_FLAG_COMPRESSED;
  auto it = etl::find(_rx_history.begin(), _rx_history.end(),
                      frame.header.sequence_id);
  const bool is_duplicate = (it != _rx_history.end());
  const bridge::router::CommandContext ctx(
      &frame, cmd_id, frame.header.sequence_id, is_duplicate,
      rpc::requires_ack(cmd_id));
  if (!is_duplicate) {
    if (_rx_history.full()) _rx_history.pop();
    _rx_history.push(frame.header.sequence_id);
  }
  if (!_isSecurityCheckPassed(ctx.raw_command)) {
    (void)sendFrame(rpc::StatusCode::STATUS_ERROR, ctx.sequence_id);
    return;
  }
  const auto& dispatch_table = _dispatchTable();
  if (cmd_id < rpc::RPC_MAX_COMMAND_ID && dispatch_table[cmd_id] != nullptr) {
    (this->*dispatch_table[cmd_id])(ctx);  // GCOVR_EXCL_BR_LINE
  } else {
    onUnknownCommand(ctx);
  }
}

void BridgeClass::_handleStatusOk(const bridge::router::CommandContext& ctx) {
  (void)ctx;
}
void BridgeClass::onUnknownCommand(const bridge::router::CommandContext& ctx) {
  if (_command_handler.is_valid())
    _command_handler(*ctx.frame);
  else
    emitStatus(rpc::StatusCode::STATUS_ERROR);
}
void BridgeClass::enterSafeState() {
  bridge::hal::forceSafeState();
  _tx_enabled = false;
  _clearPendingTxQueue();
  _fsm.receive(bridge::fsm::EvReset());
  _notifyObservers(MsgBridgeLost());
}

void BridgeClass::emitStatus(rpc::StatusCode code,
                             etl::span<const uint8_t> pl) {
  (void)sendFrame(code, 0, pl);
}

void BridgeClass::emitStatus(rpc::StatusCode code, etl::string_view msg) {
  (void)sendFrame(code, 0,
                  etl::span<const uint8_t>(
                      reinterpret_cast<const uint8_t*>(msg.data()),
                      msg.length()));
}

void BridgeClass::emitStatus(rpc::StatusCode code,
                             const __FlashStringHelper* msg) {
  if (msg == nullptr) {
    (void)sendFrame(code);
    return;
  }
  constexpr size_t max_len = rpc::MAX_PAYLOAD_SIZE - 1U;
  etl::string<max_len> str;
  str.resize(max_len);
  bridge::hal::copy_string(str.data(), reinterpret_cast<const char*>(msg), max_len);
  str.resize(etl::strlen(str.data()));
  (void)sendFrame(code, 0, etl::span<const uint8_t>(reinterpret_cast<const uint8_t*>(str.data()), str.length()));
}

bool BridgeClass::sendFrame(rpc::StatusCode s, uint16_t seq,
                            etl::span<const uint8_t> p) {
  return _sendFrame(rpc::to_underlying(s), seq, p);
}
bool BridgeClass::sendFrame(rpc::CommandId c, uint16_t seq,
                            etl::span<const uint8_t> p) {
  return _sendFrame(rpc::to_underlying(c), seq, p);
}

void BridgeClass::_sendRawFrame(uint16_t command_id, uint16_t sequence_id,
                                etl::span<const uint8_t> payload) {
  const uint16_t raw_cmd = command_id & ~rpc::RPC_CMD_FLAG_COMPRESSED;
  const bool is_sync = isSynchronized();
  const bool has_secret = !_shared_secret.empty();
  const bool is_excluded = (raw_cmd >= rpc::RPC_STATUS_CODE_MIN &&
                            raw_cmd <= rpc::RPC_STATUS_CODE_MAX) ||
                           (raw_cmd >= rpc::RPC_SYSTEM_COMMAND_MIN &&
                            raw_cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);
  const bool do_encrypt = is_sync && has_secret && !is_excluded;

  rpc::Frame f = {};
  f.header = {rpc::PROTOCOL_VERSION, static_cast<uint16_t>(payload.size()),
              command_id, sequence_id};

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> encrypted_payload;

  if (do_encrypt) {
    ++_tx_nonce_counter;
    f.nonce.fill(0);
    constexpr etl::string_view mcu_prefix("MCU");
    etl::copy_n(mcu_prefix.begin(), 3, f.nonce.begin());
    etl::byte_stream_writer n_writer(f.nonce.data() + 4, 8, etl::endian::big);
    n_writer.write<uint64_t>(_tx_nonce_counter);

    etl::array<uint8_t, rpc::FRAME_HEADER_SIZE> header_buf;
    rpc::checksum::serialize_header(f.header, header_buf);

    if (rpc::security::aead_encrypt(encrypted_payload, f.tag, payload,
                                    _session_key, f.nonce, header_buf)) {
      f.payload =
          etl::span<const uint8_t>(encrypted_payload.data(), payload.size());
    } else {
      return;
    }
  } else {
    f.payload = payload;
    f.nonce.fill(0);
    f.tag.fill(0);
  }

  f.crc = rpc::checksum::compute(f);
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> buffer;
  size_t len = rpc::FrameParser::serialize(
      f, etl::span<uint8_t>(buffer.data(), buffer.size()));
#if defined(BRIDGE_HOST_TEST) && defined(BRIDGE_FAULT_INJECTION)
  if (bridge::test::fault::consume(
          bridge::test::fault::FaultPoint::BRIDGE_SERIALIZE_ZERO)) {
    len = 0;
  }
#endif
  if (len > 0)
    _packet_serial.send(_stream, etl::span<const uint8_t>(buffer.data(), len));
}

bool BridgeClass::_sendFrame(uint16_t cmd, uint16_t seq,
                             etl::span<const uint8_t> pl) {
  const bool is_system =
      (cmd >= rpc::RPC_STATUS_CODE_MIN && cmd <= rpc::RPC_STATUS_CODE_MAX) ||
      (cmd >= rpc::RPC_SYSTEM_COMMAND_MIN && cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);

  if (!_tx_enabled && !is_system) return false;
  if (is_reliable_cmd(cmd)) {
    BRIDGE_ATOMIC_BLOCK {
      if (_pending_tx_queue.full()) return false;
      TxPayloadBuffer* buf = _tx_payload_pool.allocate();
#if defined(BRIDGE_HOST_TEST) && defined(BRIDGE_FAULT_INJECTION)
      if (bridge::test::fault::consume(
              bridge::test::fault::FaultPoint::BRIDGE_POOL_ALLOC_FAIL)) {
        if (buf) _tx_payload_pool.release(buf);  // GCOVR_EXCL_BR_LINE
        buf = nullptr;
      }
#endif
      if (!buf) return false;
      etl::copy_n(pl.begin(), pl.size(), buf->data.begin());
      _pending_tx_queue.push_back({cmd, seq, buf, pl.size()});
    }
    if (!_fsm.isAwaitingAck()) _flushPendingTxQueue();
    return true;
  }
  _sendRawFrame(cmd, seq, pl);
  return true;
}

void BridgeClass::_flushPendingTxQueue() {
  BRIDGE_ATOMIC_BLOCK {
    if (!_tx_enabled || _pending_tx_queue.empty() || _fsm.isAwaitingAck())
      return;
    const auto& f = _pending_tx_queue.front();
    _sendRawFrame(f.command_id, f.sequence_id,
                  etl::span<const uint8_t>(f.buffer->data.data(), f.length));
    _retry_count = 0;
    _last_command_id = f.command_id;
    _timers.start(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
    _fsm.receive(bridge::fsm::EvSendCritical());
  }
}
void BridgeClass::_onAckTimeout() {
  if (!_fsm.isAwaitingAck()) return;
  if (++_retry_count >= _retry_limit) {
    _timers.stop(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
    // [SIL-2] FaultState::on_enter_state() calls hal::forceSafeState() on entry.
    // Disable TX and drain queue here (BridgeClass resources not accessible from FSM).
    _fsm.receive(bridge::fsm::EvTimeout());
    _tx_enabled = false;
    _clearPendingTxQueue();
    return;
  }
  _retransmitLastFrame();
}
void BridgeClass::_retransmitLastFrame() {
  BRIDGE_ATOMIC_BLOCK {
    if (_pending_tx_queue.empty()) return;
    const auto& f = _pending_tx_queue.front();
    _sendRawFrame(f.command_id, f.sequence_id,
                  etl::span<const uint8_t>(f.buffer->data.data(), f.length));
    _timers.start(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
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

void BridgeClass::_handleSetBaudrateCommand(
    const bridge::router::CommandContext& ctx) {
  if (ctx.is_duplicate) {
    (void)sendFrame(rpc::CommandId::CMD_SET_BAUDRATE_RESP, ctx.sequence_id);
    return;
  }
  _json_arena.reset();
  JsonDocument doc(&_json_arena);
  auto res = rpc::Payload::parse<rpc::payload::SetBaudratePacket>(*ctx.frame, doc);
  if (res) {
    _handleSetBaudrate(res.value());
    (void)sendFrame(rpc::CommandId::CMD_SET_BAUDRATE_RESP, ctx.sequence_id);
  } else
    (void)sendFrame(rpc::StatusCode::STATUS_ERROR, ctx.sequence_id);
}
void BridgeClass::_handleEnterBootloaderCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::EnterBootloader>(
      ctx, [this](const auto& m) { _handleEnterBootloader(m); });
}
void BridgeClass::_handleSetPinModeCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::PinMode>(ctx, [this](const auto& m) {
    if (bridge::hal::isValidPin(m.pin))
      ::pinMode(m.pin, m.mode);
    else
      emitStatus(rpc::StatusCode::STATUS_ERROR);
  });
}
void BridgeClass::_handleDigitalWriteCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::DigitalWrite>(ctx, [this](const auto& m) {
    if (bridge::hal::isValidPin(m.pin))
      ::digitalWrite(m.pin, m.value);
    else
      emitStatus(rpc::StatusCode::STATUS_ERROR);
  });
}
void BridgeClass::_handleAnalogWriteCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::AnalogWrite>(ctx, [this](const auto& m) {
    if (bridge::hal::isValidPin(m.pin))
      ::analogWrite(m.pin, m.value);
    else
      emitStatus(rpc::StatusCode::STATUS_ERROR);
  });
}
void BridgeClass::_handleDigitalReadCommand(
    const bridge::router::CommandContext& ctx) {
  _handlePinRead<rpc::payload::DigitalReadResponse>(
      ctx, rpc::CommandId::CMD_DIGITAL_READ_RESP, &bridge::hal::isValidPin,
      ::digitalRead);
}
void BridgeClass::_handleAnalogReadCommand(
    const bridge::router::CommandContext& ctx) {
  _handlePinRead<rpc::payload::AnalogReadResponse>(
      ctx, rpc::CommandId::CMD_ANALOG_READ_RESP, &bridge::hal::isValidPin,
      ::analogRead);
}
void BridgeClass::_handleConsoleWriteCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::ConsoleWrite>(
      ctx, [](const auto& m) { Console._push(m); });
}

#if BRIDGE_ENABLE_DATASTORE
void BridgeClass::_handleDataStoreGetResponseCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::DatastoreGetResponse>(
      ctx, [](const auto& m) { DataStore._onResponse(m); });
}
#endif
#if BRIDGE_ENABLE_MAILBOX
void BridgeClass::_handleMailboxPushCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::MailboxPush>(
      ctx, [](const auto& m) { Mailbox._onIncomingData(m); });
}
void BridgeClass::_handleMailboxReadResponseCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::MailboxReadResponse>(
      ctx, [](const auto& m) { Mailbox._onIncomingData(m); });
}
void BridgeClass::_handleMailboxAvailableResponseCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::MailboxAvailableResponse>(
      ctx, [](const auto& m) { Mailbox._onAvailableResponse(m); });
}
#endif
#if BRIDGE_ENABLE_FILESYSTEM
void BridgeClass::_handleFileWriteCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::FileWrite>(
      ctx, [](const auto& m) { FileSystem._onWrite(m); });
}
void BridgeClass::_handleFileReadCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::FileRead>(
      ctx, [](const auto& m) { FileSystem._onRead(m); });
}
void BridgeClass::_handleFileRemoveCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::FileRemove>(
      ctx, [](const auto& m) { FileSystem._onRemove(m); });
}
void BridgeClass::_handleFileReadResponseCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::FileReadResponse>(
      ctx, [](const auto& m) { FileSystem._onResponse(m); });
}
#endif
#if BRIDGE_ENABLE_PROCESS
void BridgeClass::_handleProcessKillCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::ProcessKill>(
      ctx, [](const auto& m) { Process._onKillNotification(m); });
}
void BridgeClass::_handleProcessRunAsyncResponseCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::ProcessRunAsyncResponse>(
      ctx, [](const auto& m) { Process._onRunAsyncResponse(m); });
}
void BridgeClass::_handleProcessPollResponseCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::ProcessPollResponse>(
      ctx, [](const auto& m) { Process._onPollResponse(m); });
}
#endif
#if BRIDGE_ENABLE_SPI
void BridgeClass::_handleSpiSetConfigCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::SpiConfig>(
      ctx, [](const auto& m) { SPIService.setConfig(m); });
}
#endif

void BridgeClass::_handleStatusMalformed(
    const bridge::router::CommandContext& ctx) {
  (void)ctx;
  enterSafeState();
}
void BridgeClass::_handleStatusAck(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::AckPacket>(
      ctx, [this](const auto& ack) { _handleAck(ack.command_id); });
}

void BridgeClass::_handleGetVersion(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc::payload::VersionResponse resp = {
        rpc::FIRMWARE_VERSION_MAJOR, rpc::FIRMWARE_VERSION_MINOR,
        (uint32_t)rpc::FIRMWARE_VERSION_PATCH};
    (void)send(rpc::CommandId::CMD_GET_VERSION_RESP, ctx.sequence_id, resp);
  });
}
void BridgeClass::_handleGetFreeMemory(
    const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc::payload::FreeMemoryResponse resp = {
        (uint32_t)bridge::hal::getFreeMemory()};
    (void)send(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, ctx.sequence_id, resp);
  });
}

void BridgeClass::_handleLinkSync(const bridge::router::CommandContext& ctx) {
  _json_arena.reset();
  JsonDocument doc(&_json_arena);
  auto res = rpc::Payload::parse<rpc::payload::LinkSync>(*ctx.frame, doc);
  if (!res) {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }
  const auto& msg = res.value();
  rpc::payload::LinkSync resp = {};
  etl::copy_n(msg.nonce.begin(), rpc::RPC_HANDSHAKE_NONCE_LENGTH, resp.nonce.begin());

  if (!_shared_secret.empty()) {
    etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> handshake_key;
    rpc::security::hkdf_sha256(
        etl::span<uint8_t>(handshake_key),
        etl::span<const uint8_t>(_shared_secret),
        etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_SALT),
        etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH));

    etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> full_tag;
    Hmac hmac_engine;
    wc_HmacSetKey(&hmac_engine, WC_SHA256, handshake_key.data(),
                  rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH);
    wc_HmacUpdate(&hmac_engine, msg.nonce.data(), rpc::RPC_HANDSHAKE_NONCE_LENGTH);
    wc_HmacFinal(&hmac_engine, full_tag.data());

    const bool tag_ok = rpc::security::timing_safe_equal(
            etl::span<const uint8_t>(full_tag.data(), rpc::RPC_HANDSHAKE_TAG_LENGTH),
            etl::span<const uint8_t>(msg.tag.data(), rpc::RPC_HANDSHAKE_TAG_LENGTH));
    if (!tag_ok) {
      _fsm.receive(bridge::fsm::EvHandshakeFailed());
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }

    etl::copy_n(full_tag.begin(), rpc::RPC_HANDSHAKE_TAG_LENGTH, resp.tag.begin());
    rpc::security::hkdf_sha256(
        etl::span<uint8_t>(_session_key), etl::span<const uint8_t>(_shared_secret),
        etl::span<const uint8_t>(msg.nonce),
        etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_SESSION));
    _tx_nonce_counter = 0;
    _rx_nonce_counter = 0;
    rpc::security::secure_zero(handshake_key);
    rpc::security::secure_zero(full_tag);
  }

  _fsm.receive(bridge::fsm::EvHandshakeStart());
  _fsm.receive(bridge::fsm::EvHandshakeComplete());
  _tx_enabled = true;
  (void)send(rpc::CommandId::CMD_LINK_SYNC_RESP, ctx.sequence_id, resp);
  _notifyObservers(MsgBridgeSynchronized());
}

void BridgeClass::_handleLinkReset(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::HandshakeConfig>(
      ctx, [this, &ctx](const auto& msg) {
        _handleSetTiming(msg);
        enterSafeState();
        (void)sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP, ctx.sequence_id);
      });
}
void BridgeClass::_handleGetCapabilities(
    const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc::payload::Capabilities resp = {};
    resp.ver = rpc::PROTOCOL_VERSION;
    resp.arch = bridge::hal::getArchId();
    resp.feat = bridge::hal::getCapabilities();
    bridge::hal::getPinCounts(resp.dig, resp.ana);
    (void)send(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, ctx.sequence_id,
               resp);
  });
}

void BridgeClass::_handleXoff(const bridge::router::CommandContext& ctx) {
  (void)ctx;
  _tx_enabled = false;
}
void BridgeClass::_handleXon(const bridge::router::CommandContext& ctx) {
  (void)ctx;
  _tx_enabled = true;
  _flushPendingTxQueue();
}
void BridgeClass::_handleSetBaudrate(
    const rpc::payload::SetBaudratePacket& msg) {
  if (msg.baudrate == 0 || msg.baudrate == _pending_baudrate) return;
  _pending_baudrate = msg.baudrate;
  _timers.start(_timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE]);
}
void BridgeClass::_handleSetTiming(const rpc::payload::HandshakeConfig& msg) {
  if (msg.ack_timeout_ms > 0) {
    _ack_timeout_ms = msg.ack_timeout_ms;
    _timers.set_period(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT],
                       _ack_timeout_ms);
  }
  if (msg.response_timeout_ms > 0)
    _response_timeout_ms = msg.response_timeout_ms;
}
void BridgeClass::_handleEnterBootloader(
    const rpc::payload::EnterBootloader& msg) {
  if (msg.magic == rpc::RPC_BOOTLOADER_MAGIC) {
    this->flushStream();
    _timers.start(_timer_ids[bridge::scheduler::TIMER_BOOTLOADER_DELAY]);
  }
}
void BridgeClass::_onBootloaderDelay() { bridge::hal::enterBootloader(); }

void BridgeClass::_handleSpiBegin(const bridge::router::CommandContext& ctx) {
  (void)ctx;
  SPIService.begin();
  (void)send(rpc::StatusCode::STATUS_ACK, ctx.sequence_id,
             rpc::payload::AckPacket{ctx.raw_command});
}
void BridgeClass::_handleSpiEnd(const bridge::router::CommandContext& ctx) {
  (void)ctx;
  SPIService.end();
  (void)send(rpc::StatusCode::STATUS_ACK, ctx.sequence_id,
             rpc::payload::AckPacket{ctx.raw_command});
}
void BridgeClass::_handleSpiTransfer(
    const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    _json_arena.reset();
    JsonDocument doc(&_json_arena);
    auto res = rpc::Payload::parse<rpc::payload::SpiTransfer>(*ctx.frame, doc);
    if (res) {
      size_t len = etl::min(res->data.size(), _rx_storage.size());
      etl::copy_n(res->data.begin(), len, _rx_storage.begin());
      size_t tr =
          SPIService.transfer(etl::span<uint8_t>(_rx_storage.data(), len));
      if (tr == 0) {
        emitStatus(rpc::StatusCode::STATUS_ERROR);
        return;
      }
      rpc::payload::SpiTransferResponse resp = {};
      resp.data = etl::span<const uint8_t>(_rx_storage.data(), len);
      (void)send(rpc::CommandId::CMD_SPI_TRANSFER_RESP, ctx.sequence_id, resp);
    }
  });
}

void BridgeClass::_handleReceivedFrame(etl::span<const uint8_t> p) {
  auto res = _frame_parser.parse(p);
  if (!res) {
    _last_parse_error = res.error();
    emitStatus(rpc::StatusCode::STATUS_MALFORMED);
    return;
  }
  rpc::Frame frame = res.value();
  const uint16_t raw_cmd =
      frame.header.command_id & ~rpc::RPC_CMD_FLAG_COMPRESSED;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> dec_pl;

  const bool is_excluded = (raw_cmd >= rpc::RPC_STATUS_CODE_MIN &&
                            raw_cmd <= rpc::RPC_STATUS_CODE_MAX) ||
                           (raw_cmd >= rpc::RPC_SYSTEM_COMMAND_MIN &&
                            raw_cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);

  if (isSynchronized() && !_shared_secret.empty() && !is_excluded) {
    etl::array<uint8_t, rpc::FRAME_HEADER_SIZE> h_buf;
    rpc::checksum::serialize_header(frame.header, h_buf);

    if (rpc::security::aead_decrypt(dec_pl, frame.payload, frame.tag,
                                    _session_key, frame.nonce, h_buf)) {
      uint64_t counter = 0;
      etl::byte_stream_reader n_reader(frame.nonce.data() + 4, 8,
                                       etl::endian::big);
#if defined(BRIDGE_HOST_TEST) && defined(BRIDGE_FAULT_INJECTION)
      const bool force_nonce_fail = bridge::test::fault::consume(
          bridge::test::fault::FaultPoint::BRIDGE_NONCE_READ_FAIL);
#else
      constexpr bool force_nonce_fail = false;
#endif
      if (!force_nonce_fail) {
        if (auto c_opt = n_reader.read<uint64_t>()) counter = *c_opt;
      }
      if (counter <= _rx_nonce_counter) {
        emitStatus(rpc::StatusCode::STATUS_ERROR);
        return;
      }
      _rx_nonce_counter = counter;
      frame.payload =
          etl::span<const uint8_t>(dec_pl.data(), frame.payload.size());
    } else {
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }
  }
  rpc::Frame eff;
  auto dec = _decompressFrame(frame, eff);
  if (!dec) {
    _last_parse_error = dec.error();
    emitStatus(rpc::StatusCode::STATUS_MALFORMED);
    return;
  }
  _dispatchCommand(eff);
}

void BridgeClass::_onPacketReceived(etl::span<const uint8_t> p) {
  _handleReceivedFrame(p);
}

etl::expected<void, rpc::FrameError> BridgeClass::_decompressFrame(
    const rpc::Frame& in, rpc::Frame& out) {
  out.header = in.header;
  out.nonce = in.nonce;
  out.tag = in.tag;
  if (!is_compressed_cmd(in.header.command_id)) {
    out.payload = in.payload;
    return {};
  }
  _rx_storage.fill(0);
  size_t d_len = ::rle::decode(
      in.payload, etl::span<uint8_t>(_rx_storage.data(), _rx_storage.size()));
  if (d_len == 0 && !in.payload.empty())
    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);
  out.payload = etl::span<uint8_t>(_rx_storage.data(), d_len);
  out.header.payload_length = (uint16_t)d_len;
  return {};
}

void BridgeClass::_applyTimingConfig(const rpc::payload::HandshakeConfig& msg) {
  _handleSetTiming(msg);
}
bool BridgeClass::_isSecurityCheckPassed(uint16_t cmd) const {
  if (_shared_secret.empty()) return true;
  if ((cmd >= rpc::RPC_STATUS_CODE_MIN && cmd <= rpc::RPC_STATUS_CODE_MAX) ||
      (cmd >= rpc::RPC_SYSTEM_COMMAND_MIN &&
       cmd <= rpc::RPC_SYSTEM_COMMAND_MAX))
    return true;
  return _fsm.isSynchronized();
}

void BridgeClass::signalXoff() { (void)sendFrame(rpc::CommandId::CMD_XOFF); }
void BridgeClass::signalXon() { (void)sendFrame(rpc::CommandId::CMD_XON); }

namespace bridge {
void SafeStatePolicy::handle(::BridgeClass& bridge, const etl::exception& e) {
  (void)e;
  bridge.enterSafeState();
}
}  // namespace bridge

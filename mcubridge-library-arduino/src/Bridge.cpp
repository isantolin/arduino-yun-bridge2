#include "Bridge.h"

#include <etl/algorithm.h>
#include <etl/functional.h>
#include <etl/iterator.h>

#include "hal/ArchTraits.h"
#if defined(BRIDGE_HOST_TEST) && defined(BRIDGE_FAULT_INJECTION)
#include "BridgeFaultInjection.h"
#endif
#include <wolfssl/wolfcrypt/settings.h>
#include <wolfssl/wolfcrypt/types.h>

#include "hal/progmem_compat.h"
#include "protocol/rle.h"
#include "security/security.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"

// [SIL-2] Global Bridge instance using default Serial
BridgeClass Bridge(Serial);

namespace etl {
// [SIL-2] Custom error handler to ensure fail-safe state on ETL exceptions
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
      _retry_limit(rpc::DEFAULT_RETRY_LIMIT),
      _ack_timeout_ms(rpc::DEFAULT_ACK_TIMEOUT_MS),
      _response_timeout_ms(rpc::HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS),
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
      _is_post_passed(false),
      _tx_enabled(true),
      _tx_payload_pool(),
      _pending_tx_queue(),
      _rx_history() {}

// [MEM-SAVE] Static wrappers to bridge between the static jump table and member
// functions. This avoids the 4-8 byte overhead of member function pointers on
// some architectures.
#define DISPATCH_WRAPPER(method)                                  \
  [](BridgeClass& b, const bridge::router::CommandContext& ctx) { \
    b.method(ctx);                                                \
  }

void BridgeClass::_dispatchCommand(const rpc::Frame& frame) {
  const uint16_t cmd_id =
      frame.envelope.command_id & ~rpc::CMD_FLAG_COMPRESSED;
  auto it = etl::find(_rx_history.begin(), _rx_history.end(),
                      frame.envelope.sequence_id);
  const bool is_duplicate = (it != _rx_history.end());
  const bridge::router::CommandContext ctx(
      &frame, cmd_id, frame.envelope.sequence_id, is_duplicate,
      rpc::requires_ack(cmd_id));

  if (!is_duplicate) {
    if (_rx_history.full()) _rx_history.pop();
    _rx_history.push(frame.envelope.sequence_id);
  }

  if (!_isSecurityCheckPassed(ctx.raw_command)) {
    (void)sendFrame(rpc::StatusCode::STATUS_ERROR, ctx.sequence_id);
    return;
  }

  auto handler = _getHandler(cmd_id);
  if (handler) {
    handler(*this, ctx);
  } else {
    onUnknownCommand(ctx);
  }
}

BridgeClass::DispatchHandler BridgeClass::_getHandler(uint16_t command_id) {
  // [SIL-2] [MEM-SAVE] Static O(1) jump table in Flash using lambda
  // initialization.
  static constexpr etl::array<DispatchHandler, rpc::MAX_COMMAND_ID> table =
      []() {
        etl::array<DispatchHandler, rpc::MAX_COMMAND_ID> t = {};
        t.fill(nullptr);

        // Status Handlers (0x30 - 0x3F)
        t[rpc::to_underlying(rpc::StatusCode::STATUS_OK)] =
            DISPATCH_WRAPPER(_handleStatusOk);
        t[rpc::to_underlying(rpc::StatusCode::STATUS_ACK)] =
            DISPATCH_WRAPPER(_handleStatusAck);
        t[rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED)] =
            DISPATCH_WRAPPER(_handleStatusMalformed);

        // System Commands (0x40 - 0x4F)
        t[rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION)] =
            DISPATCH_WRAPPER(_handleGetVersion);
        t[rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY)] =
            DISPATCH_WRAPPER(_handleGetFreeMemory);
        t[rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC)] =
            DISPATCH_WRAPPER(_handleLinkSync);
        t[rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET)] =
            DISPATCH_WRAPPER(_handleLinkReset);
        t[rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES)] =
            DISPATCH_WRAPPER(_handleGetCapabilities);
        t[rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE)] =
            DISPATCH_WRAPPER(_handleSetBaudrateCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER)] =
            DISPATCH_WRAPPER(_handleEnterBootloaderCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_XOFF)] =
            DISPATCH_WRAPPER(_handleXoff);
        t[rpc::to_underlying(rpc::CommandId::CMD_XON)] =
            DISPATCH_WRAPPER(_handleXon);

        // GPIO Commands (0x50 - 0x5F)
        t[rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE)] =
            DISPATCH_WRAPPER(_handleSetPinModeCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE)] =
            DISPATCH_WRAPPER(_handleDigitalWriteCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE)] =
            DISPATCH_WRAPPER(_handleAnalogWriteCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ)] =
            DISPATCH_WRAPPER(_handleDigitalReadCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ)] =
            DISPATCH_WRAPPER(_handleAnalogReadCommand);

        // Console Commands (0x60)
        t[rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE)] =
            DISPATCH_WRAPPER(_handleConsoleWriteCommand);

#if BRIDGE_ENABLE_DATASTORE
        t[rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP)] =
            DISPATCH_WRAPPER(_handleDataStoreGetResponseCommand);
#endif

#if BRIDGE_ENABLE_MAILBOX
        t[rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH)] =
            DISPATCH_WRAPPER(_handleMailboxPushCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP)] =
            DISPATCH_WRAPPER(_handleMailboxReadResponseCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP)] =
            DISPATCH_WRAPPER(_handleMailboxAvailableResponseCommand);
#endif

#if BRIDGE_ENABLE_FILESYSTEM
        t[rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE)] =
            DISPATCH_WRAPPER(_handleFileWriteCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_FILE_READ)] =
            DISPATCH_WRAPPER(_handleFileReadCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE)] =
            DISPATCH_WRAPPER(_handleFileRemoveCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP)] =
            DISPATCH_WRAPPER(_handleFileReadResponseCommand);
#endif

#if BRIDGE_ENABLE_PROCESS
        t[rpc::to_underlying(rpc::CommandId::CMD_PROCESS_KILL)] =
            DISPATCH_WRAPPER(_handleProcessKillCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP)] =
            DISPATCH_WRAPPER(_handleProcessRunAsyncResponseCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP)] =
            DISPATCH_WRAPPER(_handleProcessPollResponseCommand);
#endif

#if BRIDGE_ENABLE_SPI
        t[rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN)] =
            DISPATCH_WRAPPER(_handleSpiBegin);
        t[rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER)] =
            DISPATCH_WRAPPER(_handleSpiTransfer);
        t[rpc::to_underlying(rpc::CommandId::CMD_SPI_END)] =
            DISPATCH_WRAPPER(_handleSpiEnd);
        t[rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG)] =
            DISPATCH_WRAPPER(_handleSpiSetConfigCommand);
#endif

        return t;
      }();
  return (command_id < rpc::MAX_COMMAND_ID) ? table[command_id] : nullptr;
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
  } else if (xoff_sent &&
             avail < bridge::config::
                         FLOW_CONTROL_XON_THRESHOLD) {  // GCOVR_EXCL_BR_LINE
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
  (void)sendFrame(
      code, 0,
      etl::span<const uint8_t>(reinterpret_cast<const uint8_t*>(msg.data()),
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
  bridge::hal::copy_string(str.data(), reinterpret_cast<const char*>(msg),
                           max_len);
  str.resize(etl::strlen(str.data()));
  (void)sendFrame(
      code, 0,
      etl::span<const uint8_t>(reinterpret_cast<const uint8_t*>(str.data()),
                               str.length()));
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
  const uint16_t raw_cmd = command_id & ~rpc::CMD_FLAG_COMPRESSED;
  const bool is_excluded = (raw_cmd >= rpc::STATUS_CODE_MIN &&
                            raw_cmd <= rpc::STATUS_CODE_MAX) ||
                           (raw_cmd >= rpc::SYSTEM_COMMAND_MIN &&
                            raw_cmd <= rpc::SYSTEM_COMMAND_MAX);
  const bool do_encrypt =
      isSynchronized() && !_shared_secret.empty() && !is_excluded;

  etl::array<uint8_t, rpc::AEAD_NONCE_SIZE> nonce = {};
  etl::array<uint8_t, rpc::AEAD_TAG_SIZE> tag = {};
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> enc_pl;
  etl::span<const uint8_t> final_payload = payload;

  if (do_encrypt) {
    if (!rpc::security::aead_encrypt_frame(raw_cmd, sequence_id, payload,
                                           _session_key, _tx_nonce_counter,
                                           enc_pl, nonce, tag))
      return;
    final_payload = etl::span<const uint8_t>(enc_pl.data(), payload.size());
  }

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> buffer;
  size_t len = rpc::FrameBuilder::build(buffer, command_id, sequence_id,
                                        final_payload, nonce, tag);

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
  const uint16_t raw_cmd = cmd & ~rpc::CMD_FLAG_COMPRESSED;
  const bool is_system = (raw_cmd >= rpc::STATUS_CODE_MIN &&
                          raw_cmd <= rpc::STATUS_CODE_MAX) ||
                         (raw_cmd >= rpc::SYSTEM_COMMAND_MIN &&
                          raw_cmd <= rpc::SYSTEM_COMMAND_MAX);

  if (!_tx_enabled && !is_system) return false;
  if (is_reliable_cmd(cmd)) {
    BRIDGE_ATOMIC_BLOCK {
      if (_pending_tx_queue.full()) return false;
      auto* buf = _tx_payload_pool.allocate();
      if (!buf) return false;
      etl::copy_n(pl.data(), pl.size(), buf->data.data());
      _pending_tx_queue.push_back({cmd, seq, buf, pl.size()});
      if (!_fsm.isAwaitingAck()) _flushPendingTxQueue();
    }
    return true;
  }
  _sendRawFrame(cmd, seq, pl);
  return true;
}

void BridgeClass::_flushPendingTxQueue() {
  BRIDGE_ATOMIC_BLOCK {
    if (_pending_tx_queue.empty()) return;
    if (!_tx_enabled) return;
    const auto& f = _pending_tx_queue.front();
    _last_command_id = f.command_id;
    _retry_count = 0;
    _fsm.receive(bridge::fsm::EvSendCritical());
    _sendRawFrame(f.command_id, f.sequence_id,
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
  // [MEM-SAVE] Replaced manual ACK building with centralized helper.
  rpc_pb_AckPacket p = rpc_pb_AckPacket_init_default;
  p.command_id = command_id;
  (void)send(rpc::StatusCode::STATUS_ACK, sequence_id, rpc_pb_AckPacket_fields, p);
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
  _withResponse(ctx, [this, &ctx]() {
    auto res = rpc::Payload::parse<rpc_pb_SetBaudratePacket>(*ctx.frame, rpc_pb_SetBaudratePacket_fields);
    if (res) {
      _handleSetBaudrate(*res);
      (void)sendFrame(rpc::CommandId::CMD_SET_BAUDRATE_RESP, ctx.sequence_id);
    } else
      (void)sendFrame(rpc::StatusCode::STATUS_ERROR, ctx.sequence_id);
  });
}

void BridgeClass::_handleEnterBootloaderCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_EnterBootloader>(ctx, rpc_pb_EnterBootloader_fields, [this](const auto& m) { _handleEnterBootloader(m); });
}

void BridgeClass::_handleSetPinModeCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_PinMode>(ctx, rpc_pb_PinMode_fields, [](const auto& m) {
    // Direct literal mapping
    uint8_t m_val = INPUT;
    if (m.mode == 1)
      m_val = OUTPUT;
    else if (m.mode == 2)
      m_val = INPUT_PULLUP;
    pinMode(m.pin, m_val);
  });
}

void BridgeClass::_handleDigitalWriteCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_DigitalWrite>(ctx, rpc_pb_DigitalWrite_fields, [](const auto& m) {
    digitalWrite(m.pin, (m.value == 0) ? LOW : HIGH);
  });
}

void BridgeClass::_handleAnalogWriteCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_AnalogWrite>(ctx, rpc_pb_AnalogWrite_fields, [](const auto& m) {
    analogWrite(m.pin, (int)m.value);
  });
}

void BridgeClass::_handleDigitalReadCommand(
    const bridge::router::CommandContext& ctx) {
  _handlePinRead<rpc_pb_DigitalReadResponse>(
      ctx, rpc::CommandId::CMD_DIGITAL_READ_RESP, [](uint8_t pin) { return digitalRead(pin); },
      rpc_pb_DigitalReadResponse_fields);
}

void BridgeClass::_handleAnalogReadCommand(
    const bridge::router::CommandContext& ctx) {
  _handlePinRead<rpc_pb_AnalogReadResponse>(
      ctx, rpc::CommandId::CMD_ANALOG_READ_RESP, [](uint8_t pin) { return analogRead(pin); },
      rpc_pb_AnalogReadResponse_fields);
}

void BridgeClass::_handleConsoleWriteCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_ConsoleWrite>(ctx, rpc_pb_ConsoleWrite_fields, [](const auto& m) { Console._push(m); });
}

#if BRIDGE_ENABLE_DATASTORE
void BridgeClass::_handleDataStoreGetResponseCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_DatastoreGetResponse>(ctx, rpc_pb_DatastoreGetResponse_fields, [](const auto& m) { DataStore._onResponse(m); });
}
#endif

#if BRIDGE_ENABLE_MAILBOX
void BridgeClass::_handleMailboxPushCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_MailboxPush>(ctx, rpc_pb_MailboxPush_fields, [](const auto& m) { Mailbox._onIncomingData(m); });
}
void BridgeClass::_handleMailboxReadResponseCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_MailboxReadResponse>(ctx, rpc_pb_MailboxReadResponse_fields, [](const auto& m) { Mailbox._onIncomingData(m); });
}
void BridgeClass::_handleMailboxAvailableResponseCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_MailboxAvailableResponse>(ctx, rpc_pb_MailboxAvailableResponse_fields, [](const auto& m) { Mailbox._onAvailableResponse(m); });
}
#endif

#if BRIDGE_ENABLE_FILESYSTEM
void BridgeClass::_handleFileWriteCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_FileWrite>(ctx, rpc_pb_FileWrite_fields, [](const auto& m) { FileSystem._onWrite(m); });
}
void BridgeClass::_handleFileReadCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_FileRead>(ctx, rpc_pb_FileRead_fields, [](const auto& m) { FileSystem._onRead(m); });
}
void BridgeClass::_handleFileRemoveCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_FileRemove>(ctx, rpc_pb_FileRemove_fields, [](const auto& m) { FileSystem._onRemove(m); });
}
void BridgeClass::_handleFileReadResponseCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_FileReadResponse>(ctx, rpc_pb_FileReadResponse_fields, [](const auto& m) { FileSystem._onResponse(m); });
}
#endif

#if BRIDGE_ENABLE_PROCESS
void BridgeClass::_handleProcessKillCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_ProcessKill>(ctx, rpc_pb_ProcessKill_fields, [](const auto& m) { Process._onKillNotification(m); });
}
void BridgeClass::_handleProcessRunAsyncResponseCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_ProcessRunAsyncResponse>(ctx, rpc_pb_ProcessRunAsyncResponse_fields, [](const auto& m) { Process._onRunAsyncResponse(m); });
}
void BridgeClass::_handleProcessPollResponseCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_ProcessPollResponse>(ctx, rpc_pb_ProcessPollResponse_fields, [](const auto& m) { Process._onPollResponse(m); });
}
#endif

#if BRIDGE_ENABLE_SPI
void BridgeClass::_handleSpiSetConfigCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc_pb_SpiConfig>(ctx, rpc_pb_SpiConfig_fields, [](const auto& m) { SPIService.setConfig(m); });
}
#endif

void BridgeClass::_handleStatusAck(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc_pb_AckPacket>(ctx, rpc_pb_AckPacket_fields, [this](const auto& ack) { _handleAck(ack.command_id); });
}

void BridgeClass::_handleGetVersion(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc_pb_VersionResponse resp = rpc_pb_VersionResponse_init_default;
    resp.major = rpc::FIRMWARE_VERSION_MAJOR;
    resp.minor = rpc::FIRMWARE_VERSION_MINOR;
    resp.patch = (uint32_t)rpc::FIRMWARE_VERSION_PATCH;
    (void)send(rpc::CommandId::CMD_GET_VERSION_RESP, ctx.sequence_id, rpc_pb_VersionResponse_fields, resp);
  });
}

void BridgeClass::_handleGetFreeMemory(
    const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc_pb_FreeMemoryResponse resp = rpc_pb_FreeMemoryResponse_init_default;
    resp.value = (uint32_t)bridge::hal::getFreeMemory();
    (void)send(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, ctx.sequence_id, rpc_pb_FreeMemoryResponse_fields, resp);
  });
}

void BridgeClass::_handleLinkSync(const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc_pb_LinkSync>(*ctx.frame, rpc_pb_LinkSync_fields);
  if (!res) {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }
  const auto& msg = *res;
  rpc_pb_LinkSync resp = rpc_pb_LinkSync_init_default;
  const size_t n_size =
      etl::min(static_cast<size_t>(msg.nonce.size),
               static_cast<size_t>(rpc::HANDSHAKE_NONCE_LENGTH));
  etl::copy_n(msg.nonce.bytes, n_size, resp.nonce.bytes);
  resp.nonce.size = static_cast<pb_size_t>(n_size);

  if (!_shared_secret.empty()) {
    etl::array<uint8_t, rpc::HANDSHAKE_HKDF_OUTPUT_LENGTH> out_tag;
    const bool tag_ok = rpc::security::handshake_authenticate_raw(
        _shared_secret.data(), _shared_secret.size(), msg.nonce.bytes,
        n_size, msg.tag.bytes, msg.tag.size, out_tag.data());

    if (!tag_ok) {
      _fsm.receive(bridge::fsm::EvHandshakeFailed());
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }

    etl::copy_n(out_tag.data(), rpc::HANDSHAKE_TAG_LENGTH,
                resp.tag.bytes);
    resp.tag.size = rpc::HANDSHAKE_TAG_LENGTH;
    rpc::security::derive_session_key_raw(
        _shared_secret.data(), _shared_secret.size(), msg.nonce.bytes,
        n_size, _session_key.data());
    _tx_nonce_counter = 0;
    _rx_nonce_counter = 0;
    rpc::security::secure_zero(etl::span<uint8_t>(out_tag));
  }

  _fsm.receive(bridge::fsm::EvHandshakeStart());
  _fsm.receive(bridge::fsm::EvHandshakeComplete());
  _tx_enabled = true;
  (void)send(rpc::CommandId::CMD_LINK_SYNC_RESP, ctx.sequence_id, rpc_pb_LinkSync_fields, resp);
  _notifyObservers(MsgBridgeSynchronized());
}

void BridgeClass::_handleLinkReset(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc_pb_HandshakeConfig>(ctx, rpc_pb_HandshakeConfig_fields, [this, &ctx](const auto& msg) {
        _applyTimingConfig(msg);
        enterSafeState();
        (void)sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP, ctx.sequence_id);
      });
}

void BridgeClass::_handleGetCapabilities(
    const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc_pb_Capabilities resp = rpc_pb_Capabilities_init_default;
    resp.ver = rpc::PROTOCOL_VERSION;
    resp.arch = bridge::hal::getArchId();
    bridge::hal::fillCapabilities(resp);
    uint8_t dig = 0, ana = 0;
    bridge::hal::getPinCounts(dig, ana);
    resp.dig = dig;
    resp.ana = ana;
    (void)send(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, ctx.sequence_id, rpc_pb_Capabilities_fields, resp);
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
    const rpc_pb_SetBaudratePacket& msg) {
  if (msg.baudrate == 0 || msg.baudrate == _pending_baudrate)
    return;
  _pending_baudrate = msg.baudrate;
  _timers.start(_timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE]);
}

void BridgeClass::_applyTimingConfig(const rpc_pb_HandshakeConfig& msg) {
  if (msg.ack_timeout_ms > 0) {
    _ack_timeout_ms = msg.ack_timeout_ms;
    _timers.set_period(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT],
                       _ack_timeout_ms);
  }
  if (msg.response_timeout_ms > 0)
    _response_timeout_ms = msg.response_timeout_ms;
}

void BridgeClass::_handleEnterBootloader(
    const rpc_pb_EnterBootloader& msg) {
  if (msg.magic == rpc::BOOTLOADER_MAGIC) {
    this->flushStream();
    _timers.start(_timer_ids[bridge::scheduler::TIMER_BOOTLOADER_DELAY]);
  }
}

void BridgeClass::_onBootloaderDelay() { bridge::hal::enterBootloader(); }

void BridgeClass::_handleReceivedFrame(etl::span<const uint8_t> p) {
  auto res = rpc::FrameParser::parse(p);
  if (!res) {
    _last_parse_error = res.error();
    emitStatus(rpc::StatusCode::STATUS_MALFORMED);
    return;
  }
  rpc::Frame frame = res.value();
  const uint16_t cmd_id = frame.envelope.command_id;
  const uint16_t raw_cmd = cmd_id & ~rpc::CMD_FLAG_COMPRESSED;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> dec_pl;

  const bool is_excluded = (raw_cmd >= rpc::STATUS_CODE_MIN &&
                            raw_cmd <= rpc::STATUS_CODE_MAX) ||
                           (raw_cmd >= rpc::SYSTEM_COMMAND_MIN &&
                            raw_cmd <= rpc::SYSTEM_COMMAND_MAX);

  if (isSynchronized() && !_shared_secret.empty() && !is_excluded) {
    if (!rpc::security::aead_decrypt_frame(
            raw_cmd, frame.envelope.sequence_id, frame.payload(),
            etl::span<const uint8_t>(frame.envelope.tag.bytes, 16),
            _session_key,
            etl::span<const uint8_t>(frame.envelope.nonce.bytes, 12),
            dec_pl) ||
        !rpc::security::validate_frame_nonce(
            etl::span<const uint8_t>(frame.envelope.nonce.bytes, 12),
            _rx_nonce_counter)) {
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }
    // Update envelope with decrypted payload
    etl::copy_n(dec_pl.data(), frame.envelope.payload.size,
                frame.envelope.payload.bytes);
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
  out = in;
  if (!rpc::is_compressed(in.envelope.command_id)) return {};

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> decomp_pl;
  size_t decomp_size = rle::decode(in.payload(), decomp_pl);
  if (decomp_size == 0)
    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);

  etl::copy_n(decomp_pl.data(), decomp_size, _transient_buffer.data());
  etl::copy_n(decomp_pl.data(), decomp_size, out.envelope.payload.bytes);
  out.envelope.payload.size = static_cast<pb_size_t>(decomp_size);
  return {};
}

bool BridgeClass::_isSecurityCheckPassed(uint16_t cmd) const {
  if (_shared_secret.empty()) return true;
  if ((cmd >= rpc::STATUS_CODE_MIN && cmd <= rpc::STATUS_CODE_MAX) ||
      (cmd >= rpc::SYSTEM_COMMAND_MIN &&
       cmd <= rpc::SYSTEM_COMMAND_MAX))
    return true;
  return _fsm.isSynchronized();
}

void BridgeClass::signalXoff() { (void)sendFrame(rpc::CommandId::CMD_XOFF); }
void BridgeClass::signalXon() { (void)sendFrame(rpc::CommandId::CMD_XON); }

void BridgeClass::_handleSpiBegin(const bridge::router::CommandContext& ctx) {
  SPIService.begin();
  _processAck(ctx.raw_command, ctx.sequence_id);
}
void BridgeClass::_handleSpiEnd(const bridge::router::CommandContext& ctx) {
  SPIService.end();
  _processAck(ctx.raw_command, ctx.sequence_id);
}
void BridgeClass::_handleSpiTransfer(
    const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    auto res = rpc::Payload::parse<rpc_pb_SpiTransfer>(*ctx.frame, rpc_pb_SpiTransfer_fields);
    if (res) {
      size_t len = etl::min((size_t)res->data.size, _rx_storage.size());
      etl::copy_n(res->data.bytes, len, _rx_storage.begin());
      size_t tr =
          SPIService.transfer(etl::span<uint8_t>(_rx_storage.data(), len));
      if (tr == 0) {
        emitStatus(rpc::StatusCode::STATUS_ERROR);
        return;
      }
      rpc_pb_SpiTransferResponse resp = rpc_pb_SpiTransferResponse_init_default;
      rpc::payload::copy_to_pb_bytes(resp.data, _rx_storage.data(), len);
      (void)send(rpc::CommandId::CMD_SPI_TRANSFER_RESP, ctx.sequence_id, rpc_pb_SpiTransferResponse_fields, resp);
    } else
      emitStatus(rpc::StatusCode::STATUS_ERROR);
  });
}

void BridgeClass::_handleStatusMalformed(
    const bridge::router::CommandContext& ctx) {
  (void)ctx;
  enterSafeState();
}

namespace bridge {
void SafeStatePolicy::handle(::BridgeClass& bridge, const etl::exception& e) {
  (void)e;
  bridge.enterSafeState();
}
}  // namespace bridge

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
      _is_post_passed(false),
      _tx_enabled(true),
      _fs_read_handler(),
      _mailbox_available_count(0),
      _spi_initialized(false),
      _spi_settings(4000000, MSBFIRST, SPI_MODE0),
      _tx_payload_pool(),
      _pending_tx_queue(),
      _rx_history() {}

#define DISPATCH_WRAPPER(method)                                  \
  [](BridgeClass& b, const bridge::router::CommandContext& ctx) { \
    b.method(ctx);                                                \
  }

void BridgeClass::_dispatchCommand(const rpc_pb_RpcEnvelope& envelope) {
  const uint16_t cmd_id = envelope.command_id & ~rpc::RPC_CMD_FLAG_COMPRESSED;
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

using DispatchHandler = void (*)(BridgeClass&,
                                 const bridge::router::CommandContext&);

void BridgeClass::_handleStatusOk(const bridge::router::CommandContext& ctx) {
  (void)ctx;
}
void BridgeClass::_handleStatusAck(const bridge::router::CommandContext& ctx) {
  _handleAck(ctx.envelope->command_id);
}
void BridgeClass::_handleStatusMalformed(
    const bridge::router::CommandContext& ctx) {
  (void)ctx;
  enterSafeState();
}

void BridgeClass::_handleGetVersion(const bridge::router::CommandContext& ctx) {
  rpc::payload::VersionResponse resp = {};
  resp.major = rpc::FIRMWARE_VERSION_MAJOR;
  resp.minor = rpc::FIRMWARE_VERSION_MINOR;
  resp.patch = (uint32_t)rpc::FIRMWARE_VERSION_PATCH;
  (void)send(rpc::CommandId::CMD_GET_VERSION_RESP, ctx.sequence_id, resp);
}

void BridgeClass::_handleGetFreeMemory(
    const bridge::router::CommandContext& ctx) {
  rpc::payload::FreeMemoryResponse resp = {};
  resp.value = (uint32_t)bridge::hal::getFreeMemory();
  (void)send(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, ctx.sequence_id, resp);
}

void BridgeClass::_handleLinkSync(const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::LinkSync>(*ctx.envelope);
  if (!res) {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }
  const auto& msg = res.value();
  rpc::payload::LinkSync resp = {};
  const size_t n_size =
      etl::min(static_cast<size_t>(msg.nonce.size),
               static_cast<size_t>(rpc::RPC_HANDSHAKE_NONCE_LENGTH));
  etl::copy_n(msg.nonce.bytes, n_size, resp.nonce.bytes);
  resp.nonce.size = static_cast<pb_size_t>(n_size);

  if (!_shared_secret.empty()) {
    etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> out_tag;
    const bool tag_ok = rpc::security::handshake_authenticate_raw(
        _shared_secret.data(), _shared_secret.size(), msg.nonce.bytes, n_size,
        msg.tag.bytes, msg.tag.size, out_tag.data());
    if (!tag_ok) {
      _fsm.receive(bridge::fsm::EvHandshakeFailed());
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }
    etl::copy_n(out_tag.data(), rpc::RPC_HANDSHAKE_TAG_LENGTH, resp.tag.bytes);
    resp.tag.size = rpc::RPC_HANDSHAKE_TAG_LENGTH;
    rpc::security::derive_session_key_raw(
        _shared_secret.data(), _shared_secret.size(), msg.nonce.bytes, n_size,
        _session_key.data());
    _tx_nonce_counter = 0;
    _rx_nonce_counter = 0;
    rpc::security::secure_zero(etl::span<uint8_t>(out_tag));
  }
  _fsm.receive(bridge::fsm::EvHandshakeStart());
  _fsm.receive(bridge::fsm::EvHandshakeComplete());
  _tx_enabled = true;
  (void)send(rpc::CommandId::CMD_LINK_SYNC_RESP, ctx.sequence_id, resp);
  _notifyObservers(MsgBridgeSynchronized());
}

void BridgeClass::_handleLinkReset(const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::HandshakeConfig>(*ctx.envelope);
  if (res) {
    _applyTimingConfig(res.value());
    enterSafeState();
    (void)sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP, ctx.sequence_id);
  }
}

void BridgeClass::_handleGetCapabilities(
    const bridge::router::CommandContext& ctx) {
  rpc::payload::Capabilities resp = {};
  resp.ver = rpc::PROTOCOL_VERSION;
  resp.arch = bridge::hal::getArchId();
  bridge::hal::fillCapabilities(resp);
  uint8_t dig = 0, ana = 0;
  bridge::hal::getPinCounts(dig, ana);
  resp.dig = dig;
  resp.ana = ana;
  (void)send(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, ctx.sequence_id, resp);
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

void BridgeClass::_handleSetPinModeCommand(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::PinMode>(*ctx.envelope);
  if (res) {
    uint8_t m_val = INPUT;
    if (res->mode == 1)
      m_val = OUTPUT;
    else if (res->mode == 2)
      m_val = INPUT_PULLUP;
    pinMode(res->pin, m_val);
    _processAck(ctx.raw_command, ctx.sequence_id);
  }
}

void BridgeClass::_handleDigitalWriteCommand(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::DigitalWrite>(*ctx.envelope);
  if (res) {
    digitalWrite(res->pin, (res->value == 0) ? LOW : HIGH);
    _processAck(ctx.raw_command, ctx.sequence_id);
  }
}

void BridgeClass::_handleAnalogWriteCommand(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::AnalogWrite>(*ctx.envelope);
  if (res) {
    analogWrite(res->pin, (int)res->value);
    _processAck(ctx.raw_command, ctx.sequence_id);
  }
}

void BridgeClass::_handleDigitalReadCommand(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::PinRead>(*ctx.envelope);
  if (res) {
    rpc::payload::DigitalReadResponse resp = {};
    resp.value = digitalRead(res->pin) ? 1 : 0;
    (void)send(rpc::CommandId::CMD_DIGITAL_READ_RESP, ctx.sequence_id, resp);
  }
}

void BridgeClass::_handleAnalogReadCommand(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::PinRead>(*ctx.envelope);
  if (res) {
    rpc::payload::AnalogReadResponse resp = {};
    resp.value = analogRead(res->pin);
    (void)send(rpc::CommandId::CMD_ANALOG_READ_RESP, ctx.sequence_id, resp);
  }
}

void BridgeClass::_handleConsoleWriteCommand(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::ConsoleWrite>(*ctx.envelope);
  if (res) {
    const size_t to_write = etl::min(static_cast<size_t>(res->data.size),
                                     _console_rx_buffer.available());
    for (size_t i = 0; i < to_write; ++i)
      _console_rx_buffer.push(res->data.bytes[i]);
    _processAck(ctx.raw_command, ctx.sequence_id);
  }
}

void BridgeClass::_handleDataStoreGetResponseCommand(
    const bridge::router::CommandContext& ctx) {
  auto res =
      rpc::Payload::parse<rpc::payload::DatastoreGetResponse>(*ctx.envelope);
  if (res) {
    if (!_pending_datastore_gets.empty()) {
      const auto pending = _pending_datastore_gets.front();
      _pending_datastore_gets.pop();
      if (pending.handler.is_valid()) {
        pending.handler(
            etl::string_view(pending.key.data()),
            etl::span<const uint8_t>(res->value.bytes, res->value.size));
      }
    }
  }
}

void BridgeClass::_handleMailboxPushCommand(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::MailboxPush>(*ctx.envelope);
  if (res) {
    _mailbox_rx_buffer.clear();
    _mailbox_rx_buffer.insert(_mailbox_rx_buffer.end(), res->data.bytes,
                              res->data.bytes + res->data.size);
    _processAck(ctx.raw_command, ctx.sequence_id);
  }
}

void BridgeClass::_handleMailboxReadResponseCommand(
    const bridge::router::CommandContext& ctx) {
  auto res =
      rpc::Payload::parse<rpc::payload::MailboxReadResponse>(*ctx.envelope);
  if (res) {
    _mailbox_rx_buffer.clear();
    _mailbox_rx_buffer.insert(_mailbox_rx_buffer.end(), res->content.bytes,
                              res->content.bytes + res->content.size);
  }
}

void BridgeClass::_handleMailboxAvailableResponseCommand(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::MailboxAvailableResponse>(
      *ctx.envelope);
  if (res) {
    _mailbox_available_count = res->count;
  }
}

void BridgeClass::_handleFileWriteCommand(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::FileWrite>(*ctx.envelope);
  if (res) {
    auto ok = bridge::hal::writeFile(
        etl::string_view(res->path),
        etl::span<const uint8_t>(res->data.bytes, res->data.size));
    (void)sendFrame(
        ok ? rpc::StatusCode::STATUS_OK : rpc::StatusCode::STATUS_ERROR,
        ctx.sequence_id);
  }
}

void BridgeClass::_handleFileReadCommand(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::FileRead>(*ctx.envelope);
  if (res) {
    size_t offset = 0;
    etl::array<uint8_t, 64> buffer;
    const etl::string_view path(res->path);
    using bridge::etl_ext::CounterIterator;
    (void)etl::find_if(
        CounterIterator<uint16_t>(0U),
        CounterIterator(bridge::config::FILE_MAX_READ_CHUNKS), [&](uint16_t) {
          auto read_res = bridge::hal::readFile(path, offset, buffer);
          if (!read_res) return true;
          rpc::payload::FileReadResponse resp = {};
          const size_t to_copy =
              etl::min(read_res->size(), sizeof(resp.content.bytes));
          resp.content.size = (pb_size_t)to_copy;
          etl::copy_n(read_res->data(), to_copy, resp.content.bytes);
          (void)send(rpc::CommandId::CMD_FILE_READ_RESP, 0, resp);
          if (read_res->size() < buffer.size()) return true;
          offset += read_res->size();
          return false;
        });
  }
}

void BridgeClass::_handleFileRemoveCommand(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::FileRemove>(*ctx.envelope);
  if (res) {
    auto ok = bridge::hal::removeFile(etl::string_view(res->path));
    (void)sendFrame(
        ok ? rpc::StatusCode::STATUS_OK : rpc::StatusCode::STATUS_ERROR,
        ctx.sequence_id);
  }
}

void BridgeClass::_handleFileReadResponseCommand(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::FileReadResponse>(*ctx.envelope);
  if (res) {
    if (_fs_read_handler.is_valid())
      _fs_read_handler(
          etl::span<const uint8_t>(res->content.bytes, res->content.size));
  }
}

void BridgeClass::_handleProcessRunAsyncResponseCommand(
    const bridge::router::CommandContext& ctx) {
  auto res =
      rpc::Payload::parse<rpc::payload::ProcessRunAsyncResponse>(*ctx.envelope);
  if (res) {
    if (!_pending_process_runs.empty()) {
      auto p = _pending_process_runs.front();
      _pending_process_runs.pop();
      if (p.handler.is_valid()) p.handler(res->pid);
    }
  }
}

void BridgeClass::_handleProcessPollResponseCommand(
    const bridge::router::CommandContext& ctx) {
  auto res =
      rpc::Payload::parse<rpc::payload::ProcessPollResponse>(*ctx.envelope);
  if (res) {
    if (!_pending_process_polls.empty()) {
      auto p = _pending_process_polls.front();
      _pending_process_polls.pop();
      if (p.handler.is_valid())
        p.handler(static_cast<rpc::StatusCode>(res->status),
                  static_cast<uint16_t>(res->exit_code),
                  etl::span<const uint8_t>(res->stdout_data.bytes,
                                           res->stdout_data.size),
                  etl::span<const uint8_t>(res->stderr_data.bytes,
                                           res->stderr_data.size));
    }
  }
}

void BridgeClass::_handleProcessKillCommand(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::ProcessKill>(*ctx.envelope);
  if (res) {
    bridge::hal::killProcess(res->pid);
  }
}

void BridgeClass::_handleSpiBegin(const bridge::router::CommandContext& ctx) {
  spiBegin();
  _processAck(ctx.raw_command, ctx.sequence_id);
}
void BridgeClass::_handleSpiEnd(const bridge::router::CommandContext& ctx) {
  spiEnd();
  _processAck(ctx.raw_command, ctx.sequence_id);
}
void BridgeClass::_handleSpiTransfer(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::SpiTransfer>(*ctx.envelope);
  if (res) {
    size_t len = etl::min((size_t)res->data.size, _rx_storage.size());
    etl::copy_n(res->data.bytes, len, _rx_storage.begin());
    size_t tr = spiTransfer(etl::span<uint8_t>(_rx_storage.data(), len));
    if (tr == 0) {
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }
    rpc::payload::SpiTransferResponse resp = {};
    const size_t to_copy = etl::min(len, sizeof(resp.data.bytes));
    resp.data.size = (pb_size_t)to_copy;
    etl::copy_n(_rx_storage.data(), to_copy, resp.data.bytes);
    (void)send(rpc::CommandId::CMD_SPI_TRANSFER_RESP, ctx.sequence_id, resp);
  }
}
void BridgeClass::_handleSpiSetConfigCommand(
    const bridge::router::CommandContext& ctx) {
  auto res = rpc::Payload::parse<rpc::payload::SpiConfig>(*ctx.envelope);
  if (res) {
    spiSetConfig(res.value());
    _processAck(ctx.raw_command, ctx.sequence_id);
  }
}

BridgeClass::DispatchHandler BridgeClass::_getHandler(uint16_t command_id) {
  static constexpr etl::array<DispatchHandler, rpc::RPC_MAX_COMMAND_ID> table =
      []() {
        etl::array<DispatchHandler, rpc::RPC_MAX_COMMAND_ID> t = {};
        t.fill(nullptr);
        t[rpc::to_underlying(rpc::StatusCode::STATUS_OK)] =
            DISPATCH_WRAPPER(_handleStatusOk);
        t[rpc::to_underlying(rpc::StatusCode::STATUS_ACK)] =
            DISPATCH_WRAPPER(_handleStatusAck);
        t[rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED)] =
            DISPATCH_WRAPPER(_handleStatusMalformed);
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
        t[rpc::to_underlying(rpc::CommandId::CMD_XOFF)] =
            DISPATCH_WRAPPER(_handleXoff);
        t[rpc::to_underlying(rpc::CommandId::CMD_XON)] =
            DISPATCH_WRAPPER(_handleXon);
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
        t[rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE)] =
            DISPATCH_WRAPPER(_handleConsoleWriteCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP)] =
            DISPATCH_WRAPPER(_handleDataStoreGetResponseCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH)] =
            DISPATCH_WRAPPER(_handleMailboxPushCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP)] =
            DISPATCH_WRAPPER(_handleMailboxReadResponseCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP)] =
            DISPATCH_WRAPPER(_handleMailboxAvailableResponseCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE)] =
            DISPATCH_WRAPPER(_handleFileWriteCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_FILE_READ)] =
            DISPATCH_WRAPPER(_handleFileReadCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE)] =
            DISPATCH_WRAPPER(_handleFileRemoveCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP)] =
            DISPATCH_WRAPPER(_handleFileReadResponseCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_PROCESS_KILL)] =
            DISPATCH_WRAPPER(_handleProcessKillCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP)] =
            DISPATCH_WRAPPER(_handleProcessRunAsyncResponseCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP)] =
            DISPATCH_WRAPPER(_handleProcessPollResponseCommand);
        t[rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN)] =
            DISPATCH_WRAPPER(_handleSpiBegin);
        t[rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER)] =
            DISPATCH_WRAPPER(_handleSpiTransfer);
        t[rpc::to_underlying(rpc::CommandId::CMD_SPI_END)] =
            DISPATCH_WRAPPER(_handleSpiEnd);
        t[rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG)] =
            DISPATCH_WRAPPER(_handleSpiSetConfigCommand);
        return t;
      }();
  return (command_id < rpc::RPC_MAX_COMMAND_ID) ? table[command_id] : nullptr;
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
          BridgeClass, &BridgeClass::_onPacketReceived>(*this));
}

void BridgeClass::process() { (void)_scheduler_policy.schedule_tasks(_tasks); }
void BridgeClass::WatchdogTask::task_process_work() {
  bridge::hal::watchdog_kick();
}
void BridgeClass::SerialTask::task_process_work() {
  if (bridge == nullptr) return;
  bridge->consoleWrite(nullptr, 0);  // Trigger console flush if needed
  bridge->_packet_serial.update(bridge->_stream);
  const int avail = bridge->_stream.available();
  if (!xoff_sent && avail > bridge::config::FLOW_CONTROL_XOFF_THRESHOLD) {
    bridge->signalXoff();
    xoff_sent = true;
  } else if (xoff_sent && avail < bridge::config::FLOW_CONTROL_XON_THRESHOLD) {
    bridge->signalXon();
    xoff_sent = false;
  }
}
void BridgeClass::TimerTask::task_process_work() {
  if (bridge == nullptr) return;
  const uint32_t now = ::millis();
  if (last_tick_ms == 0) last_tick_ms = now;
  const uint32_t elapsed = now - last_tick_ms;
  if (elapsed > 0) {
    bridge->_timers.tick(elapsed);
    last_tick_ms = now;
  }
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
  rpc_pb_GenericResponse resp = rpc_pb_GenericResponse_init_default;
  const size_t to_copy = etl::min(msg.size(), sizeof(resp.message) - 1U);
  if (to_copy > 0U) etl::copy_n(msg.begin(), to_copy, resp.message);
  resp.message[to_copy] = '\0';
  (void)send(code, 0, resp);
}
void BridgeClass::emitStatus(rpc::StatusCode code,
                             const __FlashStringHelper* msg) {
  if (msg == nullptr) {
    (void)sendFrame(code);
    return;
  }
  constexpr size_t max_len = 63U;
  etl::string<max_len> str;
  str.resize(max_len);
  bridge::hal::copy_string(str.data(), reinterpret_cast<const char*>(msg),
                           max_len);
  str.resize(etl::strlen(str.data()));
  rpc_pb_GenericResponse resp = rpc_pb_GenericResponse_init_default;
  const size_t to_copy =
      etl::min(static_cast<size_t>(str.length()), sizeof(resp.message) - 1U);
  if (to_copy > 0U) etl::copy_n(str.begin(), to_copy, resp.message);
  resp.message[to_copy] = '\0';
  (void)send(code, 0, resp);
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
  etl::copy_n(final_payload.begin(), pl_size, env.payload.bytes);
  env.payload.size = static_cast<pb_size_t>(pl_size);
  size_t len = rpc::serialize_frame(env, buffer);
  if (len > 0)
    _packet_serial.send(_stream, etl::span<const uint8_t>(buffer.data(), len));
}

bool BridgeClass::_sendFrame(uint16_t cmd, uint16_t seq,
                             etl::span<const uint8_t> pl) {
  const uint16_t raw_cmd = cmd & ~rpc::RPC_CMD_FLAG_COMPRESSED;
  const bool is_system = (raw_cmd >= rpc::RPC_STATUS_CODE_MIN &&
                          raw_cmd <= rpc::RPC_STATUS_CODE_MAX) ||
                         (raw_cmd >= rpc::RPC_SYSTEM_COMMAND_MIN &&
                          raw_cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);
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
  rpc::payload::AckPacket p;
  p.command_id = command_id;
  (void)send(rpc::StatusCode::STATUS_ACK, sequence_id, p);
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

void BridgeClass::datastorePut(etl::string_view key,
                               etl::span<const uint8_t> value) {
  rpc::payload::DatastorePut p = {};
  const size_t k_copy = etl::min(key.size(), sizeof(p.key) - 1U);
  if (k_copy > 0U) etl::copy_n(key.begin(), k_copy, p.key);
  p.key[k_copy] = '\0';
  const size_t v_copy = etl::min(value.size(), sizeof(p.value.bytes));
  p.value.size = (pb_size_t)v_copy;
  if (v_copy > 0U) etl::copy_n(value.data(), v_copy, p.value.bytes);
  (void)send(rpc::CommandId::CMD_DATASTORE_PUT, 0, p);
}
void BridgeClass::datastoreGet(etl::string_view key,
                               DataStoreGetHandler handler) {
  if (_pending_datastore_gets.full()) {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }
  rpc::payload::DatastoreGet p = {};
  const size_t k_copy = etl::min(key.size(), sizeof(p.key) - 1U);
  if (k_copy > 0U) etl::copy_n(key.begin(), k_copy, p.key);
  p.key[k_copy] = '\0';
  if (!send(rpc::CommandId::CMD_DATASTORE_GET, 0, p)) {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }
  PendingDataStoreGet pending = {};
  etl::copy_n(p.key, k_copy + 1, pending.key.begin());
  pending.handler = handler;
  _pending_datastore_gets.push(pending);
}

void BridgeClass::fileWrite(etl::string_view path,
                            etl::span<const uint8_t> data) {
  rpc::payload::FileWrite p = {};
  const size_t p_copy = etl::min(path.size(), sizeof(p.path) - 1U);
  if (p_copy > 0U) etl::copy_n(path.begin(), p_copy, p.path);
  p.path[p_copy] = '\0';
  const size_t d_copy = etl::min(data.size(), sizeof(p.data.bytes));
  p.data.size = (pb_size_t)d_copy;
  if (d_copy > 0U) etl::copy_n(data.data(), d_copy, p.data.bytes);
  (void)send(rpc::CommandId::CMD_FILE_WRITE, 0, p);
}
void BridgeClass::fileRead(etl::string_view path, FileReadHandler handler) {
  _fs_read_handler = handler;
  rpc::payload::FileRead p = {};
  const size_t p_copy = etl::min(path.size(), sizeof(p.path) - 1U);
  if (p_copy > 0U) etl::copy_n(path.begin(), p_copy, p.path);
  p.path[p_copy] = '\0';
  (void)send(rpc::CommandId::CMD_FILE_READ, 0, p);
}
void BridgeClass::fileRemove(etl::string_view path) {
  rpc::payload::FileRemove p = {};
  const size_t p_copy = etl::min(path.size(), sizeof(p.path) - 1U);
  if (p_copy > 0U) etl::copy_n(path.begin(), p_copy, p.path);
  p.path[p_copy] = '\0';
  (void)send(rpc::CommandId::CMD_FILE_REMOVE, 0, p);
}

void BridgeClass::mailboxPush(etl::span<const uint8_t> data) {
  rpc::payload::MailboxPush p = {};
  const size_t to_copy = etl::min(data.size(), sizeof(p.data.bytes));
  p.data.size = (pb_size_t)to_copy;
  if (to_copy > 0U) etl::copy_n(data.data(), to_copy, p.data.bytes);
  (void)send(rpc::CommandId::CMD_MAILBOX_PUSH, 0, p);
}
void BridgeClass::mailboxRequestRead() {
  (void)sendFrame(rpc::CommandId::CMD_MAILBOX_READ);
}
void BridgeClass::mailboxRequestAvailable() {
  (void)sendFrame(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
}
void BridgeClass::mailboxSignalProcessed() {
  (void)sendFrame(rpc::CommandId::CMD_MAILBOX_PROCESSED);
}
int BridgeClass::mailboxRead() {
  if (_mailbox_rx_buffer.empty()) return -1;
  uint8_t c = _mailbox_rx_buffer.front();
  _mailbox_rx_buffer.pop();
  return c;
}
int BridgeClass::mailboxPeek() {
  if (_mailbox_rx_buffer.empty()) return -1;
  return _mailbox_rx_buffer.front();
}

void BridgeClass::processRunAsync(etl::string_view cmd,
                                  etl::span<const etl::string_view> args,
                                  ProcessRunHandler handler) {
  if (_pending_process_runs.full()) {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
    if (handler.is_valid()) handler(-1);
    return;
  }
  rpc::payload::ProcessRunAsync p = {};
  etl::string<64> cmd_full;
  cmd_full.assign(cmd.begin(), cmd.end());
  for (auto arg : args) {
    if (cmd_full.available() > arg.size() + 1) {
      cmd_full.append(" ");
      cmd_full.append(arg.begin(), arg.end());
    }
  }
  const size_t c_copy = etl::min(cmd_full.size(), sizeof(p.command) - 1U);
  etl::copy_n(cmd_full.begin(), c_copy, p.command);
  p.command[c_copy] = '\0';
  if (send(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, 0, p)) {
    if (handler.is_valid()) _pending_process_runs.push({handler});
  } else {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
    if (handler.is_valid()) handler(-1);
  }
}
void BridgeClass::pollProcess(int32_t pid, ProcessPollHandler handler) {
  if (_pending_process_polls.full()) {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }
  rpc::payload::ProcessPoll p = {};
  p.pid = pid;
  if (send(rpc::CommandId::CMD_PROCESS_POLL, 0, p)) {
    if (handler.is_valid()) _pending_process_polls.push({pid, handler});
  } else {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}
void BridgeClass::processKill(int32_t pid) {
  rpc::payload::ProcessKill p = {};
  p.pid = pid;
  (void)send(rpc::CommandId::CMD_PROCESS_KILL, 0, p);
}

size_t BridgeClass::consoleWrite(uint8_t c) {
  if (_console_tx_buffer.full()) consoleWrite(nullptr, 0);
  if (!_console_tx_buffer.full()) {
    _console_tx_buffer.push_back(c);
    return 1;
  }
  return 0;
}
size_t BridgeClass::consoleWrite(const uint8_t* buffer, size_t size) {
  if (buffer == nullptr || size == 0) {
    if (!_console_tx_buffer.empty()) {
      rpc::payload::ConsoleWrite p = {};
      const size_t to_copy =
          etl::min(_console_tx_buffer.size(), sizeof(p.data.bytes));
      p.data.size = (pb_size_t)to_copy;
      etl::copy_n(_console_tx_buffer.data(), to_copy, p.data.bytes);
      if (send(rpc::CommandId::CMD_CONSOLE_WRITE, 0, p)) {
        _console_tx_buffer.clear();
      }
    }
    return 0;
  }
  size_t written = 0;
  while (written < size) {
    if (_console_tx_buffer.full()) consoleWrite(nullptr, 0);
    if (_console_tx_buffer.full()) break;
    size_t can_write = etl::min(size - written, _console_tx_buffer.available());
    _console_tx_buffer.insert(_console_tx_buffer.end(), buffer + written,
                              buffer + written + can_write);
    written += can_write;
  }
  return written;
}
int BridgeClass::consoleRead() {
  if (_console_rx_buffer.empty()) return -1;
  uint8_t c = _console_rx_buffer.front();
  _console_rx_buffer.pop();
  return c;
}
int BridgeClass::consolePeek() {
  if (_console_rx_buffer.empty()) return -1;
  return _console_rx_buffer.front();
}

void BridgeClass::spiBegin() {
  SPI.begin();
  _spi_initialized = true;
}
void BridgeClass::spiEnd() {
  SPI.end();
  _spi_initialized = false;
}
void BridgeClass::spiSetConfig(const rpc::payload::SpiConfig& config) {
  _spi_settings =
      SPISettings(config.frequency, config.bit_order, config.data_mode);
}
size_t BridgeClass::spiTransfer(etl::span<uint8_t> buffer) {
  if (!_spi_initialized || buffer.empty()) return 0;
  SPI.beginTransaction(_spi_settings);
  uint32_t start = millis();
  auto it = etl::find_if(buffer.begin(), buffer.end(), [&](uint8_t& b) {
    if (millis() - start > rpc::RPC_SPI_TIMEOUT_MS) return true;
    b = SPI.transfer(b);
    return false;
  });
  SPI.endTransaction();
  return (it == buffer.end()) ? buffer.size() : 0;
}

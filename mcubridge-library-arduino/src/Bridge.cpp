#include "Bridge.h"

#include <etl/algorithm.h>
#include <etl/functional.h>
#include <etl/iterator.h>

#include "hal/ArchTraits.h"
#include "hal/progmem_compat.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"

namespace {
void _onStartupStabilizationTimeout() { Bridge._onStartupStabilized(); }
void _onAckTimeoutInternal() { Bridge._onAckTimeout(); }
void _onRxDedupeTimeout() { Bridge._onRxDedupe(); }
void _onBaudrateChangeTimeout() { Bridge._onBaudrateChange(); }

constexpr bool is_reliable_cmd(uint16_t id) { return rpc::is_reliable(id); }

constexpr bool is_compressed_cmd(uint16_t id) {
  return (id & rpc::RPC_CMD_FLAG_COMPRESSED) != 0;
}
}  // namespace

BridgeClass Bridge(Serial);

namespace etl {
void __attribute__((weak)) __attribute__((unused)) handle_error(
    const etl::exception& e) {
  BridgeClass::ErrorPolicy::handle(Bridge, e);
}
}  // namespace etl

void BridgeClass::registerObserver(BridgeObserver& observer) {
  if (!_observers.full()) {
    _observers.push_back(&observer);
  }
}

void BridgeClass::notify_observers(const MsgBridgeSynchronized& msg) {
  etl::for_each(
      _observers.begin(), _observers.end(),
      [&msg](BridgeObserver* observer) { observer->notification(msg); });
}

void BridgeClass::notify_observers(const MsgBridgeLost& msg) {
  etl::for_each(
      _observers.begin(), _observers.end(),
      [&msg](BridgeObserver* observer) { observer->notification(msg); });
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
      _packet_serial(
          etl::span<uint8_t>(_ps_rx_storage.data(), _ps_rx_storage.size()),
          etl::span<uint8_t>(_ps_work_buffer.data(), _ps_work_buffer.size())),
      _shared_secret(),
      _fsm(),
      _watchdog_task(),
      _serial_task(*this),
      _timer_task(*this),
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
  _shared_secret.clear();
  _rx_storage.fill(0);

  _tasks.push_back(&_watchdog_task);
  _tasks.push_back(&_serial_task);
  _tasks.push_back(&_timer_task);

  // [SIL-2] Initialize O(log N) Dispatch Table (RAM-efficient)  // Eradicates
  // 'switch' statements as per mission critical requirements.
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION)] =
      &BridgeClass::_handleGetVersion;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY)] =
      &BridgeClass::_handleGetFreeMemory;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC)] =
      &BridgeClass::_handleLinkSync;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET)] =
      &BridgeClass::_handleLinkReset;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES)] =
      &BridgeClass::_handleGetCapabilities;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE)] =
      &BridgeClass::_handleSetBaudrateCommand;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER)] =
      &BridgeClass::_handleEnterBootloaderCommand;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_XOFF)] =
      &BridgeClass::_handleXoff;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_XON)] =
      &BridgeClass::_handleXon;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE)] =
      &BridgeClass::_handleSetPinModeCommand;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE)] =
      &BridgeClass::_handleDigitalWriteCommand;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE)] =
      &BridgeClass::_handleAnalogWriteCommand;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ)] =
      &BridgeClass::_handleDigitalReadCommand;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ)] =
      &BridgeClass::_handleAnalogReadCommand;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE)] =
      &BridgeClass::_handleConsoleWriteCommand;
  _dispatch_table[rpc::to_underlying(rpc::StatusCode::STATUS_OK)] =
      &BridgeClass::_handleStatusOk;
  _dispatch_table[rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED)] =
      &BridgeClass::_handleStatusMalformed;
  _dispatch_table[rpc::to_underlying(rpc::StatusCode::STATUS_ACK)] =
      &BridgeClass::_handleStatusAck;

#if BRIDGE_ENABLE_DATASTORE
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP)] =
      &BridgeClass::_handleDataStoreGetResponseCommand;
#endif

#if BRIDGE_ENABLE_MAILBOX
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH)] =
      &BridgeClass::_handleMailboxPushCommand;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP)] =
      &BridgeClass::_handleMailboxReadResponseCommand;
  _dispatch_table[rpc::to_underlying(
      rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP)] =
      &BridgeClass::_handleMailboxAvailableResponseCommand;
#endif

#if BRIDGE_ENABLE_FILESYSTEM
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE)] =
      &BridgeClass::_handleFileWriteCommand;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_FILE_READ)] =
      &BridgeClass::_handleFileReadCommand;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE)] =
      &BridgeClass::_handleFileRemoveCommand;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP)] =
      &BridgeClass::_handleFileReadResponseCommand;
#endif

#if BRIDGE_ENABLE_PROCESS
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_PROCESS_KILL)] =
      &BridgeClass::_handleProcessKillCommand;
  _dispatch_table[rpc::to_underlying(
      rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP)] =
      &BridgeClass::_handleProcessRunAsyncResponseCommand;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP)] =
      &BridgeClass::_handleProcessPollResponseCommand;
#endif

#if BRIDGE_ENABLE_SPI
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN)] =
      &BridgeClass::_handleSpiBegin;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER)] =
      &BridgeClass::_handleSpiTransfer;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_SPI_END)] =
      &BridgeClass::_handleSpiEnd;
  _dispatch_table[rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG)] =
      &BridgeClass::_handleSpiSetConfigCommand;
#endif

  // [SIL-2] Register service observers
#if BRIDGE_ENABLE_CONSOLE
  registerObserver(Console);
#endif
#if BRIDGE_ENABLE_MAILBOX
  registerObserver(Mailbox);
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  registerObserver(FileSystem);
#endif
#if BRIDGE_ENABLE_DATASTORE
  registerObserver(DataStore);
#endif
#if BRIDGE_ENABLE_PROCESS
  registerObserver(Process);
#endif
#if BRIDGE_ENABLE_SPI
  registerObserver(SPIService);
#endif

  if constexpr (bridge::hal::CurrentArchTraits::id ==
                bridge::hal::ArchId::ARCH_AVR) {
    _hardware_serial = static_cast<HardwareSerial*>(&stream);
  }
  bridge::hal::forceSafeState();
}

void BridgeClass::begin(uint32_t baudrate, const char* secret) {
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

#if defined(ARDUINO_ARCH_AVR)
  wdt_enable(WDTO_4S);
#endif

  if constexpr (bridge::hal::CurrentArchTraits::id ==
                bridge::hal::ArchId::ARCH_AVR) {
    if (baudrate > 0 && _hardware_serial) _hardware_serial->begin(baudrate);
  }

  _tx_enabled = true;
  _timers.clear();
  _timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT] = _timers.register_timer(
      _onAckTimeoutInternal, _ack_timeout_ms, etl::timer::mode::REPEATING);
  _timer_ids[bridge::scheduler::TIMER_RX_DEDUPE] = _timers.register_timer(
      _onRxDedupeTimeout, bridge::config::HANDSHAKE_RETRY_DELAY_MS,
      etl::timer::mode::REPEATING);
  _timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE] = _timers.register_timer(
      _onBaudrateChangeTimeout, bridge::config::BAUDRATE_CHANGE_DELAY_MS,
      etl::timer::mode::SINGLE_SHOT);
  _timer_ids[bridge::scheduler::TIMER_STARTUP_STABILIZATION] =
      _timers.register_timer(_onStartupStabilizationTimeout,
                             bridge::config::STARTUP_STABILIZATION_MS,
                             etl::timer::mode::SINGLE_SHOT);
  _timers.start(_timer_ids[bridge::scheduler::TIMER_STARTUP_STABILIZATION]);

  _packet_serial.setPacketHandler(
      etl::delegate<void(etl::span<const uint8_t>)>::create<
          BridgeClass, &BridgeClass::_onPacketReceived>(*this));
  _packet_serial.setErrorHandler(
      etl::delegate<void(PacketSerial2::ErrorCode)>::create<
          BridgeClass, &BridgeClass::_onPacketSerialError>(*this));
}

void BridgeClass::process() { (void)_scheduler_policy.schedule_tasks(_tasks); }

void BridgeClass::WatchdogTask::task_process_work() {
#if defined(ARDUINO_ARCH_AVR)
  wdt_reset();
#endif
}

void BridgeClass::SerialTask::task_process_work() {
  bridge._packet_serial.update(bridge._stream);

  int available_bytes = bridge._stream.available();
  if (!xoff_sent && available_bytes > 48) {
    bridge.signalXoff();
    xoff_sent = true;
  } else if (xoff_sent && available_bytes < 16) {
    bridge.signalXon();
    xoff_sent = false;
  }
}

void BridgeClass::TimerTask::task_process_work() {
  uint32_t now = bridge::now_ms();
  if (last_tick_ms == 0) last_tick_ms = now;
  uint32_t elapsed = now - last_tick_ms;
  if (elapsed > 0) {
    bridge._timers.tick(elapsed);
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

  // [SIL-2] Deterministic Dispatcher: O(log N) search in RAM-efficient
  // structure. Eradicates 'switch' statements as per mission critical
  // requirements.
  bool handled = false;
  const uint16_t raw_cmd = ctx.raw_command;
  auto dispatch_it = _dispatch_table.find(raw_cmd);

  if (dispatch_it != _dispatch_table.end()) {
    (this->*(dispatch_it->second))(ctx);
    handled = true;
  }

  if (!handled) {
    onUnknownCommand(ctx);
  }
}

void BridgeClass::_handleStatusOk(const bridge::router::CommandContext& ctx) {
  (void)ctx;
  // OK status received from daemon, no action needed but hookable
}

void BridgeClass::onUnknownCommand(const bridge::router::CommandContext& ctx) {
  if (_command_handler.is_valid())
    _command_handler(*ctx.frame);
  else
    emitStatus<rpc::StatusCode::STATUS_ERROR>();
}

void BridgeClass::_onStartupStabilized() {
  uint32_t start_ms = bridge::now_ms();
  // [SIL-2] Deterministic drain via ETL algorithm (No Raw Loops).
  // We simulate a loop using a recursive structure that avoids deep stack if
  // needed, or a counting iterator if available. Given AVR limits, we use a
  // simple functional approach using etl::find_if on a counting range.
  struct Counter {
    uint16_t current = 0;
    bool operator==(const Counter& other) const {
      return current == other.current;
    }
    bool operator!=(const Counter& other) const {
      return current != other.current;
    }
    Counter& operator++() {
      ++current;
      return *this;
    }
    uint16_t operator*() const { return current; }
  };

  Counter it_begin{0};
  Counter it_end{bridge::config::STARTUP_DRAIN_FINAL};

  (void)etl::find_if(it_begin, it_end, [this, start_ms](uint16_t) {
    if (_stream.available() <= 0 ||
        (bridge::now_ms() - start_ms >= bridge::config::SERIAL_TIMEOUT_MS)) {
      return true;  // Stop condition
    }
    (void)_stream.read();
    return false;  // Continue
  });

  BRIDGE_ATOMIC_BLOCK { _fsm.receive(bridge::fsm::EvStabilized()); }
}

void BridgeClass::enterSafeState() {
  BRIDGE_ATOMIC_BLOCK { _fsm.receive(bridge::fsm::EvReset()); }
  etl::for_each(_timer_ids.begin(), _timer_ids.end(),
                [this](etl::timer::id::type id) { _timers.stop(id); });
  _pending_baudrate = 0;
  _retry_count = 0;
  _clearPendingTxQueue();
  _rx_history.clear();
  _tx_enabled = true;
  rpc::security::secure_zero(
      etl::span<uint8_t>(_shared_secret.data(), _shared_secret.size()));
  _shared_secret.clear();
#if BRIDGE_ENABLE_PROCESS
  Process.reset();
#endif
  bridge::hal::forceSafeState();
  notify_observers(MsgBridgeLost());
}

void BridgeClass::emitStatus(rpc::StatusCode status_code,
                             etl::span<const uint8_t> payload) {
  if (_status_handler.is_valid()) _status_handler(status_code, payload);
  (void)sendFrame(status_code, 0, payload);
}

void BridgeClass::emitStatus(rpc::StatusCode status_code,
                             etl::string_view message) {
  if (message.empty()) {
    emitStatus(status_code, etl::span<const uint8_t>());
    return;
  }
  const size_t max_len = etl::min(message.length(), rpc::MAX_PAYLOAD_SIZE - 1U);
  etl::copy_n(message.data(), max_len, _transient_buffer.data());
  _transient_buffer[max_len] = rpc::RPC_NULL_TERMINATOR;
  emitStatus(status_code,
             etl::span<const uint8_t>(_transient_buffer.data(), max_len));
}

void BridgeClass::emitStatus(rpc::StatusCode status_code,
                             const __FlashStringHelper* message) {
  if (message == nullptr) {
    emitStatus(status_code, etl::span<const uint8_t>());
    return;
  }
  constexpr size_t max_len = rpc::MAX_PAYLOAD_SIZE - 1U;
  bridge::hal::copy_string(
      static_cast<char*>(static_cast<void*>(_transient_buffer.data())),
      static_cast<const char*>(static_cast<const void*>(message)), max_len);
  _transient_buffer[max_len] = rpc::RPC_NULL_TERMINATOR;
  const size_t actual_len =
      etl::string_view(static_cast<const char*>(
                           static_cast<const void*>(_transient_buffer.data())))
          .length();
  emitStatus(status_code,
             etl::span<const uint8_t>(_transient_buffer.data(), actual_len));
}

bool BridgeClass::sendFrame(rpc::StatusCode status_code, uint16_t sequence_id,
                            etl::span<const uint8_t> payload) {
  return _sendFrame(rpc::to_underlying(status_code), sequence_id, payload);
}
bool BridgeClass::sendFrame(rpc::CommandId command_id, uint16_t sequence_id,
                            etl::span<const uint8_t> payload) {
  return _sendFrame(rpc::to_underlying(command_id), sequence_id, payload);
}

void BridgeClass::_sendRawFrame(uint16_t command_id, uint16_t sequence_id,
                                etl::span<const uint8_t> payload) {
  rpc::Frame f = {};
  f.header.version = rpc::PROTOCOL_VERSION;
  f.header.command_id = command_id;
  f.header.sequence_id = sequence_id;
  f.header.payload_length = static_cast<uint16_t>(payload.size());
  f.payload = payload;
  f.crc = rpc::checksum::compute(f);
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> buffer;
  size_t len = rpc::FrameParser::serialize(
      f, etl::span<uint8_t>(buffer.data(), buffer.size()));
  if (len > 0)
    (void)_packet_serial.send(_stream, etl::span<const uint8_t>(buffer.data(), len));
}

bool BridgeClass::_sendFrame(uint16_t command_id, uint16_t sequence_id,
                             etl::span<const uint8_t> payload) {
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
  _sendRawFrame(command_id, sequence_id, payload);
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
    _fsm.receive(bridge::fsm::EvTimeout());
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

void BridgeClass::_handleAck(uint16_t command_id) {
  if (!_fsm.isAwaitingAck() || command_id != _last_command_id) return;
  _timers.stop(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
  _clearPendingTxQueue();
  _fsm.receive(bridge::fsm::EvAckReceived());
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

void BridgeClass::_handleSetBaudrateCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::SetBaudratePacket>(
      ctx, [this](const auto& m) { _handleSetBaudrate(m); });
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
      emitStatus<rpc::StatusCode::STATUS_ERROR>();
  });
}

void BridgeClass::_handleDigitalWriteCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::DigitalWrite>(ctx, [this](const auto& m) {
    if (bridge::hal::isValidPin(m.pin))
      ::digitalWrite(m.pin, m.value);
    else
      emitStatus<rpc::StatusCode::STATUS_ERROR>();
  });
}

void BridgeClass::_handleAnalogWriteCommand(
    const bridge::router::CommandContext& ctx) {
  _withPayloadAck<rpc::payload::AnalogWrite>(ctx, [this](const auto& m) {
    if (bridge::hal::isValidPin(m.pin))
      ::analogWrite(m.pin, m.value);
    else
      emitStatus<rpc::StatusCode::STATUS_ERROR>();
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
      ctx, [](const auto& m) { Process._kill(m); });
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
  _handleAck(ctx.raw_command);
}

void BridgeClass::_handleGetVersion(const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc::payload::VersionResponse resp = {
        rpc::FIRMWARE_VERSION_MAJOR, rpc::FIRMWARE_VERSION_MINOR,
        static_cast<uint32_t>(rpc::FIRMWARE_VERSION_PATCH)};
    _sendPbResponse(rpc::CommandId::CMD_GET_VERSION_RESP, ctx.sequence_id,
                    resp);
  });
}

void BridgeClass::_handleGetFreeMemory(
    const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    rpc::payload::FreeMemoryResponse resp = {
        static_cast<uint32_t>(bridge::hal::getFreeMemory())};
    _sendPbResponse(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, ctx.sequence_id,
                    resp);
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
    rpc::security::hkdf_sha256(
        etl::span<uint8_t>(handshake_key),
        etl::span<const uint8_t>(_shared_secret),
        etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_SALT),
        etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH));

    etl::array<uint8_t, 32> full_tag;
    full_tag.fill(0);
    rpc::security::McuBridgeSha256 hmac_engine;
    hmac_engine.resetHMAC(handshake_key);
    hmac_engine.update(msg.nonce);
    hmac_engine.finalizeHMAC(full_tag);

    if (!rpc::security::timing_safe_equal(
            etl::span<const uint8_t>(full_tag.data(),
                                     rpc::RPC_HANDSHAKE_TAG_LENGTH),
            etl::span<const uint8_t>(msg.tag.data(), 16))) {
      _fsm.receive(bridge::fsm::EvHandshakeFailed());
      emitStatus<rpc::StatusCode::STATUS_ERROR>();
      return;
    }
    etl::copy_n(full_tag.begin(), rpc::RPC_HANDSHAKE_TAG_LENGTH,
                resp.tag.begin());
    rpc::security::secure_zero(handshake_key);
    rpc::security::secure_zero(full_tag);
  }

  _fsm.receive(bridge::fsm::EvHandshakeStart());
  _fsm.receive(bridge::fsm::EvHandshakeComplete());
  _sendPbResponse(rpc::CommandId::CMD_LINK_SYNC_RESP, ctx.sequence_id, resp);
  notify_observers(MsgBridgeSynchronized());
}

void BridgeClass::_handleLinkReset(const bridge::router::CommandContext& ctx) {
  _withPayload<rpc::payload::HandshakeConfig>(
      ctx, [this, &ctx](const rpc::payload::HandshakeConfig& msg) {
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
    _sendPbResponse(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, ctx.sequence_id,
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

void BridgeClass::_handleSpiBegin(const bridge::router::CommandContext& ctx) {
  (void)ctx;
  SPIService.begin();
}
void BridgeClass::_handleSpiEnd(const bridge::router::CommandContext& ctx) {
  (void)ctx;
  SPIService.end();
}
void BridgeClass::_handleSpiTransfer(
    const bridge::router::CommandContext& ctx) {
  _withResponse(ctx, [this, &ctx]() {
    auto res = rpc::Payload::parse<rpc::payload::SpiTransfer>(*ctx.frame);
    if (res) {
      size_t len = etl::min(res->data.size(), _rx_storage.size());
      etl::copy_n(res->data.begin(), len, _rx_storage.begin());
      size_t transferred =
          SPIService.transfer(etl::span<uint8_t>(_rx_storage.data(), len));
      if (transferred == 0) {
        emitStatus<rpc::StatusCode::STATUS_ERROR>();
        return;
      }
      rpc::payload::SpiTransferResponse resp = {};
      resp.data = etl::span<const uint8_t>(_rx_storage.data(), len);
      _sendPbResponse(rpc::CommandId::CMD_SPI_TRANSFER_RESP, ctx.sequence_id,
                      resp);
    }
  });
}

void BridgeClass::_handleReceivedFrame(etl::span<const uint8_t> p) {
  auto res = _frame_parser.parse(p);
  if (!res) {
    _last_parse_error = res.error();
    emitStatus<rpc::StatusCode::STATUS_MALFORMED>();
    return;
  }
  rpc::Frame eff;
  auto dec = _decompressFrame(res.value(), eff);
  if (!dec) {
    _last_parse_error = dec.error();
    emitStatus<rpc::StatusCode::STATUS_MALFORMED>();
    return;
  }
  _dispatchCommand(eff);
}

void BridgeClass::_onPacketReceived(etl::span<const uint8_t> p) {
  _handleReceivedFrame(p);
}

void BridgeClass::_onPacketSerialError(PacketSerial2::ErrorCode error) {
  (void)error;
  _last_parse_error = rpc::FrameError::MALFORMED;
  _consecutive_crc_errors++;
  if (_consecutive_crc_errors >= rpc::MAX_CONSECUTIVE_CRC_ERRORS) {
    _fsm.receive(bridge::fsm::EvReset());
    emitStatus<rpc::StatusCode::STATUS_ERROR>();
  }
}

etl::expected<void, rpc::FrameError> BridgeClass::_decompressFrame(
    const rpc::Frame& in, rpc::Frame& out) {
  out.header = in.header;
  if (!is_compressed_cmd(in.header.command_id)) {
    out.payload = in.payload;
    return {};
  }
  _rx_storage.fill(0);
  size_t dec_len = ::rle::decode(
      in.payload, etl::span<uint8_t>(_rx_storage.data(), _rx_storage.size()));
  if (dec_len == 0 && !in.payload.empty())
    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);
  out.payload = etl::span<uint8_t>(_rx_storage.data(), dec_len);
  out.header.payload_length = static_cast<uint16_t>(dec_len);
  return {};
}

[[maybe_unused]] void BridgeClass::_computeHandshakeTag(
    const etl::span<const uint8_t> nonce, etl::span<uint8_t> tag) {
  etl::array<uint8_t, 32> handshake_key;
  handshake_key.fill(0);
  rpc::security::hkdf_sha256(
      etl::span<uint8_t>(handshake_key),
      etl::span<const uint8_t>(_shared_secret),
      etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_SALT),
      etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH));

  etl::array<uint8_t, 32> full_tag;
  full_tag.fill(0);
  rpc::security::McuBridgeSha256 hmac_engine;
  hmac_engine.resetHMAC(handshake_key);
  hmac_engine.update(nonce);
  hmac_engine.finalizeHMAC(full_tag);

  etl::copy_n(full_tag.data(), rpc::RPC_HANDSHAKE_TAG_LENGTH, tag.data());
  rpc::security::secure_zero(handshake_key);
  rpc::security::secure_zero(full_tag);
}

void BridgeClass::_applyTimingConfig(const rpc::payload::HandshakeConfig& msg) {
  _handleSetTiming(msg);
}

bool BridgeClass::_isSecurityCheckPassed(uint16_t command_id) const {
  if (_shared_secret.empty()) return true;
  if (rpc::is_any_of(command_id, rpc::CommandId::CMD_LINK_SYNC,
                     rpc::CommandId::CMD_LINK_RESET,
                     rpc::CommandId::CMD_GET_VERSION))
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

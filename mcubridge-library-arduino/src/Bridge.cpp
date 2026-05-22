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

#include <wolfssl/wolfcrypt/settings.h>
#include <wolfssl/wolfcrypt/types.h>

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
      _frame_parser(),
      _is_post_passed(false),
      _tx_enabled(true),
      _tx_payload_pool(),
      _pending_tx_queue(),
      _rx_history() {
}

void BridgeClass::_dispatchCommand(const rpc_pb_McuFrame& frame) {
  if (_command_handler.is_valid()) {
    _command_handler(frame);
  }

  switch (frame.which_message) {
    case rpc_pb_McuFrame_ack_tag:
      _handleAck(frame.message.ack.command_id);
      break;
    case rpc_pb_McuFrame_error_tag:
      _handleStatusMalformed(frame);
      break;
    case rpc_pb_McuFrame_get_version_tag:
      _handleGetVersion(frame);
      break;
    case rpc_pb_McuFrame_get_free_memory_tag:
      _handleGetFreeMemory(frame);
      break;
    case rpc_pb_McuFrame_link_sync_tag:
      _handleLinkSync(frame);
      break;
    case rpc_pb_McuFrame_link_reset_tag:
      _handleLinkReset(frame);
      break;
    case rpc_pb_McuFrame_get_capabilities_tag:
      _handleGetCapabilities(frame);
      break;
    case rpc_pb_McuFrame_set_baudrate_tag:
      _handleSetBaudrateCommand(frame);
      break;
    case rpc_pb_McuFrame_enter_bootloader_tag:
      _handleEnterBootloaderCommand(frame);
      break;
    case rpc_pb_McuFrame_xoff_tag:
      _handleXoff(frame);
      break;
    case rpc_pb_McuFrame_xon_tag:
      _handleXon(frame);
      break;
    case rpc_pb_McuFrame_set_pin_mode_tag:
      _handleSetPinModeCommand(frame);
      break;
    case rpc_pb_McuFrame_digital_write_tag:
      _handleDigitalWriteCommand(frame);
      break;
    case rpc_pb_McuFrame_analog_write_tag:
      _handleAnalogWriteCommand(frame);
      break;
    case rpc_pb_McuFrame_digital_read_tag:
      _handleDigitalReadCommand(frame);
      break;
    case rpc_pb_McuFrame_analog_read_tag:
      _handleAnalogReadCommand(frame);
      break;
    case rpc_pb_McuFrame_console_write_tag:
      _handleConsoleWriteCommand(frame);
      break;
#if BRIDGE_ENABLE_DATASTORE
    case rpc_pb_McuFrame_datastore_get_resp_tag:
      _handleDataStoreGetResponseCommand(frame);
      break;
#endif
#if BRIDGE_ENABLE_MAILBOX
    case rpc_pb_McuFrame_mailbox_push_tag:
      _handleMailboxPushCommand(frame);
      break;
    case rpc_pb_McuFrame_mailbox_read_resp_tag:
      _handleMailboxReadResponseCommand(frame);
      break;
    case rpc_pb_McuFrame_mailbox_available_resp_tag:
      _handleMailboxAvailableResponseCommand(frame);
      break;
#endif
#if BRIDGE_ENABLE_FILESYSTEM
    case rpc_pb_McuFrame_file_write_tag:
      _handleFileWriteCommand(frame);
      break;
    case rpc_pb_McuFrame_file_read_tag:
      _handleFileReadCommand(frame);
      break;
    case rpc_pb_McuFrame_file_remove_tag:
      _handleFileRemoveCommand(frame);
      break;
    case rpc_pb_McuFrame_file_read_resp_tag:
      _handleFileReadResponseCommand(frame);
      break;
#endif
#if BRIDGE_ENABLE_PROCESS
    case rpc_pb_McuFrame_process_kill_tag:
      _handleProcessKillCommand(frame);
      break;
    case rpc_pb_McuFrame_process_run_resp_tag:
      _handleProcessRunAsyncResponseCommand(frame);
      break;
    case rpc_pb_McuFrame_process_poll_resp_tag:
      _handleProcessPollResponseCommand(frame);
      break;
#endif
#if BRIDGE_ENABLE_SPI
    case rpc_pb_McuFrame_spi_begin_tag:
      _handleSpiBegin(frame);
      break;
    case rpc_pb_McuFrame_spi_transfer_tag:
      _handleSpiTransfer(frame);
      break;
    case rpc_pb_McuFrame_spi_end_tag:
      _handleSpiEnd(frame);
      break;
    case rpc_pb_McuFrame_spi_set_config_tag:
      _handleSpiSetConfigCommand(frame);
      break;
#endif
    default:
      onUnknownCommand(frame);
      break;
  }
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

void BridgeClass::_handleStatusOk(const rpc_pb_McuFrame& frame) {
  (void)frame;
}

void BridgeClass::onUnknownCommand(const rpc_pb_McuFrame& frame) {
  if (_command_handler.is_valid())
    _command_handler(frame);
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

bool BridgeClass::send(const rpc_pb_McuFrame& frame) {
  pb_ostream_t stream = pb_ostream_from_buffer(_transient_buffer.data(), _transient_buffer.size());
  if (!pb_encode(&stream, rpc_pb_McuFrame_fields, &frame)) return false;
  return _sendFrame(static_cast<uint16_t>(frame.which_message), 
                    static_cast<uint16_t>(frame.seq_id),
                    etl::span<const uint8_t>(_transient_buffer.data(), stream.bytes_written));
}

bool BridgeClass::send(uint16_t tag, uint16_t seq, const void* struct_ptr, const pb_msgdesc_t* fields) {
  // [SIL-2] Manual encoding of McuFrame container to avoid large union allocation.
  pb_ostream_t stream = pb_ostream_from_buffer(_transient_buffer.data(), _transient_buffer.size());
  
  // Manually encode McuFrame header then the submessage tag
  if (!pb_encode_tag(&stream, PB_WT_VARINT, rpc_pb_McuFrame_seq_id_tag)) return false;
  if (!pb_encode_varint(&stream, seq)) return false;
  if (!pb_encode_tag(&stream, PB_WT_STRING, tag)) return false;
  if (!pb_encode_submessage(&stream, fields, struct_ptr)) return false;
  
  return _sendFrame(tag, seq, etl::span<const uint8_t>(_transient_buffer.data(), stream.bytes_written));
}

void BridgeClass::_sendRawFrame(uint16_t command_id, uint16_t sequence_id,
                                etl::span<const uint8_t> payload) {
  const uint16_t raw_cmd = command_id & ~rpc::RPC_CMD_FLAG_COMPRESSED;
  const bool is_excluded = (raw_cmd >= rpc::RPC_STATUS_CODE_MIN &&
                            raw_cmd <= rpc::RPC_STATUS_CODE_MAX) ||
                           (raw_cmd >= rpc::RPC_SYSTEM_COMMAND_MIN &&
                            raw_cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);
  const bool do_encrypt = isSynchronized() && !_shared_secret.empty() && !is_excluded;

  rpc::Frame f = {};
  f.header = {rpc::PROTOCOL_VERSION, static_cast<uint16_t>(payload.size()),
              command_id, sequence_id};

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> enc_pl;

  if (do_encrypt) {
    if (!rpc::security::aead_encrypt_frame_raw(f, payload.data(), payload.size(),
                                           _session_key.data(),
                                           _tx_nonce_counter, enc_pl.data()))
      return;
  } else {
    f.payload = payload;
    f.nonce.fill(0);
    f.tag.fill(0);
  }

  f.crc = rpc::checksum::compute(f);
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> buffer;
  size_t len = rpc::FrameParser::serialize(f, buffer);

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
  const uint16_t raw_cmd = cmd & ~rpc::RPC_CMD_FLAG_COMPRESSED;
  const bool is_system =
      (raw_cmd >= rpc::RPC_STATUS_CODE_MIN && raw_cmd <= rpc::RPC_STATUS_CODE_MAX) ||
      (raw_cmd >= rpc::RPC_SYSTEM_COMMAND_MIN && raw_cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);

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
  AckPacket p = AckPacket_init_default;
  p.command_id = command_id;
  (void)send(rpc::CommandId::CMD_ACK, sequence_id, p);
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

void BridgeClass::_handleSetBaudrateCommand(const rpc_pb_McuFrame& frame) {
  _handleSetBaudrate(frame.message.set_baudrate);
  (void)sendFrame(rpc::CommandId::CMD_SET_BAUDRATE_RESP, frame.seq_id);
}

void BridgeClass::_handleEnterBootloaderCommand(const rpc_pb_McuFrame& frame) {
  _handleEnterBootloader(frame.message.enter_bootloader);
}

void BridgeClass::_handleSetPinModeCommand(const rpc_pb_McuFrame& frame) {
  const auto& m = frame.message.set_pin_mode;
  uint8_t m_val = INPUT;
  if (m.mode == 1) m_val = OUTPUT;
  else if (m.mode == 2) m_val = INPUT_PULLUP;
  pinMode(m.pin, m_val);
}

void BridgeClass::_handleDigitalWriteCommand(const rpc_pb_McuFrame& frame) {
  const auto& m = frame.message.digital_write;
  digitalWrite(m.pin, (m.value == 0) ? LOW : HIGH);
}

void BridgeClass::_handleAnalogWriteCommand(const rpc_pb_McuFrame& frame) {
  const auto& m = frame.message.analog_write;
  analogWrite(m.pin, (int)m.value);
}

void BridgeClass::_handleDigitalReadCommand(const rpc_pb_McuFrame& frame) {
  const uint16_t val = digitalRead(frame.message.digital_read.pin);
  rpc_pb_DigitalReadResponse resp = rpc_pb_DigitalReadResponse_init_default;
  resp.value = val;
  (void)send(rpc::CommandId::CMD_DIGITAL_READ_RESP, frame.seq_id, resp);
}

void BridgeClass::_handleAnalogReadCommand(const rpc_pb_McuFrame& frame) {
  const uint16_t val = analogRead(frame.message.analog_read.pin);
  rpc_pb_AnalogReadResponse resp = rpc_pb_AnalogReadResponse_init_default;
  resp.value = val;
  (void)send(rpc::CommandId::CMD_ANALOG_READ_RESP, frame.seq_id, resp);
}

void BridgeClass::_handleConsoleWriteCommand(const rpc_pb_McuFrame& frame) {
  Console._push(frame.message.console_write);
}

#if BRIDGE_ENABLE_DATASTORE
void BridgeClass::_handleDataStoreGetResponseCommand(const rpc_pb_McuFrame& frame) {
  DataStore._onResponse(frame.message.datastore_get_resp);
}
#endif

#if BRIDGE_ENABLE_MAILBOX
void BridgeClass::_handleMailboxPushCommand(const rpc_pb_McuFrame& frame) {
  Mailbox._onIncomingData(frame.message.mailbox_push);
}
void BridgeClass::_handleMailboxReadResponseCommand(const rpc_pb_McuFrame& frame) {
  Mailbox._onIncomingData(frame.message.mailbox_read_resp);
}
void BridgeClass::_handleMailboxAvailableResponseCommand(const rpc_pb_McuFrame& frame) {
  Mailbox._onAvailableResponse(frame.message.mailbox_available_resp);
}
#endif

#if BRIDGE_ENABLE_FILESYSTEM
void BridgeClass::_handleFileWriteCommand(const rpc_pb_McuFrame& frame) {
  FileSystem._onWrite(frame.message.file_write);
}
void BridgeClass::_handleFileReadCommand(const rpc_pb_McuFrame& frame) {
  FileSystem._onRead(frame.message.file_read);
}
void BridgeClass::_handleFileRemoveCommand(const rpc_pb_McuFrame& frame) {
  FileSystem._onRemove(frame.message.file_remove);
}
void BridgeClass::_handleFileReadResponseCommand(const rpc_pb_McuFrame& frame) {
  FileSystem._onResponse(frame.message.file_read_resp);
}
#endif

#if BRIDGE_ENABLE_PROCESS
void BridgeClass::_handleProcessKillCommand(const rpc_pb_McuFrame& frame) {
  Process._onKillNotification(frame.message.process_kill);
}
void BridgeClass::_handleProcessRunAsyncResponseCommand(const rpc_pb_McuFrame& frame) {
  Process._onRunAsyncResponse(frame.message.process_run_resp);
}
void BridgeClass::_handleProcessPollResponseCommand(const rpc_pb_McuFrame& frame) {
  Process._onPollResponse(frame.message.process_poll_resp);
}
#endif

#if BRIDGE_ENABLE_SPI
void BridgeClass::_handleSpiSetConfigCommand(const rpc_pb_McuFrame& frame) {
  SPIService.setConfig(frame.message.spi_set_config);
}
#endif

void BridgeClass::_handleStatusAck(const rpc_pb_McuFrame& frame) {
  _handleAck(frame.message.ack.command_id);
}

void BridgeClass::_handleGetVersion(const rpc_pb_McuFrame& frame) {
  rpc_pb_VersionResponse resp = rpc_pb_VersionResponse_init_default;
  resp.major = rpc::FIRMWARE_VERSION_MAJOR;
  resp.minor = rpc::FIRMWARE_VERSION_MINOR;
  resp.patch = (uint32_t)rpc::FIRMWARE_VERSION_PATCH;
  (void)send(rpc::CommandId::CMD_VERSION_RESP, frame.seq_id, resp);
}

void BridgeClass::_handleGetFreeMemory(const rpc_pb_McuFrame& frame) {
  rpc_pb_FreeMemoryResponse resp = rpc_pb_FreeMemoryResponse_init_default;
  resp.value = (uint32_t)bridge::hal::getFreeMemory();
  (void)send(rpc::CommandId::CMD_FREE_MEMORY_RESP, frame.seq_id, resp);
}

void BridgeClass::_handleLinkSync(const rpc_pb_McuFrame& frame) {
  const auto& msg = frame.message.link_sync;
  rpc_pb_LinkSyncResponse resp = rpc_pb_LinkSyncResponse_init_default;
  etl::copy_n(msg.nonce, 16, resp.nonce);

  if (!_shared_secret.empty()) {
    etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> out_tag;
    const bool tag_ok = rpc::security::handshake_authenticate_raw(
        _shared_secret.data(), _shared_secret.size(),
        msg.nonce, 16,
        msg.tag, rpc::RPC_HANDSHAKE_TAG_LENGTH,
        out_tag.data());

    if (!tag_ok) {
      _fsm.receive(bridge::fsm::EvHandshakeFailed());
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }

    etl::copy_n(out_tag.data(), 16, resp.tag);
    rpc::security::derive_session_key_raw(
        _shared_secret.data(), _shared_secret.size(),
        msg.nonce, 16,
        _session_key.data());
    _tx_nonce_counter = 0;
    _rx_nonce_counter = 0;
    rpc::security::secure_zero(etl::span<uint8_t>(out_tag));
  }

  _fsm.receive(bridge::fsm::EvHandshakeStart());
  _fsm.receive(bridge::fsm::EvHandshakeComplete());
  _tx_enabled = true;
  (void)send(rpc::CommandId::CMD_LINK_SYNC_RESP, frame.seq_id, resp);
  _notifyObservers(MsgBridgeSynchronized());
}

void BridgeClass::_handleLinkReset(const rpc_pb_McuFrame& frame) {
    // applyTimingConfig ...
    enterSafeState();
    (void)sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP, frame.seq_id);
}

void BridgeClass::_handleGetCapabilities(const rpc_pb_McuFrame& frame) {
    rpc_pb_Capabilities resp = rpc_pb_Capabilities_init_default;
    resp.ver = rpc::PROTOCOL_VERSION;
    resp.arch = bridge::hal::getArchId();
    resp.feat = bridge::hal::getCapabilities();
    uint8_t dig = 0, ana = 0;
    bridge::hal::getPinCounts(dig, ana);
    resp.dig = dig;
    resp.ana = ana;
    (void)send(rpc::CommandId::CMD_CAPABILITIES_RESP, frame.seq_id, resp);
}

void BridgeClass::_handleXoff(const rpc_pb_McuFrame& frame) {
  (void)frame;
  _tx_enabled = false;
}

void BridgeClass::_handleXon(const rpc_pb_McuFrame& frame) {
  (void)frame;
  _tx_enabled = true;
  _flushPendingTxQueue();
}

void BridgeClass::_handleSetBaudrate(const rpc_pb_SetBaudratePacket& msg) {
  if (msg.baudrate == 0 || msg.baudrate == _pending_baudrate) return;
  _pending_baudrate = msg.baudrate;
  _timers.start(_timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE]);
}

void BridgeClass::_applyTimingConfig(const rpc_pb_HandshakeConfig& msg) {
  if (msg.ack_timeout_ms > 0) {
    _ack_timeout_ms = msg.ack_timeout_ms;
    _timers.set_period(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT], _ack_timeout_ms);
  }
  if (msg.response_timeout_ms > 0)
    _response_timeout_ms = msg.response_timeout_ms;
}

void BridgeClass::_handleEnterBootloader(const rpc_pb_EnterBootloader& msg) {
  if (msg.magic == rpc::RPC_BOOTLOADER_MAGIC) {
    this->flushStream();
    _timers.start(_timer_ids[bridge::scheduler::TIMER_BOOTLOADER_DELAY]);
  }
}

void BridgeClass::_onBootloaderDelay() { bridge::hal::enterBootloader(); }

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
    if (!rpc::security::aead_decrypt_frame_raw(frame, _session_key.data(), dec_pl.data()) ||
        !rpc::security::validate_frame_nonce(frame, _rx_nonce_counter)) {
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

  rpc_pb_McuFrame pb_frame = rpc_pb_McuFrame_init_default;
  pb_istream_t stream = pb_istream_from_buffer(eff.payload.data(), eff.payload.size());
  if (pb_decode(&stream, rpc_pb_McuFrame_fields, &pb_frame)) {
      if (pb_frame.seq_id == 0) pb_frame.seq_id = eff.header.sequence_id;
      _dispatchCommand(pb_frame);
  } else {
      emitStatus(rpc::StatusCode::STATUS_MALFORMED);
  }
}

void BridgeClass::_onPacketReceived(etl::span<const uint8_t> p) {
  _handleReceivedFrame(p);
}

etl::expected<void, rpc::FrameError> BridgeClass::_decompressFrame(
    const rpc::Frame& in, rpc::Frame& out) {
  out = in;
  if (!rpc::is_compressed(in.header.command_id)) return {};

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> decomp_pl;
  size_t decomp_size = rle::decode(in.payload, decomp_pl);
  if (decomp_size == 0) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);

  etl::copy_n(decomp_pl.data(), decomp_size, _transient_buffer.data());
  out.payload = etl::span<const uint8_t>(_transient_buffer.data(), decomp_size);
  return {};
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

void BridgeClass::_handleSpiBegin(const rpc_pb_McuFrame& frame) {
  SPIService.begin();
  (void)sendFrame(rpc::CommandId::CMD_ACK, frame.seq_id);
}
void BridgeClass::_handleSpiEnd(const rpc_pb_McuFrame& frame) {
  SPIService.end();
  (void)sendFrame(rpc::CommandId::CMD_ACK, frame.seq_id);
}
void BridgeClass::_handleSpiTransfer(
    const rpc_pb_McuFrame& frame) {
    const auto& msg = frame.message.spi_transfer;
    size_t len = etl::min((size_t)msg.data.size, _rx_storage.size());
    etl::copy_n(msg.data.bytes, len, _rx_storage.begin());
    size_t tr =
        SPIService.transfer(etl::span<uint8_t>(_rx_storage.data(), len));
    if (tr == 0) {
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }
    rpc_pb_SpiTransferResponse resp = rpc_pb_SpiTransferResponse_init_default;
    etl::copy_n(_rx_storage.data(), len, resp.data.bytes);
    resp.data.size = len;
    (void)send(rpc::CommandId::CMD_SPI_TRANSFER_RESP, frame.seq_id, resp);
}

void BridgeClass::_handleStatusMalformed(
    const rpc_pb_McuFrame& frame) {
  (void)frame;
  enterSafeState();
}

namespace bridge {
void SafeStatePolicy::handle(::BridgeClass& bridge, const etl::exception& e) {
  (void)e;
  bridge.enterSafeState();
}
}  // namespace bridge

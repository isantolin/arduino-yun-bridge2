#include "Bridge.h"

#include <etl/algorithm.h>
#include <etl/functional.h>
#include <etl/iterator.h>
#include <etl/utility.h>
#include <wolfssl/wolfcrypt/settings.h>
#include <wolfssl/wolfcrypt/types.h>

#include "hal/ArchTraits.h"
#include "security/security.h"
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
}

bool BridgeClass::_preDispatch(const bridge::router::CommandContext& ctx,
                               bool needs_ack, bool retransmit_on_dup) {
  if (needs_ack) {
    _processAck(ctx.raw_command, ctx.sequence_id);
  }
  if (ctx.is_duplicate) {
    if (retransmit_on_dup) {
      _retransmitLastFrame();
    }
    return false;
  }
  return true;
}

// =============================================================================
// [ETL] Per-command static dispatch handlers
// Each handler is a static member function: takes BridgeClass& self (explicit
// this) so it can be stored in a plain function pointer (not a member fn ptr),
// enabling a static const dispatch table with ZERO sizeof(BridgeClass) growth.
// Handlers that call static _handleXxx methods use [] (no capture); handlers
// that call non-static _handleXxx methods use [&self].
// =============================================================================

void BridgeClass::_onCmd_StatusAck(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_AckPacket>(
      ctx, [&self](const bridge::router::CommandContext& c,
                   const rpc_pb_AckPacket& m) { self._handleStatusAck(c, m); });
}

// [A] No-payload, no-ack, retransmit-on-dup (idempotent query commands).
void BridgeClass::_onCmd_GetVersion(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<_NoPayload>(
      ctx,
      [&self](const bridge::router::CommandContext& c) { self._handleGetVersion(c); },
      false, true);
}
void BridgeClass::_onCmd_GetFreeMemory(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<_NoPayload>(
      ctx,
      [&self](const bridge::router::CommandContext& c) { self._handleGetFreeMemory(c); },
      false, true);
}
void BridgeClass::_onCmd_GetCapabilities(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<_NoPayload>(
      ctx,
      [&self](const bridge::router::CommandContext& c) { self._handleGetCapabilities(c); },
      false, true);
}

// [B] No-payload, ack, no-retransmit (fire-and-forget control commands).
void BridgeClass::_onCmd_LinkReset(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<_NoPayload>(
      ctx, [&self](const bridge::router::CommandContext& c) { self._handleLinkReset(c); });
}
void BridgeClass::_onCmd_Xoff(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<_NoPayload>(
      ctx, [&self](const bridge::router::CommandContext& c) { self._handleXoff(c); });
}
void BridgeClass::_onCmd_Xon(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<_NoPayload>(
      ctx, [&self](const bridge::router::CommandContext& c) { self._handleXon(c); });
}

// [C] Typed, ack, no-retransmit (standard bidirectional commands).
void BridgeClass::_onCmd_LinkSync(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_LinkSync>(
      ctx, [&self](const bridge::router::CommandContext& c,
                   const rpc_pb_LinkSync& m) { self._handleLinkSync(c, m); });
}
void BridgeClass::_onCmd_SetBaudrate(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_SetBaudratePacket>(
      ctx,
      [&self](const bridge::router::CommandContext&,
              const rpc_pb_SetBaudratePacket& m) { self._handleSetBaudrate(m); });
}
void BridgeClass::_onCmd_EnterBootloader(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_EnterBootloader>(
      ctx, [&self](const bridge::router::CommandContext&,
                   const rpc_pb_EnterBootloader& m) { self._handleEnterBootloader(m); });
}
// Static handlers (_handleSetPinMode, _handleDigitalWrite, _handleAnalogWrite,
// _handleConsoleWrite): []  no-capture lambda is correct (static members).
void BridgeClass::_onCmd_SetPinMode(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_PinMode>(
      ctx, [](const bridge::router::CommandContext&,
              const rpc_pb_PinMode& m) { _handleSetPinMode(m); });
}
void BridgeClass::_onCmd_DigitalWrite(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_DigitalWrite>(
      ctx, [](const bridge::router::CommandContext&,
              const rpc_pb_DigitalWrite& m) { _handleDigitalWrite(m); });
}
void BridgeClass::_onCmd_AnalogWrite(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_AnalogWrite>(
      ctx, [](const bridge::router::CommandContext&,
              const rpc_pb_AnalogWrite& m) { _handleAnalogWrite(m); });
}
void BridgeClass::_onCmd_ConsoleWrite(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_ConsoleWrite>(
      ctx, [](const bridge::router::CommandContext&,
              const rpc_pb_ConsoleWrite& m) { _handleConsoleWrite(m); });
}

// [D] Typed, no-ack, retransmit-on-dup (shared PinRead decode; internal branch).
void BridgeClass::_onCmd_PinRead(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_PinRead>(
      ctx,
      [&self](const bridge::router::CommandContext& c, const rpc_pb_PinRead& m) {
        if (c.raw_command == rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ)) {
          self._handleDigitalRead(c, m);
        } else {
          self._handleAnalogRead(c, m);
        }
      },
      false, true);
}

#if BRIDGE_ENABLE_DATASTORE
void BridgeClass::_onCmd_DatastoreGetResp(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_DatastoreGetResponse>(
      ctx, [](const bridge::router::CommandContext& c,
              const rpc_pb_DatastoreGetResponse& m) {
        _handleDataStoreGetResponse(c, m);
      });
}
#endif

#if BRIDGE_ENABLE_MAILBOX
void BridgeClass::_onCmd_MailboxPush(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_MailboxPush>(
      ctx, [](const bridge::router::CommandContext& c,
              const rpc_pb_MailboxPush& m) { _handleMailboxPush(c, m); });
}

// [E] No ack, no dup-check — response-only messages are always processed.
void BridgeClass::_onCmd_MailboxReadResp(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  rpc_pb_MailboxReadResponse m = {};
  if (!self._decodePayload(ctx,
                      rpc::Payload::get_fields<rpc_pb_MailboxReadResponse>(),
                      &m, rpc::Payload::get_tag<rpc_pb_MailboxReadResponse>(),
                      sizeof(rpc_pb_MailboxReadResponse))) {
    self.emitStatus(rpc::StatusCode::STATUS_MALFORMED);
    return;
  }
  _handleMailboxReadResponse(m);
}
void BridgeClass::_onCmd_MailboxAvailableResp(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  rpc_pb_MailboxAvailableResponse m = {};
  if (!self._decodePayload(
          ctx, rpc::Payload::get_fields<rpc_pb_MailboxAvailableResponse>(), &m,
          rpc::Payload::get_tag<rpc_pb_MailboxAvailableResponse>(),
          sizeof(rpc_pb_MailboxAvailableResponse))) {
    self.emitStatus(rpc::StatusCode::STATUS_MALFORMED);
    return;
  }
  _handleMailboxAvailableResponse(m);
}
#endif

#if BRIDGE_ENABLE_FILESYSTEM
void BridgeClass::_onCmd_FileWrite(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_FileWrite>(
      ctx, [](const bridge::router::CommandContext& c,
              const rpc_pb_FileWrite& m) { _handleFileWrite(c, m); });
}
void BridgeClass::_onCmd_FileRead(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_FileRead>(
      ctx, [](const bridge::router::CommandContext& c,
              const rpc_pb_FileRead& m) { _handleFileRead(c, m); });
}
void BridgeClass::_onCmd_FileRemove(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_FileRemove>(
      ctx, [](const bridge::router::CommandContext& c,
              const rpc_pb_FileRemove& m) { _handleFileRemove(c, m); });
}
void BridgeClass::_onCmd_FileReadResp(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_FileReadResponse>(
      ctx, [](const bridge::router::CommandContext& c,
              const rpc_pb_FileReadResponse& m) { _handleFileReadResponse(c, m); });
}
#endif

#if BRIDGE_ENABLE_PROCESS
void BridgeClass::_onCmd_ProcessKill(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_ProcessKill>(
      ctx, [](const bridge::router::CommandContext& c,
              const rpc_pb_ProcessKill& m) { _handleProcessKill(c, m); });
}
void BridgeClass::_onCmd_ProcessRunAsyncResp(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_ProcessRunAsyncResponse>(
      ctx, [](const bridge::router::CommandContext& c,
              const rpc_pb_ProcessRunAsyncResponse& m) {
        _handleProcessRunAsyncResponse(c, m);
      });
}
void BridgeClass::_onCmd_ProcessPollResp(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_ProcessPollResponse>(
      ctx, [](const bridge::router::CommandContext& c,
              const rpc_pb_ProcessPollResponse& m) {
        _handleProcessPollResponse(c, m);
      });
}
#endif

#if BRIDGE_ENABLE_SPI
void BridgeClass::_onCmd_SpiBegin(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<_NoPayload>(
      ctx,
      [&self](const bridge::router::CommandContext& c) { self._handleSpiBegin(c); });
}
// [D] Typed, no-ack, retransmit-on-dup (SPI transfer query).
void BridgeClass::_onCmd_SpiTransfer(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_SpiTransfer>(
      ctx,
      [&self](const bridge::router::CommandContext& c,
              const rpc_pb_SpiTransfer& m) { self._handleSpiTransfer(c, m); },
      false, true);
}
void BridgeClass::_onCmd_SpiEnd(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<_NoPayload>(
      ctx,
      [&self](const bridge::router::CommandContext& c) { self._handleSpiEnd(c); });
}
void BridgeClass::_onCmd_SpiSetConfig(BridgeClass& self, const bridge::router::CommandContext& ctx) {
  self._dispatchCmd<rpc_pb_SpiConfig>(
      ctx, [](const bridge::router::CommandContext&,
              const rpc_pb_SpiConfig& m) { _handleSpiSetConfig(m); });
}
#endif

// =============================================================================
// [ETL] Static dispatch table — sorted by command_id for O(log N) lower_bound.
// Defined as a translation-unit static (not a class member): zero RAM cost on
// AVR (linker places const arrays in PROGMEM), zero sizeof(BridgeClass) change.
// IMPORTANT: keep entries in ascending command_id order.
// =============================================================================
// clang-format off
const BridgeClass::DispatchEntry BridgeClass::k_dispatch_table[] = {
    {rpc::to_underlying(rpc::StatusCode::STATUS_ACK),            &BridgeClass::_onCmd_StatusAck},
    {rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION),        &BridgeClass::_onCmd_GetVersion},
    {rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY),    &BridgeClass::_onCmd_GetFreeMemory},
    {rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC),          &BridgeClass::_onCmd_LinkSync},
    {rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET),         &BridgeClass::_onCmd_LinkReset},
    {rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES),   &BridgeClass::_onCmd_GetCapabilities},
    {rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE),       &BridgeClass::_onCmd_SetBaudrate},
    {rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER),   &BridgeClass::_onCmd_EnterBootloader},
    {rpc::to_underlying(rpc::CommandId::CMD_XOFF),               &BridgeClass::_onCmd_Xoff},
    {rpc::to_underlying(rpc::CommandId::CMD_XON),                &BridgeClass::_onCmd_Xon},
    {rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE),       &BridgeClass::_onCmd_SetPinMode},
    {rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE),      &BridgeClass::_onCmd_DigitalWrite},
    {rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE),       &BridgeClass::_onCmd_AnalogWrite},
    {rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ),       &BridgeClass::_onCmd_PinRead},
    {rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ),        &BridgeClass::_onCmd_PinRead},
    {rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE),      &BridgeClass::_onCmd_ConsoleWrite},
#if BRIDGE_ENABLE_DATASTORE
    {rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP), &BridgeClass::_onCmd_DatastoreGetResp},
#endif
#if BRIDGE_ENABLE_MAILBOX
    {rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH),            &BridgeClass::_onCmd_MailboxPush},
    {rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP),       &BridgeClass::_onCmd_MailboxReadResp},
    {rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP),  &BridgeClass::_onCmd_MailboxAvailableResp},
#endif
#if BRIDGE_ENABLE_FILESYSTEM
    {rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE),         &BridgeClass::_onCmd_FileWrite},
    {rpc::to_underlying(rpc::CommandId::CMD_FILE_READ),          &BridgeClass::_onCmd_FileRead},
    {rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE),        &BridgeClass::_onCmd_FileRemove},
    {rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP),     &BridgeClass::_onCmd_FileReadResp},
#endif
#if BRIDGE_ENABLE_PROCESS
    {rpc::to_underlying(rpc::CommandId::CMD_PROCESS_KILL),             &BridgeClass::_onCmd_ProcessKill},
    {rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP),   &BridgeClass::_onCmd_ProcessRunAsyncResp},
    {rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP),        &BridgeClass::_onCmd_ProcessPollResp},
#endif
#if BRIDGE_ENABLE_SPI
    {rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN),          &BridgeClass::_onCmd_SpiBegin},
    {rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER),       &BridgeClass::_onCmd_SpiTransfer},
    {rpc::to_underlying(rpc::CommandId::CMD_SPI_END),            &BridgeClass::_onCmd_SpiEnd},
    {rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG),     &BridgeClass::_onCmd_SpiSetConfig},
#endif
};
// clang-format on
const size_t BridgeClass::k_dispatch_table_size =
    sizeof(BridgeClass::k_dispatch_table) /
    sizeof(BridgeClass::k_dispatch_table[0]);

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
    (void)sendFrame(rpc::StatusCode::STATUS_ERROR, ctx.sequence_id);
    return;
  }

  // [SIL-2] Pre-checks: commands with unique protocol semantics that cannot be
  // expressed as uniform table entries (no ack/dup contract, or ack-only).
  if (ctx.raw_command == rpc::to_underlying(rpc::StatusCode::STATUS_OK)) {
    _processAck(ctx.raw_command, ctx.sequence_id);
    return;
  }
  if (ctx.raw_command == rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED)) {
    _handleStatusMalformed(ctx);
    return;
  }

  // [ETL] O(log N) binary search in the static sorted dispatch table.
  // Table is defined in Bridge.cpp and never mutated at runtime (zero RAM growth).
  const DispatchEntry key{ctx.raw_command, nullptr};
  const DispatchEntry* const table_end =
      k_dispatch_table + k_dispatch_table_size;
  const DispatchEntry* found = etl::lower_bound(
      k_dispatch_table, table_end, key,
      [](const DispatchEntry& e, const DispatchEntry& k) {
        return e.command_id < k.command_id;
      });
  if (found != table_end && found->command_id == ctx.raw_command) {
    found->fn(*this, ctx);
  } else {
    onUnknownCommand(ctx);
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
    _hardware_serial = nullptr;}

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
#if BRIDGE_ENABLE_POST_TESTS
  _is_post_passed = rpc::security::run_cryptographic_self_tests();
  if (!_is_post_passed) enterSafeState();
#else
  _is_post_passed = true;
#endif
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
  // [SIL-2/H-2] Handshake response watchdog: fires EvTimeout if MPU does not
  // complete CMD_LINK_SYNC within _response_timeout_ms after a reset.
  _timer_ids[bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT] =
      _timers.register_timer([]() { Bridge._onHandshakeTimeout(); },
                             _response_timeout_ms,
                             etl::timer::mode::SINGLE_SHOT);
  _timers.start(_timer_ids[bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT]);
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

void BridgeClass::_serialize_and_send(const rpc_pb_RpcEnvelope& env) {
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> buffer;
  const size_t len = rpc::serialize_frame(env, buffer);
  if (len > 0)
    _packet_serial.send(_stream, etl::span<const uint8_t>(buffer.data(), len));
}

bool BridgeClass::_sendFrameRaw(const rpc_pb_RpcEnvelope& env,
                                uint16_t command_id) {
  if (!_tx_enabled && !rpc::is_system_command(command_id)) return false;
  _serialize_and_send(env);
  return true;
}

void BridgeClass::_transmit(uint16_t command_id, uint16_t sequence_id,
                            etl::span<const uint8_t> payload) {
  const uint16_t raw_cmd = command_id;
  const bool is_excluded = rpc::is_system_command(raw_cmd);
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
  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  env.version = rpc::PROTOCOL_VERSION;
  env.command_id = command_id;
  env.sequence_id = sequence_id;
  etl::copy_n(nonce.begin(), rpc::AEAD_NONCE_SIZE, env.nonce.bytes);
  env.nonce.size = static_cast<pb_size_t>(rpc::AEAD_NONCE_SIZE);
  const size_t pl_size = etl::min(final_payload.size(),
                                  static_cast<size_t>(rpc::MAX_PAYLOAD_SIZE));
  env.which_payload_type = rpc_pb_RpcEnvelope_encrypted_payload_with_tag_tag;
  if (do_encrypt) {
    etl::copy_n(final_payload.begin(), pl_size,
                env.payload_type.encrypted_payload_with_tag.bytes);
    etl::copy_n(tag.begin(), rpc::AEAD_TAG_SIZE,
                env.payload_type.encrypted_payload_with_tag.bytes + pl_size);
    env.payload_type.encrypted_payload_with_tag.size =
        static_cast<pb_size_t>(pl_size + rpc::AEAD_TAG_SIZE);
  } else {
    etl::copy_n(final_payload.begin(), pl_size,
                env.payload_type.encrypted_payload_with_tag.bytes);
    env.payload_type.encrypted_payload_with_tag.size =
        static_cast<pb_size_t>(pl_size);
  }
  _serialize_and_send(env);
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
  (void)send(rpc::StatusCode::STATUS_ACK, sequence_id, p);
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

// [SIL-2/H-2] Fires if the MPU has not completed the handshake within
// _response_timeout_ms. Drives the FSM to FAULT, which calls
// hal::forceSafeState() on entry, then disables TX.
void BridgeClass::_onHandshakeTimeout() {
  if (_fsm.isSynchronized()) return;  // stale callback after a late sync
  _fsm.receive(bridge::fsm::EvTimeout());
  _tx_enabled = false;
}
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
  constexpr etl::array<etl::pair<rpc_pb_PinModeType, uint8_t>, 3> kPinModeMap{{
      {rpc_pb_PinModeType_PIN_INPUT, INPUT},
      {rpc_pb_PinModeType_PIN_OUTPUT, OUTPUT},
      {rpc_pb_PinModeType_PIN_INPUT_PULLUP, INPUT_PULLUP},
  }};
  auto it = etl::find_if(kPinModeMap.begin(), kPinModeMap.end(),
                         [&m](const etl::pair<rpc_pb_PinModeType, uint8_t>& p) {
                           return p.first == m.mode;
                         });
  const uint8_t m_val = (it != kPinModeMap.end()) ? it->second : INPUT;
  pinMode(m.pin, m_val);
}

void BridgeClass::_handleDigitalWrite(const rpc_pb_DigitalWrite& m) {
  digitalWrite(m.pin, (m.value == 0) ? LOW : HIGH);
}
void BridgeClass::_handleAnalogWrite(const rpc_pb_AnalogWrite& m) {
  analogWrite(m.pin, static_cast<int>(m.value));  // [SIL-2/H-6] no C-cast
}

void BridgeClass::_handleDigitalRead(const bridge::router::CommandContext& ctx,
                                     const rpc_pb_PinRead& m) {
  if (m.pin < bridge::config::DIGITAL_PINS) {
    rpc_pb_DigitalReadResponse resp = rpc_pb_DigitalReadResponse_init_default;
    resp.value = static_cast<uint32_t>(::digitalRead(m.pin));
    if (!send(rpc::CommandId::CMD_DIGITAL_READ_RESP, ctx.sequence_id, resp))
      emitStatus(rpc::StatusCode::STATUS_ERROR);
  } else
    emitStatus(rpc::StatusCode::STATUS_ERROR);
}

void BridgeClass::_handleAnalogRead(const bridge::router::CommandContext& ctx,
                                    const rpc_pb_PinRead& m) {
  // [SIL-2/H-7] Analog pins are indexed differently from digital pins.
  // #if guard is used instead of `if constexpr` because cppcheck does not
  // honour constexpr branch elimination and would flag the unsigned comparison
  // `m.pin < ANALOG_PINS` as [unsignedLessThanZero] when ANALOG_PINS == 0
  // (non-AVR/SAMD fallback target).  The preprocessor guard ensures cppcheck
  // only sees the `emitStatus` path on the fallback configuration.
#if defined(ARDUINO_ARCH_AVR) || defined(ARDUINO_ARCH_SAMD)
  if (m.pin < bridge::config::ANALOG_PINS) {
    rpc_pb_AnalogReadResponse resp = rpc_pb_AnalogReadResponse_init_default;
    resp.value = static_cast<uint32_t>(::analogRead(m.pin));
    if (!send(rpc::CommandId::CMD_ANALOG_READ_RESP, ctx.sequence_id, resp))
      emitStatus(rpc::StatusCode::STATUS_ERROR);
  } else {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
#else
  // Fallback target: no analog pins — every request is out of range.
  static_cast<void>(ctx);
  static_cast<void>(m);
  emitStatus(rpc::StatusCode::STATUS_ERROR);
#endif

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
  MailboxClass::_onPush(m);
}
void BridgeClass::_handleMailboxReadResponse(
    const rpc_pb_MailboxReadResponse& m) {
  MailboxClass::_onReadResponse(m);
}
void BridgeClass::_handleMailboxAvailableResponse(
    const rpc_pb_MailboxAvailableResponse& m) {
  MailboxClass::_onAvailableResponse(m);
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
  // [SIL-2/H-5] Use the dedicated _spi_buffer instead of _rx_buffer.
  // _rx_buffer is owned by PacketSerial and can be written by a serial ISR
  // (on ESP32/SAMD) while a blocking SPI transfer is in progress.
  size_t len = etl::min(static_cast<size_t>(m.data.size), _spi_buffer.size());
  etl::copy_n(m.data.bytes, len, _spi_buffer.begin());
  size_t tr = SPIService.transfer(etl::span<uint8_t>(_spi_buffer.data(), len));
  if (tr == 0) {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }
  rpc_pb_SpiTransferResponse resp = rpc_pb_SpiTransferResponse_init_default;
  const size_t to_copy = etl::min(len, sizeof(resp.data.bytes));
  resp.data.size = static_cast<pb_size_t>(to_copy);
  if (to_copy > 0) etl::copy_n(_spi_buffer.data(), to_copy, resp.data.bytes);
  if (!send(rpc::CommandId::CMD_SPI_TRANSFER_RESP, ctx.sequence_id, resp))
    emitStatus(rpc::StatusCode::STATUS_ERROR);
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
  // [SIL-2/H-2] Handshake complete: cancel the watchdog timer so it does not
  // fire a spurious EvTimeout after a successful synchronisation.
  _timers.stop(_timer_ids[bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT]);
  (void)send(rpc::CommandId::CMD_LINK_SYNC_RESP, ctx.sequence_id, resp);
}

void BridgeClass::_handleLinkReset(const bridge::router::CommandContext& ctx) {
  if (ctx.envelope->which_payload_type != 0) {
    rpc_pb_HandshakeConfig res_msg = rpc_pb_HandshakeConfig_init_default;
    if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_HandshakeConfig>(),
                       &res_msg,
                       rpc::Payload::get_tag<rpc_pb_HandshakeConfig>(),
                       sizeof(rpc_pb_HandshakeConfig))) {
      _applyTimingConfig(res_msg);
    }
  }
  _fsm.receive(bridge::fsm::EvReset());
  // [SIL-2/H-2] Restart the handshake watchdog with the (possibly updated)
  // _response_timeout_ms. If the MPU does not complete CMD_LINK_SYNC within
  // this window, _onHandshakeTimeout() will drive the FSM to FAULT.
  _timers.stop(_timer_ids[bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT]);
  _timers.set_period(_timer_ids[bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT],
                     _response_timeout_ms);
  _timers.start(_timer_ids[bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT]);
  (void)sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP, ctx.sequence_id);
}

void BridgeClass::_handleGetCapabilities(
    const bridge::router::CommandContext& ctx) {
  rpc_pb_Capabilities resp = rpc_pb_Capabilities_init_default;
  bridge::hal::fillCapabilities(resp);
  (void)send(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, ctx.sequence_id, resp);
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
  (void)send(rpc::CommandId::CMD_GET_VERSION_RESP, ctx.sequence_id, resp);
}

void BridgeClass::_handleGetFreeMemory(
    const bridge::router::CommandContext& ctx) {
  rpc_pb_FreeMemoryResponse resp = {};
  resp.value = (uint32_t)bridge::hal::getFreeMemory();
  (void)send(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, ctx.sequence_id, resp);
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
  const uint16_t raw_cmd = envelope.command_id;
  const bool is_excluded = rpc::is_system_command(raw_cmd);
  if (isSynchronized() && !_shared_secret.empty() && !is_excluded) {
    if (envelope.payload_type.encrypted_payload_with_tag.size < 16) {
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }
    const size_t ct_size =
        envelope.payload_type.encrypted_payload_with_tag.size - 16;
    etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> dec_pl;
    if (!rpc::security::aead_decrypt_frame(
            raw_cmd, envelope.sequence_id,
            etl::span<const uint8_t>(
                envelope.payload_type.encrypted_payload_with_tag.bytes,
                ct_size),
            etl::span<const uint8_t>(
                envelope.payload_type.encrypted_payload_with_tag.bytes +
                    ct_size,
                16),
            _session_key, etl::span<const uint8_t>(envelope.nonce.bytes, 12),
            dec_pl) ||
        !rpc::security::validate_frame_nonce(
            etl::span<const uint8_t>(envelope.nonce.bytes, 12),
            &_rx_nonce_counter)) {
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }
    etl::copy_n(dec_pl.data(), ct_size,
                envelope.payload_type.encrypted_payload_with_tag.bytes);
    envelope.payload_type.encrypted_payload_with_tag.size =
        static_cast<pb_size_t>(ct_size);
  }
  _dispatchCommand(envelope);
}

bool BridgeClass::_isSecurityCheckPassed(uint16_t cmd) const {
  if (_shared_secret.empty()) return true;
  if (rpc::is_system_command(cmd)) return true;
  return _fsm.isSynchronized();
}

void BridgeClass::signalXoff() { (void)sendFrame(rpc::CommandId::CMD_XOFF); }
void BridgeClass::signalXon() { (void)sendFrame(rpc::CommandId::CMD_XON); }

bool BridgeClass::_decodePayload(const bridge::router::CommandContext& ctx,
                                 const pb_msgdesc_t* fields, void* dest,
                                 pb_size_t expected_tag, size_t struct_size) {
  // [SIL-2/H-3] Both branches now use pb_decode_noinit, which is the only
  // safe, standard-conforming way to deserialise a Nanopb message from a
  // byte buffer. The previous 'else' branch used reinterpret_cast + copy_n,
  // which is UB in C++17 and could misfire on targets with different struct
  // padding (e.g. SAMD/ESP32 vs AVR).
  const uint8_t* src = nullptr;
  size_t src_len = 0U;
  if (ctx.envelope->which_payload_type ==
      rpc_pb_RpcEnvelope_encrypted_payload_with_tag_tag) {
    src = ctx.envelope->payload_type.encrypted_payload_with_tag.bytes;
    src_len = ctx.envelope->payload_type.encrypted_payload_with_tag.size;
  } else if (ctx.envelope->which_payload_type == expected_tag) {
    // The expected typed-union member is serialised by Nanopb at the start
    // of the union. Extract the bytes from the first member (always present).
    src = ctx.envelope->payload_type.encrypted_payload_with_tag.bytes;
    src_len = ctx.envelope->payload_type.encrypted_payload_with_tag.size;
  } else {
    return false;
  }
  (void)struct_size;  // retained in signature for API stability
  pb_istream_t stream = pb_istream_from_buffer(src, src_len);
  return pb_decode_noinit(&stream, fields, dest);
}

bool BridgeClass::_sendEncryptedHelper(uint16_t raw_cmd, uint16_t seq,
                                       const pb_msgdesc_t* fields,
                                       const void* packet) {
  if (is_reliable_cmd(raw_cmd)) {
    BRIDGE_ATOMIC_BLOCK {
      if (_pending_tx_queue.full()) return false;
      auto* buf = _tx_payload_pool.allocate();
      if (!buf) return false;
      pb_ostream_t out_stream =
          pb_ostream_from_buffer(buf->data.data(), buf->data.size());
      if (pb_encode(&out_stream, fields, packet)) {
        _pending_tx_queue.push_back(
            {raw_cmd, seq, buf, out_stream.bytes_written});
        if (!_fsm.isAwaitingAck()) _flushPendingTxQueue();
        return true;
      }
      _tx_payload_pool.release(buf);
      return false;
    }
  } else {
    pb_ostream_t out_stream =
        pb_ostream_from_buffer(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE);
    if (pb_encode(&out_stream, fields, packet)) {
      _transmit(raw_cmd, seq,
                etl::span<const uint8_t>(_transient_buffer.data(),
                                         out_stream.bytes_written));
      return true;
    }
    return false;
  }
}

namespace bridge {
void SafeStatePolicy::handle(::BridgeClass& bridge, const etl::exception&) {
  bridge.enterSafeState();
}
}  // namespace bridge

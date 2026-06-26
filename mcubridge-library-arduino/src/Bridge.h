/**
 * @file Bridge.h
 * @brief Arduino MCU Bridge v2 - MCU-side RPC library.
 */

#ifndef BRIDGE_H
#define BRIDGE_H

#include <stdint.h>

#include "etl_profile.h"
#include "hal/hal.h"

#if defined(ARDUINO_ARCH_AVR)
#include <avr/wdt.h>
#endif
// clang-format off
#include <PacketSerial.h>
#include <Codecs/COBS.h>
// clang-format on
#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/callback_timer.h>
#include <etl/delegate.h>
#include <etl/deque.h>
#include <etl/fsm.h>
#include <etl/pool.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <etl/variant.h>
#include <etl/vector.h>
#include <etl/visitor.h>

#include "config/bridge_config.h"
#include "etl_ext/CounterIterator.h"
#include "fsm/bridge_fsm.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "security/security.h"

// [SIL-2] Template De-bloating: Extern declarations
namespace etl {
extern template class span<uint8_t>;
extern template class span<const uint8_t>;
extern template class span<char>;
extern template class span<const char>;
}  // namespace etl

namespace bridge {
namespace router {
struct CommandContext {
  const rpc_pb_RpcEnvelope* envelope;
  uint16_t raw_command;
  uint16_t sequence_id;
  bool is_duplicate;
  bool requires_ack;
  CommandContext(const rpc_pb_RpcEnvelope* f, uint16_t cmd, uint16_t seq,
                 bool dup, bool ack)
      : envelope(f),
        raw_command(cmd),
        sequence_id(seq),
        is_duplicate(dup),
        requires_ack(ack) {}
};

using DecodedCommand =
    etl::variant<etl::monostate, rpc_pb_AckPacket, rpc_pb_LinkSync,
                 rpc_pb_SetBaudratePacket, rpc_pb_EnterBootloader,
                 rpc_pb_PinMode, rpc_pb_DigitalWrite, rpc_pb_AnalogWrite,
                 rpc_pb_PinRead, rpc_pb_ConsoleWrite
#if BRIDGE_ENABLE_DATASTORE
                 ,
                 rpc_pb_DatastoreGetResponse
#endif
#if BRIDGE_ENABLE_MAILBOX
                 ,
                 rpc_pb_MailboxPush, rpc_pb_MailboxReadResponse,
                 rpc_pb_MailboxAvailableResponse
#endif
#if BRIDGE_ENABLE_FILESYSTEM
                 ,
                 rpc_pb_FileWrite, rpc_pb_FileRead, rpc_pb_FileRemove,
                 rpc_pb_FileReadResponse
#endif
#if BRIDGE_ENABLE_PROCESS
                 ,
                 rpc_pb_ProcessKill, rpc_pb_ProcessRunAsyncResponse,
                 rpc_pb_ProcessPollResponse
#endif
#if BRIDGE_ENABLE_SPI
                 ,
                 rpc_pb_SpiTransfer, rpc_pb_SpiConfig
#endif
                 >;

struct DecodedResult {
  bool success;
  DecodedCommand command;
};

}  // namespace router
}  // namespace bridge

#include "ErrorPolicy.h"

class BridgeClass {
 public:
  using ErrorPolicy = bridge::SafeStatePolicy;
  explicit BridgeClass(Stream& stream);

  void begin(uint32_t baudrate = 0, const char* secret = nullptr);
  void process();
  bool isSynchronized() const;

  // Explicit registration if needed, otherwise direct calls
  void enterSafeState();

  template <typename T = etl::span<const uint8_t>>
  void emitStatus(rpc::StatusCode s,
                  const T& payload = etl::span<const uint8_t>()) {
    if constexpr (etl::is_same_v<T, etl::span<const uint8_t>>) {
      if (!sendFrame(s, 0, payload)) {
      }
    } else if constexpr (etl::is_same_v<T, etl::string_view>) {
      rpc_pb_GenericResponse resp = rpc_pb_GenericResponse_init_default;
      const size_t to_copy =
          etl::min(payload.size(), sizeof(resp.message) - 1U);
      if (to_copy > 0U) etl::copy_n(payload.begin(), to_copy, resp.message);
      resp.message[to_copy] = 0;
      if (!send(s, 0, resp)) {
      }
    }
  }
  void emitStatus(rpc::StatusCode s) {
    emitStatus(s, etl::span<const uint8_t>());
  }

  void signalXoff();
  void signalXon();

  template <typename T>
  [[nodiscard]] bool sendFrame(T command, uint16_t seq = 0,
                               etl::span<const uint8_t> p = {}) {
    static_assert(etl::is_enum_v<T> || etl::is_integral_v<T>,
                  "Command must be enum or integral");
    const uint16_t cmd = static_cast<uint16_t>(command);
    const bool is_system =
        (cmd >= rpc::RPC_STATUS_CODE_MIN && cmd <= rpc::RPC_STATUS_CODE_MAX) ||
        (cmd >= rpc::RPC_SYSTEM_COMMAND_MIN &&
         cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);
    if (!_tx_enabled && !is_system) return false;
    if (is_reliable_cmd(cmd)) {
      BRIDGE_ATOMIC_BLOCK {
        if (_pending_tx_queue.full()) return false;
        auto* buf = _tx_payload_pool.allocate();
        if (!buf) return false;
        const size_t pl_size = etl::min(p.size(), buf->data.size());
        etl::copy_n(p.data(), pl_size, buf->data.data());
        _pending_tx_queue.push_back({cmd, seq, buf, pl_size});
        if (!_fsm.isAwaitingAck()) _flushPendingTxQueue();
      }
      return true;
    }
    _transmit(cmd, seq, p);
    return true;
  }

  template <typename T>
  [[nodiscard]] bool sendSinglePass(uint16_t command_id, uint16_t sequence_id,
                                    const T& packet) {
    const bool is_system = (command_id >= rpc::RPC_STATUS_CODE_MIN &&
                            command_id <= rpc::RPC_STATUS_CODE_MAX) ||
                           (command_id >= rpc::RPC_SYSTEM_COMMAND_MIN &&
                            command_id <= rpc::RPC_SYSTEM_COMMAND_MAX);
    if (!_tx_enabled && !is_system) return false;

    etl::array<uint8_t, rpc::MAX_FRAME_SIZE> buffer;
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id = command_id;
    env.sequence_id = sequence_id;
    rpc::Payload::set<T>(env, packet);
    size_t len = rpc::serialize_frame(env, buffer);
    if (len > 0) {
      _packet_serial.send(_stream,
                          etl::span<const uint8_t>(buffer.data(), len));
      return true;
    }
    return false;
  }

  template <typename T>
  [[nodiscard]] bool send(rpc::StatusCode s, uint16_t seq, const T& packet) {
    return sendSinglePass<T>(rpc::to_underlying(s), seq, packet);
  }

  template <typename T>
  [[nodiscard]] bool send(rpc::CommandId c, uint16_t seq, const T& packet) {
    const uint16_t raw_cmd = rpc::to_underlying(c);
    const bool is_excluded = (raw_cmd >= rpc::RPC_STATUS_CODE_MIN &&
                              raw_cmd <= rpc::RPC_STATUS_CODE_MAX) ||
                             (raw_cmd >= rpc::RPC_SYSTEM_COMMAND_MIN &&
                              raw_cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);
    const bool do_encrypt =
        isSynchronized() && !_shared_secret.empty() && !is_excluded;

    if (do_encrypt) {
      return _sendEncryptedHelper(raw_cmd, seq, rpc::Payload::get_fields<T>(),
                                  &packet);
    } else {
      return sendSinglePass<T>(raw_cmd, seq, packet);
    }
  }

  using CommandHandler = etl::delegate<void(const rpc_pb_RpcEnvelope&)>;
  using StatusHandler =
      etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;
  void onCommand(CommandHandler h) { _command_handler = h; }
  void onStatus(StatusHandler h) { _status_handler = h; }
  void flushStream() { _stream.flush(); }

  void _dispatchCommand(const rpc_pb_RpcEnvelope& envelope);
  static void _onBootloaderDelay();
  void _onAckTimeout();
  void _onRxDedupe();
  void _onBaudrateChange();
  void _retransmitLastFrame();
  bool _isSecurityCheckPassed(uint16_t command_id) const;

  static constexpr bool is_reliable_cmd(uint16_t id) {
    return rpc::requires_ack(id);
  }

 protected:
  struct TxPayloadBuffer {
    etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> data;
  };
  struct PendingTxFrame {
    uint16_t command_id;
    uint16_t sequence_id;
    TxPayloadBuffer* buffer;
    size_t length;
  };

  __attribute__((noinline)) void _transmit(uint16_t command_id,
                                           uint16_t sequence_id,
                                           etl::span<const uint8_t> payload);
  void _initializeRuntime();

  // STRICT ORDER FOR CONSTRUCTOR
  Stream& _stream;
  HardwareSerial* _hardware_serial = nullptr;
  CommandHandler _command_handler;
  StatusHandler _status_handler;
  uint16_t _last_command_id = 0;
  uint16_t _tx_sequence_id = 0;
  uint8_t _retry_count = 0;
  uint8_t _retry_limit = rpc::RPC_DEFAULT_RETRY_LIMIT;
  uint16_t _ack_timeout_ms = rpc::RPC_DEFAULT_ACK_TIMEOUT_MS;
  uint32_t _response_timeout_ms = rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS;
  uint32_t _pending_baudrate = 0;

  etl::array<uint8_t, bridge::config::RX_BUFFER_SIZE> _rx_buffer;
  PacketSerial2::PacketSerial<PacketSerial2::COBS, PacketSerial2::NoCRC,
                              PacketSerial2::NoLock, PacketSerial2::NoWatchdog>
      _packet_serial;

  etl::vector<uint8_t, 64> _shared_secret;
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> _session_key;
  uint64_t _tx_nonce_counter = 0;
  uint64_t _rx_nonce_counter = 0;
  bridge::fsm::BridgeFsm _fsm;

  static __attribute__((noinline)) void _watchdogTask();
  __attribute__((noinline)) void _serialTask();
  __attribute__((noinline)) void _timerTask();

  uint32_t _timer_last_tick_ms = 0;
  bool _serial_xoff_sent = false;

  etl::callback_timer<bridge::scheduler::NUMBER_OF_TIMERS> _timers;
  etl::array<etl::timer::id::type, bridge::scheduler::NUMBER_OF_TIMERS>
      _timer_ids;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> _transient_buffer;

  bool _is_post_passed = false;
  bool _tx_enabled = true;

  etl::pool<TxPayloadBuffer, bridge::config::MAX_PENDING_TX_FRAMES>
      _tx_payload_pool;
  etl::deque<PendingTxFrame, bridge::config::MAX_PENDING_TX_FRAMES>
      _pending_tx_queue;

  etl::circular_buffer<uint16_t, bridge::config::RX_HISTORY_SIZE> _rx_history;

  void _applyTimingConfig(const rpc::payload::HandshakeConfig& msg);

  void _handleStatusOk(const bridge::router::CommandContext& ctx);
  void _handleStatusMalformed(const bridge::router::CommandContext& ctx);
  void _handleStatusAck(const bridge::router::CommandContext& ctx,
                        const rpc_pb_AckPacket& m);
  void _handleGetVersion(const bridge::router::CommandContext& ctx);
  void _handleGetFreeMemory(const bridge::router::CommandContext& ctx);
  __attribute__((noinline)) void _handleLinkSync(
      const bridge::router::CommandContext& ctx, const rpc_pb_LinkSync& m);
  void _handleLinkReset(const bridge::router::CommandContext& ctx);
  void _handleGetCapabilities(const bridge::router::CommandContext& ctx);
  void _handleXoff(const bridge::router::CommandContext& ctx);
  void _handleXon(const bridge::router::CommandContext& ctx);
  void _handleSetBaudrate(const rpc::payload::SetBaudratePacket& msg);
  void _handleSetTiming(const rpc::payload::HandshakeConfig& msg);
  void _handleEnterBootloader(const rpc::payload::EnterBootloader& msg);
  void _handleSpiBegin(const bridge::router::CommandContext& ctx);
  void _handleSpiEnd(const bridge::router::CommandContext& ctx);
  __attribute__((noinline)) void _handleSpiTransfer(
      const bridge::router::CommandContext& ctx, const rpc_pb_SpiTransfer& m);
  __attribute__((noinline)) void _handleReceivedFrame(
      etl::span<const uint8_t> p);
  void onUnknownCommand(const bridge::router::CommandContext& ctx);

  void _processAck(uint16_t command_id, uint16_t sequence_id);

  static bool _decodePayload(const bridge::router::CommandContext& ctx,
                             const pb_msgdesc_t* fields, void* dest,
                             pb_size_t expected_tag, size_t struct_size);

  bridge::router::DecodedResult _decodePayloadToVariant(
      const bridge::router::CommandContext& ctx);

  friend struct CommandVisitor;

  void _handleSetPinMode(const rpc_pb_PinMode& m);
  void _handleDigitalWrite(const rpc_pb_DigitalWrite& m);
  void _handleAnalogWrite(const rpc_pb_AnalogWrite& m);
  void _handleDigitalRead(const bridge::router::CommandContext& ctx,
                          const rpc_pb_PinRead& m);
  void _handleAnalogRead(const bridge::router::CommandContext& ctx,
                         const rpc_pb_PinRead& m);
  void _handleConsoleWrite(const rpc_pb_ConsoleWrite& m);
  void _handleDataStoreGetResponse(const bridge::router::CommandContext& ctx,
                                   const rpc_pb_DatastoreGetResponse& m);
  void _handleFileWrite(const bridge::router::CommandContext& ctx,
                        const rpc_pb_FileWrite& m);
  void _handleFileRead(const bridge::router::CommandContext& ctx,
                       const rpc_pb_FileRead& m);
  void _handleFileRemove(const bridge::router::CommandContext& ctx,
                         const rpc_pb_FileRemove& m);
  void _handleFileReadResponse(const bridge::router::CommandContext& ctx,
                               const rpc_pb_FileReadResponse& m);
  void _handleProcessKill(const bridge::router::CommandContext& ctx,
                          const rpc_pb_ProcessKill& m);
  void _handleProcessRunAsyncResponse(const bridge::router::CommandContext& ctx,
                                      const rpc_pb_ProcessRunAsyncResponse& m);
  void _handleProcessPollResponse(const bridge::router::CommandContext& ctx,
                                  const rpc_pb_ProcessPollResponse& m);
  void _handleSpiSetConfig(const rpc_pb_SpiConfig& m);
#if BRIDGE_ENABLE_MAILBOX
  void _handleMailboxPush(const bridge::router::CommandContext& ctx,
                          const rpc_pb_MailboxPush& m);
  void _handleMailboxReadResponse(const rpc_pb_MailboxReadResponse& m);
  void _handleMailboxAvailableResponse(
      const rpc_pb_MailboxAvailableResponse& m);
#endif
  bool _sendEncryptedHelper(uint16_t raw_cmd, uint16_t seq,
                            const pb_msgdesc_t* fields, const void* packet);

  void _clearPendingTxQueue();
  void _flushPendingTxQueue();
  void _handleAck(uint16_t command_id);
};

#ifndef BRIDGE_NO_GLOBAL_EXTERN
extern BridgeClass Bridge;
#endif

#endif

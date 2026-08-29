/**
 * @file Bridge.h
 * @brief Arduino MCU Bridge v2 - MCU-side RPC library.
 */

#ifndef BRIDGE_H
#define BRIDGE_H

#include <Arduino.h>
#include <stdint.h>

#if defined(ARDUINO_ARCH_AVR)
#include <avr/wdt.h>
#endif

#ifdef min
#undef min
#endif
#ifdef max
#undef max
#endif

// clang-format off
#include <Embedded_Template_Library.h>
#include <wolfssl.h>
#include <wolfssl/wolfcrypt/settings.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/bitset.h>
#include <etl/callback_timer.h>
#include <etl/delegate.h>
#include <etl/deque.h>
#include <etl/fsm.h>
#include <etl/observer.h>
#include <etl/pool.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <etl/vector.h>
#include <PacketSerial.h>
#include <Codecs/COBSR.h>
// clang-format on

#include "config/bridge_config.h"
#include "fsm/bridge_fsm.h"
#include "hal/hal.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"

// [SIL-2] Template De-bloating: Extern declarations
namespace etl {
extern template class span<uint8_t>;
extern template class span<const uint8_t>;
extern template class span<char>;
extern template class span<const char>;
}  // namespace etl

namespace bridge {

enum class SystemEvent : uint8_t {
  SAFE_STATE_ENTERED = 0,
  SYNCHRONIZED = 1,
  UNSYNCHRONIZED = 2
};

class BridgeObserver {
 public:
  virtual ~BridgeObserver() = default;
  virtual void notification(SystemEvent event) {
    if (event == SystemEvent::SAFE_STATE_ENTERED ||
        event == SystemEvent::UNSYNCHRONIZED) {
      onLost();
    }
  }
  virtual void onLost() {}
};

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

}  // namespace router

namespace utils {

template <typename Src, typename DstElem, size_t DstSize>
constexpr size_t copy_to_buf(const Src& src, DstElem (&dst)[DstSize]) {
  const size_t to_copy = etl::min(src.size(), DstSize - 1U);
  if (to_copy > 0U) {
    etl::copy_n(src.begin(), to_copy, dst);
  }
  dst[to_copy] = static_cast<DstElem>(0);
  return to_copy;
}

template <typename Src, typename DstElem, size_t DstSize>
constexpr size_t copy_bytes_to_buf(const Src& src, DstElem (&dst)[DstSize]) {
  const size_t to_copy = etl::min(src.size(), DstSize);
  if (to_copy > 0U) {
    etl::copy_n(src.data(), to_copy, dst);
  }
  return to_copy;
}

}  // namespace utils
}  // namespace bridge

class BridgeClass : public etl::observable<bridge::BridgeObserver,
                                           bridge::config::MAX_OBSERVERS> {
 public:
  explicit BridgeClass(Stream& stream);

  void begin(uint32_t baudrate = 0, const char* secret = nullptr);
  void process();
  bool isSynchronized() const;
  bool isPostPassed() const { return _state_flags.test(FLAG_POST_PASSED); }
  uint32_t getWcetMaxMicros() const { return _wcet_max_micros; }
  void resetWcetStats() { _wcet_max_micros = 0; }
  static uint16_t getFreeStackMargin() {
    return bridge::hal::getFreeStackMargin();
  }

  // Explicit registration if needed, otherwise direct calls
  void enterSafeState();

  template <typename T = etl::span<const uint8_t>>
  void emitStatus(rpc::StatusCode s,
                  const T& payload = etl::span<const uint8_t>()) {
    if constexpr (etl::is_same_v<T, etl::span<const uint8_t>>) {
      if (!sendFrame(s, 0, payload)) {
        enterSafeState();
      }
    } else if constexpr (etl::is_same_v<T, etl::string_view>) {
      rpc_pb_GenericResponse resp = rpc_pb_GenericResponse_init_default;
      const size_t to_copy =
          etl::min(payload.size(), sizeof(resp.message) - 1U);
      if (to_copy > 0U) etl::copy_n(payload.begin(), to_copy, resp.message);
      resp.message[to_copy] = 0;
      if (!send(s, 0, resp)) {
        enterSafeState();
      }
    }
  }

  void signalXoff();
  void signalXon();

  template <typename T>
  [[nodiscard]] bool sendFrame(T command, uint16_t seq = 0,
                               etl::span<const uint8_t> p = {}) {
    static_assert(etl::is_enum_v<T> || etl::is_integral_v<T>,
                  "Command must be enum or integral");
    const uint16_t cmd = static_cast<uint16_t>(command);
    const bool is_system = rpc::is_system_command(cmd);
    if (!_state_flags.test(FLAG_TX_ENABLED) && !is_system) return false;
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
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id = command_id;
    env.sequence_id = sequence_id;
    rpc::Payload::set<T>(env, packet);
    return _sendFrameRaw(env, command_id);
  }

  template <typename T>
  [[nodiscard]] bool send(rpc::StatusCode s, uint16_t seq, const T& packet) {
    return sendSinglePass<T>(rpc::to_underlying(s), seq, packet);
  }

  template <typename T>
  [[nodiscard]] bool send(rpc::CommandId c, uint16_t seq, const T& packet) {
    const uint16_t raw_cmd = rpc::to_underlying(c);
    const bool is_excluded = rpc::is_system_command(raw_cmd);
    const bool do_encrypt =
        isSynchronized() && !_shared_secret.empty() && !is_excluded;

    if (do_encrypt) {
      return _sendEncryptedHelper<T>(raw_cmd, seq, packet);
    } else {
      return sendSinglePass<T>(raw_cmd, seq, packet);
    }
  }

  using CommandHandler = etl::delegate<void(const rpc_pb_RpcEnvelope&)>;
  using StatusHandler =
      etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;
  void onCommand(CommandHandler h) { _command_handler = h; }
  void onStatus(StatusHandler h) { _status_handler = h; }

  __attribute__((noinline)) void _dispatchCommand(
      const rpc_pb_RpcEnvelope& envelope);
  static void _onBootloaderDelay();
  void _onAckTimeout();
  void _onRxDedupe();
  void _onBaudrateChange();
  void _retransmitLastFrame();
  bool _isSecurityCheckPassed(uint16_t command_id) const;

  // [ETL] Per-command dispatch handlers — declared static so their addresses
  // can be stored in a constexpr-compatible function pointer (not a member fn
  // pointer). Each accesses BridgeClass state via the explicit `self`
  // reference.
  static void _onCmd_StatusAck(BridgeClass& self,
                               const bridge::router::CommandContext& ctx);
  static void _onCmd_GetVersion(BridgeClass& self,
                                const bridge::router::CommandContext& ctx);
  static void _onCmd_GetFreeMemory(BridgeClass& self,
                                   const bridge::router::CommandContext& ctx);
  static void _onCmd_LinkSync(BridgeClass& self,
                              const bridge::router::CommandContext& ctx);
  static void _onCmd_LinkReset(BridgeClass& self,
                               const bridge::router::CommandContext& ctx);
  static void _onCmd_GetCapabilities(BridgeClass& self,
                                     const bridge::router::CommandContext& ctx);
  static void _onCmd_SetBaudrate(BridgeClass& self,
                                 const bridge::router::CommandContext& ctx);
  static void _onCmd_EnterBootloader(BridgeClass& self,
                                     const bridge::router::CommandContext& ctx);
  static void _onCmd_Xoff(BridgeClass& self,
                          const bridge::router::CommandContext& ctx);
  static void _onCmd_Xon(BridgeClass& self,
                         const bridge::router::CommandContext& ctx);
  static void _onCmd_SetPinMode(BridgeClass& self,
                                const bridge::router::CommandContext& ctx);
  static void _onCmd_DigitalWrite(BridgeClass& self,
                                  const bridge::router::CommandContext& ctx);
  static void _onCmd_AnalogWrite(BridgeClass& self,
                                 const bridge::router::CommandContext& ctx);
  // Two table entries point to this handler; internal branch on
  // ctx.raw_command.
  static void _onCmd_PinRead(BridgeClass& self,
                             const bridge::router::CommandContext& ctx);
  static void _onCmd_ConsoleWrite(BridgeClass& self,
                                  const bridge::router::CommandContext& ctx);
#if BRIDGE_ENABLE_DATASTORE
  static void _onCmd_DatastoreGetResp(
      BridgeClass& self, const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_MAILBOX
  static void _onCmd_MailboxPush(BridgeClass& self,
                                 const bridge::router::CommandContext& ctx);
  static void _onCmd_MailboxReadResp(BridgeClass& self,
                                     const bridge::router::CommandContext& ctx);
  static void _onCmd_MailboxAvailableResp(
      BridgeClass& self, const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  static void _onCmd_FileWrite(BridgeClass& self,
                               const bridge::router::CommandContext& ctx);
  static void _onCmd_FileRead(BridgeClass& self,
                              const bridge::router::CommandContext& ctx);
  static void _onCmd_FileRemove(BridgeClass& self,
                                const bridge::router::CommandContext& ctx);
  static void _onCmd_FileReadResp(BridgeClass& self,
                                  const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_PROCESS
  static void _onCmd_ProcessKill(BridgeClass& self,
                                 const bridge::router::CommandContext& ctx);
  static void _onCmd_ProcessRunAsyncResp(
      BridgeClass& self, const bridge::router::CommandContext& ctx);
  static void _onCmd_ProcessPollResp(BridgeClass& self,
                                     const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_SPI
  static void _onCmd_SpiBegin(BridgeClass& self,
                              const bridge::router::CommandContext& ctx);
  static void _onCmd_SpiTransfer(BridgeClass& self,
                                 const bridge::router::CommandContext& ctx);
  static void _onCmd_SpiEnd(BridgeClass& self,
                            const bridge::router::CommandContext& ctx);
  static void _onCmd_SpiSetConfig(BridgeClass& self,
                                  const bridge::router::CommandContext& ctx);
#endif

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
  void _initObservers();

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
  PacketSerial2::PacketSerial<PacketSerial2::COBSR, PacketSerial2::NoCRC,
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
  void
  _onHandshakeTimeout();  // [SIL-2/H-2] Handshake response watchdog callback

  enum StateFlags : uint8_t {
    FLAG_POST_PASSED = 0,
    FLAG_TX_ENABLED = 1,
    FLAG_SERIAL_XOFF = 2,
    FLAG_COUNT = 3
  };

  uint32_t _timer_last_tick_ms = 0;
  etl::bitset<FLAG_COUNT> _state_flags;

  etl::callback_timer<bridge::scheduler::NUMBER_OF_TIMERS> _timers;
  // Shared working buffer for transient operations (unencrypted encoding, SPI
  // transfer)
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> _working_buffer;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> _crypto_buffer;
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> _tx_frame_buffer;
  rpc_pb_RpcEnvelope _tx_envelope = rpc_pb_RpcEnvelope_init_zero;

  uint32_t _wcet_max_micros = 0;

  etl::pool<TxPayloadBuffer, bridge::config::MAX_PENDING_TX_FRAMES>
      _tx_payload_pool;
  etl::deque<PendingTxFrame, bridge::config::MAX_PENDING_TX_FRAMES>
      _pending_tx_queue;

  etl::circular_buffer<uint16_t, bridge::config::RX_HISTORY_SIZE> _rx_history;

  bool _preDispatch(const bridge::router::CommandContext& ctx, bool needs_ack,
                    bool retransmit_on_dup);

  // [SIL-2] Tag type: marks payload-free dispatch cases (no Protobuf decode).
  struct _NoPayload {};

  // [ETL] Dispatch table entry. Uses a regular (non-member) function pointer so
  // the table can be defined as `static const` in Bridge.cpp without adding any
  // data to sizeof(BridgeClass) — critical for AVR RAM budgets.
  struct DispatchEntry {
    uint16_t command_id;
    void (*fn)(BridgeClass&, const bridge::router::CommandContext&);
  };
  // Sorted table defined in Bridge.cpp; size exported as k_dispatch_table_size.
#if defined(__AVR__) || defined(ARDUINO_ARCH_AVR)
  static const DispatchEntry k_dispatch_table[] PROGMEM;
#else
  static const DispatchEntry k_dispatch_table[];
#endif
  static const size_t k_dispatch_table_size;

  // [SIL-2] Unified template dispatcher — consolidates decode + ack + dup-check
  // boilerplate from _dispatchCommand into a single auditable point.
  // Template wrapper per Rule 3 (AGENTS.md): only template wrappers allowed.
  //
  // Handler signature for _NoPayload: (const bridge::router::CommandContext&)
  // Handler signature for typed PB:   (const bridge::router::CommandContext&,
  //                                    const MsgType&)
  template <typename MsgType, typename Handler>
  bool _dispatchCmd(const bridge::router::CommandContext& ctx, Handler handler,
                    bool needs_ack = true, bool retransmit_on_dup = false) {
    if (!_preDispatch(ctx, needs_ack, retransmit_on_dup)) {
      return false;
    }
    if constexpr (!etl::is_same_v<MsgType, _NoPayload>) {
      const pb_size_t expected_tag = rpc::Payload::get_tag<MsgType>();
      if (ctx.envelope->which_payload_type == expected_tag) {
        // [Zero-Copy] Direct reference to active union member in Nanopb
        // envelope
        const auto& m = rpc::Payload::get<MsgType>(*ctx.envelope);
        handler(ctx, m);
      } else if (ctx.envelope->which_payload_type ==
                 rpc_pb_RpcEnvelope_encrypted_payload_with_tag_tag) {
        MsgType m = {};
        if (!_decodePayload(ctx, rpc::Payload::get_fields<MsgType>(), &m,
                            expected_tag, sizeof(MsgType))) {
          emitStatus(rpc::StatusCode::STATUS_MALFORMED);
          return false;
        }
        handler(ctx, m);
      } else {
        emitStatus(rpc::StatusCode::STATUS_MALFORMED);
        return false;
      }
    } else {
      handler(ctx);
    }
    return true;
  }

  void _applyTimingConfig(const rpc::payload::HandshakeConfig& msg);

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
  void _handleEnterBootloader(const rpc::payload::EnterBootloader& msg);
  void _handleSpiBegin(const bridge::router::CommandContext& ctx);
  void _handleSpiEnd(const bridge::router::CommandContext& ctx);
  __attribute__((noinline)) void _handleSpiTransfer(
      const bridge::router::CommandContext& ctx, const rpc_pb_SpiTransfer& m);
  __attribute__((noinline)) void _handleReceivedFrame(
      etl::span<const uint8_t> p);
  void onUnknownCommand(const bridge::router::CommandContext& ctx);

  void _processAck(uint16_t command_id, uint16_t sequence_id);

  static __attribute__((noinline)) bool _decodePayload(
      const bridge::router::CommandContext& ctx, const pb_msgdesc_t* fields,
      void* dest, pb_size_t expected_tag, size_t struct_size);

  static void _handleSetPinMode(const rpc_pb_PinMode& m);
  static void _handleDigitalWrite(const rpc_pb_DigitalWrite& m);
  static void _handleAnalogWrite(const rpc_pb_AnalogWrite& m);
  void _handlePinReadCommon(const bridge::router::CommandContext& ctx,
                            uint8_t pin, uint8_t max_pins,
                            rpc::CommandId cmd_id, int (*read_fn)(uint8_t));
  __attribute__((noinline)) void _handleDigitalRead(
      const bridge::router::CommandContext& ctx, const rpc_pb_PinRead& m);
  __attribute__((noinline)) void _handleAnalogRead(
      const bridge::router::CommandContext& ctx, const rpc_pb_PinRead& m);
  static void _handleConsoleWrite(const rpc_pb_ConsoleWrite& m);
  static void _handleDataStoreGetResponse(
      const bridge::router::CommandContext& ctx,
      const rpc_pb_DatastoreGetResponse& m);
  static void _handleFileWrite(const bridge::router::CommandContext& ctx,
                               const rpc_pb_FileWrite& m);
  static void _handleFileRead(const bridge::router::CommandContext& ctx,
                              const rpc_pb_FileRead& m);
  static void _handleFileRemove(const bridge::router::CommandContext& ctx,
                                const rpc_pb_FileRemove& m);
  static void _handleFileReadResponse(const bridge::router::CommandContext& ctx,
                                      const rpc_pb_FileReadResponse& m);
  static void _handleProcessKill(const bridge::router::CommandContext& ctx,
                                 const rpc_pb_ProcessKill& m);
  static void _handleProcessRunAsyncResponse(
      const bridge::router::CommandContext& ctx,
      const rpc_pb_ProcessRunAsyncResponse& m);
  static void _handleProcessPollResponse(
      const bridge::router::CommandContext& ctx,
      const rpc_pb_ProcessPollResponse& m);
  static void _handleSpiSetConfig(const rpc_pb_SpiConfig& m);
#if BRIDGE_ENABLE_MAILBOX
  static void _handleMailboxPush(const bridge::router::CommandContext& ctx,
                                 const rpc_pb_MailboxPush& m);
  static void _handleMailboxReadResponse(const rpc_pb_MailboxReadResponse& m);
  static void _handleMailboxAvailableResponse(
      const rpc_pb_MailboxAvailableResponse& m);
#endif
  void _serialize_and_send(const rpc_pb_RpcEnvelope& env);
  [[nodiscard]] bool _sendFrameRaw(const rpc_pb_RpcEnvelope& env,
                                   uint16_t command_id);
  bool _sendEncryptedImpl(uint16_t raw_cmd, uint16_t seq,
                          const pb_msgdesc_t* fields, const void* src);

  template <typename T>
  bool _sendEncryptedHelper(uint16_t raw_cmd, uint16_t seq, const T& packet) {
    return _sendEncryptedImpl(raw_cmd, seq, rpc::Payload::get_fields<T>(),
                              &packet);
  }

  void _clearPendingTxQueue();
  void _flushPendingTxQueue();
  void _handleAck(uint16_t command_id);
};

#ifndef BRIDGE_NO_GLOBAL_EXTERN
extern BridgeClass Bridge;
#endif

#endif

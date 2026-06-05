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

#include <PacketSerial.h>
#include <Codecs/COBS.h>

#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/callback_timer.h>
#include <etl/delegate.h>
#include <etl/deque.h>
#include <etl/expected.h>
#include <etl/flat_map.h>
#include <etl/fsm.h>
#include <etl/pool.h>
#include <etl/queue.h>
#include <etl/scheduler.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <etl/task.h>
#include <etl/variant.h>
#include <etl/vector.h>

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
  CommandContext(const rpc_pb_RpcEnvelope* f, uint16_t cmd, uint16_t seq, bool dup,
                 bool ack)
      : envelope(f),
        raw_command(cmd),
        sequence_id(seq),
        is_duplicate(dup),
        requires_ack(ack) {}
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

  void emitStatus(rpc::StatusCode s, etl::string_view m);
  void emitStatus(rpc::StatusCode s, etl::span<const uint8_t> p);
  void emitStatus(rpc::StatusCode s, const __FlashStringHelper* m);

  // [SIL-2] Template wrapper to comply with Rule 3
  template <typename = void>
  void emitStatus(rpc::StatusCode s) {
    emitStatus(s, etl::span<const uint8_t>());
  }

  void signalXoff();
  void signalXon();

  [[nodiscard]] bool sendFrame(rpc::StatusCode s, uint16_t seq = 0,
                               etl::span<const uint8_t> p = {});
  [[nodiscard]] bool sendFrame(rpc::CommandId c, uint16_t seq = 0,
                               etl::span<const uint8_t> p = {});
  [[nodiscard]] bool sendFrame(uint16_t cmd, uint16_t seq = 0,
                               etl::span<const uint8_t> p = {});

  template <typename T>
  [[nodiscard]] bool send(rpc::StatusCode s, uint16_t seq, const T& packet) {
    auto res = rpc::Payload::serialize<T>(packet, etl::span<uint8_t>(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE));
    if (res) {
      return sendFrame(s, seq,
                       etl::span<const uint8_t>(_transient_buffer.data(),
                                                res.value()));
    }
    return false;
  }

  template <typename T>
  [[nodiscard]] bool send(rpc::CommandId c, uint16_t seq, const T& packet) {
    auto res = rpc::Payload::serialize<T>(packet, etl::span<uint8_t>(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE));
    if (res) {
      return sendFrame(c, seq,
                       etl::span<const uint8_t>(_transient_buffer.data(),
                                                res.value()));
    }
    return false;
  }

  using CommandHandler = etl::delegate<void(const rpc_pb_RpcEnvelope&)>;
  using StatusHandler =
      etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;
  [[maybe_unused]] void onCommand(CommandHandler h) { _command_handler = h; }
  [[maybe_unused]] void onStatus(StatusHandler h) { _status_handler = h; }
  void flushStream() { _stream.flush(); }

  void _dispatchCommand(const rpc_pb_RpcEnvelope& envelope);
  static void _onBootloaderDelay();
  void _onAckTimeout();
  void _onRxDedupe();
  void _onBaudrateChange();
  void _retransmitLastFrame();
  bool _isSecurityCheckPassed(uint16_t command_id) const;
  void _onPacketReceived(etl::span<const uint8_t> packet);

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

  void _transmit(uint16_t command_id, uint16_t sequence_id,
                 etl::span<const uint8_t> payload);
  void _initializeRuntime();

  // STRICT ORDER FOR CONSTRUCTOR
  Stream& _stream;
  HardwareSerial* _hardware_serial;
  CommandHandler _command_handler;
  StatusHandler _status_handler;
  uint16_t _last_command_id;
  uint16_t _tx_sequence_id;
  uint8_t _retry_count;
  uint8_t _retry_limit;
  uint16_t _ack_timeout_ms;
  uint32_t _response_timeout_ms;
  uint32_t _pending_baudrate;
  uint8_t _consecutive_crc_errors;
  rpc::FrameError _last_parse_error;

  etl::array<uint8_t, bridge::config::RX_BUFFER_SIZE> _ps_rx_storage;
  etl::array<uint8_t, bridge::config::RX_BUFFER_SIZE> _ps_work_buffer;
  PacketSerial2::PacketSerial<PacketSerial2::COBS, PacketSerial2::NoCRC,
                              PacketSerial2::NoLock, PacketSerial2::NoWatchdog>
      _packet_serial;

  etl::vector<uint8_t, 64> _shared_secret;
  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> _session_key;
  uint64_t _tx_nonce_counter;
  uint64_t _rx_nonce_counter;
  bridge::fsm::BridgeFsm _fsm;

  struct WatchdogTask : public etl::task {
    WatchdogTask() : etl::task(0) {}
    uint32_t task_request_work() const override { return 1; }
    void task_process_work() override;
  } _watchdog_task;

  struct SerialTask : public etl::task {
    BridgeClass* bridge;
    bool xoff_sent;
    SerialTask() : etl::task(1), bridge(nullptr), xoff_sent(false) {}
    void bind(BridgeClass& owner) {
      bridge = &owner;
      xoff_sent = false;
    }
    uint32_t task_request_work() const override { return 1; }
    void task_process_work() override;
  } _serial_task;

  struct TimerTask : public etl::task {
    BridgeClass* bridge;
    uint32_t last_tick_ms;
    TimerTask() : etl::task(2), bridge(nullptr), last_tick_ms(0) {}
    void bind(BridgeClass& owner) {
      bridge = &owner;
      last_tick_ms = 0;
    }
    uint32_t task_request_work() const override { return 1; }
    void task_process_work() override;
  } _timer_task;

  etl::vector<etl::task*, 3> _tasks;
  etl::scheduler_policy_sequential_single _scheduler_policy;

  etl::callback_timer<bridge::scheduler::NUMBER_OF_TIMERS> _timers;
  etl::array<etl::timer::id::type, bridge::scheduler::NUMBER_OF_TIMERS>
      _timer_ids;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> _transient_buffer;
  etl::array<uint8_t, bridge::config::RX_BUFFER_SIZE> _rx_storage;

  bool _is_post_passed;
  bool _tx_enabled;


  etl::pool<TxPayloadBuffer, bridge::config::MAX_PENDING_TX_FRAMES>
      _tx_payload_pool;
  etl::deque<PendingTxFrame, bridge::config::MAX_PENDING_TX_FRAMES>
      _pending_tx_queue;

  etl::circular_buffer<uint16_t, bridge::config::RX_HISTORY_SIZE> _rx_history;

  [[maybe_unused]] void _applyTimingConfig(
      const rpc::payload::HandshakeConfig& msg);

  
  
  
  
  
  
  
  


  void _handleStatusOk(const bridge::router::CommandContext& ctx);
  void _handleStatusMalformed(const bridge::router::CommandContext& ctx);
  void _handleStatusAck(const bridge::router::CommandContext& ctx);
  void _handleGetVersion(const bridge::router::CommandContext& ctx);
  void _handleGetFreeMemory(const bridge::router::CommandContext& ctx);
  void _handleLinkSync(const bridge::router::CommandContext& ctx);
  void _handleLinkReset(const bridge::router::CommandContext& ctx);
  void _handleGetCapabilities(const bridge::router::CommandContext& ctx);
  void _handleXoff(const bridge::router::CommandContext& ctx);
  void _handleXon(const bridge::router::CommandContext& ctx);
  void _handleSetBaudrate(const rpc::payload::SetBaudratePacket& msg);
  void _handleSetTiming(const rpc::payload::HandshakeConfig& msg);
  void _handleEnterBootloader(const rpc::payload::EnterBootloader& msg);
  void _handleSpiBegin(const bridge::router::CommandContext& ctx);
  void _handleSpiEnd(const bridge::router::CommandContext& ctx);
  void _handleSpiTransfer(const bridge::router::CommandContext& ctx);
  void _handleReceivedFrame(etl::span<const uint8_t> p);
  void onUnknownCommand(const bridge::router::CommandContext& ctx);

  void _processAck(uint16_t command_id, uint16_t sequence_id);

  using DispatchHandler = void (*)(BridgeClass&,
                                   const bridge::router::CommandContext&);

  
  
  // [SIL-2] Architectural De-layering: Direct Template Dispatch
  template <typename T, void (BridgeClass::*Handler)(const T&)>
  static void _dispatchAck(BridgeClass& b, const bridge::router::CommandContext& ctx) {
    if (ctx.is_duplicate) { b._processAck(ctx.raw_command, ctx.sequence_id); return; }
    auto res = rpc::Payload::parse<T>(*ctx.envelope);
    if (res) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      (b.*Handler)(res.value());
    } else b.emitStatus(rpc::StatusCode::STATUS_MALFORMED);
  }

  template <typename T, void (BridgeClass::*Handler)(const bridge::router::CommandContext&, const T&)>
  static void _dispatchAckCtx(BridgeClass& b, const bridge::router::CommandContext& ctx) {
    if (ctx.is_duplicate) { b._processAck(ctx.raw_command, ctx.sequence_id); return; }
    auto res = rpc::Payload::parse<T>(*ctx.envelope);
    if (res) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      (b.*Handler)(ctx, res.value());
    } else b.emitStatus(rpc::StatusCode::STATUS_MALFORMED);
  }

  template <void (BridgeClass::*Handler)(const bridge::router::CommandContext&)>
  static void _dispatchSimple(BridgeClass& b, const bridge::router::CommandContext& ctx) {
    (b.*Handler)(ctx);
  }

  template <void (BridgeClass::*Handler)(const bridge::router::CommandContext&)>
  static void _dispatchSimpleAck(BridgeClass& b, const bridge::router::CommandContext& ctx) {
    if (ctx.is_duplicate) { b._processAck(ctx.raw_command, ctx.sequence_id); return; }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    (b.*Handler)(ctx);
  }

  template <void (BridgeClass::*Handler)(const bridge::router::CommandContext&)>
  static void _dispatchResponse(BridgeClass& b, const bridge::router::CommandContext& ctx) {
    if (ctx.is_duplicate) { b._retransmitLastFrame(); return; }
    (b.*Handler)(ctx);
  }

  template <typename T, void (BridgeClass::*Handler)(const T&)>
  static void _dispatchPayload(BridgeClass& b, const bridge::router::CommandContext& ctx) {
    auto res = rpc::Payload::parse<T>(*ctx.envelope);
    if (res) (b.*Handler)(res.value());
  }

  void _handleSetPinMode(const rpc_pb_PinMode& m);
  void _handleDigitalWrite(const rpc_pb_DigitalWrite& m);
  void _handleAnalogWrite(const rpc_pb_AnalogWrite& m);
  void _handleDigitalRead(const bridge::router::CommandContext& ctx);
  void _handleAnalogRead(const bridge::router::CommandContext& ctx);
  void _handleConsoleWrite(const rpc_pb_ConsoleWrite& m);
  void _handleDataStoreGetResponse(const bridge::router::CommandContext& ctx, const rpc_pb_DatastoreGetResponse& m);
  void _handleMailboxPush(const rpc_pb_MailboxPush& m);
  void _handleMailboxReadResponse(const bridge::router::CommandContext& ctx, const rpc_pb_MailboxReadResponse& m);
  void _handleMailboxAvailableResponse(const bridge::router::CommandContext& ctx, const rpc_pb_MailboxAvailableResponse& m);
  void _handleFileWrite(const bridge::router::CommandContext& ctx, const rpc_pb_FileWrite& m);
  void _handleFileRead(const bridge::router::CommandContext& ctx, const rpc_pb_FileRead& m);
  void _handleFileRemove(const bridge::router::CommandContext& ctx, const rpc_pb_FileRemove& m);
  void _handleFileReadResponse(const bridge::router::CommandContext& ctx, const rpc_pb_FileReadResponse& m);
  void _handleProcessKill(const bridge::router::CommandContext& ctx, const rpc_pb_ProcessKill& m);
  void _handleProcessRunAsyncResponse(const bridge::router::CommandContext& ctx, const rpc_pb_ProcessRunAsyncResponse& m);
  void _handleProcessPollResponse(const bridge::router::CommandContext& ctx, const rpc_pb_ProcessPollResponse& m);
  void _handleSpiSetConfig(const rpc_pb_SpiConfig& m);
  void _handleAckStruct(const rpc_pb_AckPacket& m);
  void _handleLinkResetStruct(const rpc_pb_HandshakeConfig& m);
  static DispatchHandler _getHandler(uint16_t command_id);

  void _clearPendingTxQueue();
  void _flushPendingTxQueue();
  void _handleAck(uint16_t command_id);

};

#ifndef BRIDGE_NO_GLOBAL_EXTERN
extern BridgeClass Bridge;
#endif

#endif

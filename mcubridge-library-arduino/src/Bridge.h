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
#include "fsm/bridge_fsm.h"
#include "protocol/BridgeEvents.h"
#include "protocol/rle.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "security/security.h"

namespace rpc {
class Serializable {
 public:
  virtual bool encode(msgpack::Encoder& enc) const = 0;
};
}  // namespace rpc

namespace bridge {
namespace router {
struct CommandContext {
  const rpc::Frame* frame;
  uint16_t raw_command;
  uint16_t sequence_id;
  bool is_duplicate;
  bool requires_ack;
  CommandContext(const rpc::Frame* f, uint16_t cmd, uint16_t seq, bool dup,
                 bool ack)
      : frame(f),
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

  void registerObserver(BridgeObserver& observer);

  void notify_observers(const MsgBridgeSynchronized& msg);
  void notify_observers(const MsgBridgeLost& msg);

  void begin(uint32_t baudrate = 0, const char* secret = nullptr);
  void process();
  bool isSynchronized() const;
  void enterSafeState();

  template <rpc::StatusCode S>
  void emitStatus() {
    emitStatus(S, etl::span<const uint8_t>());
  }

  void emitStatus(rpc::StatusCode s, etl::string_view m = {});
  void emitStatus(rpc::StatusCode s, etl::span<const uint8_t> p);
  void emitStatus(rpc::StatusCode s, const __FlashStringHelper* m);

  void signalXoff();
  void signalXon();

  [[nodiscard]] bool sendFrame(rpc::StatusCode s, uint16_t seq = 0,
                               etl::span<const uint8_t> p = {});
  [[nodiscard]] bool sendFrame(rpc::CommandId c, uint16_t seq = 0,
                               etl::span<const uint8_t> p = {});

  template <typename T>
  [[nodiscard]] bool send(rpc::CommandId c, uint16_t seq, const T& packet) {
    msgpack::Encoder enc(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE);
    if (packet.encode(enc)) return sendFrame(c, seq, enc.result());
    return false;
  }

  using CommandHandler = etl::delegate<void(const rpc::Frame&)>;
  using StatusHandler =
      etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;
  [[maybe_unused]] void onCommand(CommandHandler h) { _command_handler = h; }
  [[maybe_unused]] void onStatus(StatusHandler h) { _status_handler = h; }
  void flushStream() { _stream.flush(); }

  [[maybe_unused]] void _computeHandshakeTag(
      const etl::span<const uint8_t> nonce, etl::span<uint8_t> tag);

  void _dispatchCommand(const rpc::Frame& frame);
  void _onStartupStabilized();
  void _onBootloaderDelay();
  void _onAckTimeout();
  void _onRxDedupe();
  void _onBaudrateChange();
  void _retransmitLastFrame();
  bool _isSecurityCheckPassed(uint16_t command_id) const;
  void _onPacketReceived(etl::span<const uint8_t> packet);

  static constexpr bool is_reliable_cmd(uint16_t id) {
    return rpc::is_reliable(id);
  }
  static constexpr bool is_compressed_cmd(uint16_t id) {
    return (id & rpc::RPC_CMD_FLAG_COMPRESSED) != 0;
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

  etl::array<uint8_t, 256> _ps_rx_storage;
  etl::array<uint8_t, 256> _ps_work_buffer;
  PacketSerial2::PacketSerial<PacketSerial2::COBS, PacketSerial2::NoCRC,
                              PacketSerial2::NoLock, PacketSerial2::NoWatchdog>
      _packet_serial;

  etl::vector<uint8_t, 32> _shared_secret;
  bridge::fsm::BridgeFsm _fsm;

  struct WatchdogTask : public etl::task {
    WatchdogTask() : etl::task(0) {}
    uint32_t task_request_work() const override { return 1; }
    void task_process_work() override;
  } _watchdog_task;

  struct SerialTask : public etl::task {
    BridgeClass& bridge;
    bool xoff_sent;
    explicit SerialTask(BridgeClass& b)
        : etl::task(1), bridge(b), xoff_sent(false) {}
    uint32_t task_request_work() const override { return 1; }
    void task_process_work() override;
  } _serial_task;

  struct TimerTask : public etl::task {
    BridgeClass& bridge;
    uint32_t last_tick_ms;
    explicit TimerTask(BridgeClass& b)
        : etl::task(2), bridge(b), last_tick_ms(0) {}
    uint32_t task_request_work() const override { return 1; }
    void task_process_work() override;
  } _timer_task;

  etl::vector<etl::task*, 3> _tasks;
  etl::scheduler_policy_sequential_single _scheduler_policy;

  etl::callback_timer<bridge::scheduler::NUMBER_OF_TIMERS> _timers;
  etl::array<etl::timer::id::type, bridge::scheduler::NUMBER_OF_TIMERS>
      _timer_ids;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> _transient_buffer;
  etl::array<uint8_t, 256> _rx_storage;
  rpc::FrameParser _frame_parser;
  bool _is_post_passed;
  bool _tx_enabled;

  etl::vector<BridgeObserver*, bridge::config::MAX_OBSERVERS> _observers;
  etl::pool<TxPayloadBuffer, bridge::config::TX_QUEUE_CAPACITY>
      _tx_payload_pool;
  etl::queue<PendingTxFrame, bridge::config::TX_QUEUE_CAPACITY>
      _pending_tx_queue;

  etl::circular_buffer<uint16_t, bridge::config::RX_HISTORY_SIZE> _rx_history;

  [[nodiscard]] bool _sendFrame(uint16_t command_id, uint16_t sequence_id,
                                etl::span<const uint8_t> payload);
  void _sendRawFrame(uint16_t command_id, uint16_t sequence_id,
                     etl::span<const uint8_t> payload);
  [[nodiscard]] etl::expected<void, rpc::FrameError> _decompressFrame(
      const rpc::Frame& in, rpc::Frame& out);
  [[maybe_unused]] void _applyTimingConfig(
      const rpc::payload::HandshakeConfig& msg);

  void _handleSetBaudrateCommand(const bridge::router::CommandContext& ctx);
  void _handleEnterBootloaderCommand(const bridge::router::CommandContext& ctx);
  void _handleSetPinModeCommand(const bridge::router::CommandContext& ctx);
  void _handleDigitalWriteCommand(const bridge::router::CommandContext& ctx);
  void _handleAnalogWriteCommand(const bridge::router::CommandContext& ctx);
  void _handleDigitalReadCommand(const bridge::router::CommandContext& ctx);
  void _handleAnalogReadCommand(const bridge::router::CommandContext& ctx);
  void _handleConsoleWriteCommand(const bridge::router::CommandContext& ctx);
#if BRIDGE_ENABLE_DATASTORE
  void _handleDataStoreGetResponseCommand(
      const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_MAILBOX
  void _handleMailboxPushCommand(const bridge::router::CommandContext& ctx);
  void _handleMailboxReadResponseCommand(
      const bridge::router::CommandContext& ctx);
  void _handleMailboxAvailableResponseCommand(
      const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  void _handleFileWriteCommand(const bridge::router::CommandContext& ctx);
  void _handleFileReadCommand(const bridge::router::CommandContext& ctx);
  void _handleFileRemoveCommand(const bridge::router::CommandContext& ctx);
  void _handleFileReadResponseCommand(
      const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_PROCESS
  void _handleProcessKillCommand(const bridge::router::CommandContext& ctx);
  void _handleProcessRunAsyncResponseCommand(
      const bridge::router::CommandContext& ctx);
  void _handleProcessPollResponseCommand(
      const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_SPI
  void _handleSpiSetConfigCommand(const bridge::router::CommandContext& ctx);
#endif

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

  using DispatchHandler =
      void (BridgeClass::*)(const bridge::router::CommandContext&);

  // [SIL-2] O(1) Jump Table for mission-critical determinism
  static constexpr size_t DISPATCH_TABLE_SIZE = 256;
  etl::array<DispatchHandler, DISPATCH_TABLE_SIZE> _dispatch_table;

  template <typename T, typename F>
  void _withPayload(const bridge::router::CommandContext& ctx, F handler) {
    auto res = rpc::Payload::parse<T>(*ctx.frame);
    if (res) handler(res.value());
  }
  template <typename T, typename F>
  void _withPayloadAck(const bridge::router::CommandContext& ctx, F handler) {
    if (ctx.is_duplicate) {
      (void)sendFrame(rpc::StatusCode::STATUS_ACK, ctx.sequence_id);
      return;
    }
    auto res = rpc::Payload::parse<T>(*ctx.frame);
    if (res) {
      handler(res.value());
      if (ctx.requires_ack)
        (void)sendFrame(rpc::StatusCode::STATUS_ACK, ctx.sequence_id);
    } else
      emitStatus<rpc::StatusCode::STATUS_ERROR>();
  }
  template <typename F>
  void _withResponse(const bridge::router::CommandContext& ctx, F handler) {
    if (ctx.is_duplicate) {
      _retransmitLastFrame();
      return;
    }
    handler();
  }
  template <typename T, typename TID, typename TValid, typename TRead>
  void _handlePinRead(const bridge::router::CommandContext& ctx, TID resp_id,
                      TValid valid, TRead read) {
    _withResponse(ctx, [this, &ctx, resp_id, valid, read]() {
      auto res = rpc::Payload::parse<rpc::payload::PinRead>(*ctx.frame);
      if (res && valid(res->pin)) {
        T resp = {static_cast<decltype(T::value)>(read(res->pin))};
        (void)send(static_cast<rpc::CommandId>(resp_id), ctx.sequence_id, resp);
      } else
        emitStatus<rpc::StatusCode::STATUS_ERROR>();
    });
  }
  void _clearPendingTxQueue();
  void _flushPendingTxQueue();
  void _handleAck(uint16_t command_id);
  void _handleMalformed(uint16_t command_id);
};

extern BridgeClass Bridge;

#endif

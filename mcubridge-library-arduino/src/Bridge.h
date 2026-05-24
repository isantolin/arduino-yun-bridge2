/**
 * @file Bridge.h
 * @brief Arduino MCU Bridge v2 - MCU-side RPC library.
 */

#ifndef BRIDGE_H
#define BRIDGE_H

#include <stdint.h>
#include <Arduino.h>

#include <etl/callback_timer.h>
#include <etl/circular_buffer.h>
#include <etl/delegate.h>
#include <etl/deque.h>
#include <etl/pool.h>
#include <etl/scheduler.h>
#include <etl/vector.h>

#include <PacketSerial.h>
#include <Codecs/COBS.h>

#include "etl_profile.h"
#include "fsm/bridge_fsm.h"
#include "hal/hal.h"
#include "protocol/BridgeEvents.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"

namespace bridge::test {
class TestAccessor;
}

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

  void begin(uint32_t baudrate = 0, const char* secret = nullptr);
  void process();
  bool isSynchronized() const;
  void enterSafeState();

  void emitStatus(rpc::StatusCode s, etl::string_view m);
  void emitStatus(rpc::StatusCode s, etl::span<const uint8_t> p);
  void emitStatus(rpc::StatusCode s, const __FlashStringHelper* m);

  // Non-template wrapper to reduce bloat
  void emitStatus(rpc::StatusCode s) {
    emitStatus(s, etl::span<const uint8_t>());
  }

  void signalXoff();
  void signalXon();

  [[nodiscard]] bool sendFrame(rpc::StatusCode s, uint16_t seq = 0,
                               etl::span<const uint8_t> p = {});
  [[nodiscard]] bool sendFrame(rpc::CommandId c, uint16_t seq = 0,
                               etl::span<const uint8_t> p = {});

  template <typename T>
  [[nodiscard]] bool send(rpc::StatusCode s, uint16_t seq,
                          const pb_msgdesc_t* fields, const T& packet) {
    pb_ostream_t stream = pb_ostream_from_buffer(_transient_buffer.data(),
                                                 rpc::MAX_PAYLOAD_SIZE);
    if (pb_encode(&stream, fields, &packet)) {
      return sendFrame(
          s, seq,
          etl::span<const uint8_t>(_transient_buffer.data(), stream.bytes_written));
    }
    return false;
  }

  template <typename T>
  [[nodiscard]] bool send(rpc::CommandId c, uint16_t seq,
                          const pb_msgdesc_t* fields, const T& packet) {
    pb_ostream_t stream = pb_ostream_from_buffer(_transient_buffer.data(),
                                                 rpc::MAX_PAYLOAD_SIZE);
    if (pb_encode(&stream, fields, &packet)) {
      return sendFrame(
          c, seq,
          etl::span<const uint8_t>(_transient_buffer.data(), stream.bytes_written));
    }
    return false;
  }

  using CommandHandler = etl::delegate<void(const rpc::Frame&)>;
  using StatusHandler =
      etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;
  [[maybe_unused]] void onCommand(CommandHandler h) { _command_handler = h; }
  [[maybe_unused]] void onStatus(StatusHandler h) { _status_handler = h; }
  void flushStream() { _stream.flush(); }

  void _dispatchCommand(const rpc::Frame& frame);
  static void _onBootloaderDelay();
  void _onAckTimeout();
  void _onRxDedupe();
  void _onBaudrateChange();
  void _retransmitLastFrame();
  bool _isSecurityCheckPassed(uint16_t command_id) const;
  void _onPacketReceived(etl::span<const uint8_t> packet);
  void _handleAck(uint16_t cmd);

  static constexpr bool is_reliable_cmd(uint16_t id) {
    return rpc::requires_ack(id);
  }
  static constexpr bool is_compressed_cmd(uint16_t id) {
    return (id & rpc::CMD_FLAG_COMPRESSED) != 0;
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

  void _sendRawFrame(uint16_t command_id, uint16_t sequence_id,
                     etl::span<const uint8_t> payload);
  bool _sendFrame(uint16_t command_id, uint16_t sequence_id,
                  etl::span<const uint8_t> payload);
  void _initializeRuntime();
  void _clearPendingTxQueue();
  void _flushPendingTxQueue();

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

  friend class bridge::test::TestAccessor;
  etl::vector<uint8_t, rpc::AEAD_KEY_SIZE> _shared_secret;
  etl::array<uint8_t, rpc::AEAD_KEY_SIZE> _session_key;
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

  etl::vector<BridgeObserver*, bridge::config::MAX_OBSERVERS> _observers;
  etl::pool<TxPayloadBuffer, bridge::config::MAX_PENDING_TX_FRAMES>
      _tx_payload_pool;
  etl::deque<PendingTxFrame, bridge::config::MAX_PENDING_TX_FRAMES>
      _pending_tx_queue;

  etl::circular_buffer<uint16_t, bridge::config::RX_HISTORY_SIZE> _rx_history;

  [[nodiscard]] etl::expected<void, rpc::FrameError> _decompressFrame(
      const rpc::Frame& in, rpc::Frame& out);
  [[maybe_unused]] void _applyTimingConfig(
      const rpc_pb_HandshakeConfig& msg);

  void _handleSetBaudrateCommand(const bridge::router::CommandContext& ctx);
  void _handleEnterBootloaderCommand(const bridge::router::CommandContext& ctx);
  void _handleSetPinModeCommand(const bridge::router::CommandContext& ctx);
  void _handleDigitalWriteCommand(const bridge::router::CommandContext& ctx);
  void _handleAnalogWriteCommand(const bridge::router::CommandContext& ctx);
  void _handleDigitalReadCommand(const bridge::router::CommandContext& ctx);
  void _handleAnalogReadCommand(const bridge::router::CommandContext& ctx);
  void _handleGetVersion(const bridge::router::CommandContext& ctx);
  void _handleGetFreeMemory(const bridge::router::CommandContext& ctx);
  void _handleGetCapabilities(const bridge::router::CommandContext& ctx);
  void _handleXoff(const bridge::router::CommandContext& ctx);
  void _handleXon(const bridge::router::CommandContext& ctx);
  void _handleSetBaudrate(const rpc_pb_SetBaudratePacket& msg);
  void _handleSetTiming(const rpc_pb_HandshakeConfig& msg);
  void _handleEnterBootloader(const rpc_pb_EnterBootloader& msg);
  void _handleSpiBegin(const bridge::router::CommandContext& ctx);
  void _handleSpiEnd(const bridge::router::CommandContext& ctx);
  void _handleSpiTransfer(const bridge::router::CommandContext& ctx);
  void _handleSpiConfig(const bridge::router::CommandContext& ctx);
  void _handleStatusOk(const bridge::router::CommandContext& ctx);
  void _handleStatusAck(const bridge::router::CommandContext& ctx);
  void _handleStatusMalformed(const bridge::router::CommandContext& ctx);
  void _handleLinkSync(const bridge::router::CommandContext& ctx);
  void _handleLinkReset(const bridge::router::CommandContext& ctx);
  void _handleConsoleWriteCommand(const bridge::router::CommandContext& ctx);
  void _handleDataStoreGetResponseCommand(const bridge::router::CommandContext& ctx);
  void _handleMailboxPushCommand(const bridge::router::CommandContext& ctx);
  void _handleMailboxReadResponseCommand(const bridge::router::CommandContext& ctx);
  void _handleMailboxAvailableResponseCommand(const bridge::router::CommandContext& ctx);
  void _handleFileWriteCommand(const bridge::router::CommandContext& ctx);
  void _handleFileReadCommand(const bridge::router::CommandContext& ctx);
  void _handleFileRemoveCommand(const bridge::router::CommandContext& ctx);
  void _handleFileReadResponseCommand(const bridge::router::CommandContext& ctx);
  void _handleProcessKillCommand(const bridge::router::CommandContext& ctx);
  void _handleProcessRunAsyncResponseCommand(const bridge::router::CommandContext& ctx);
  void _handleProcessPollResponseCommand(const bridge::router::CommandContext& ctx);
  void _handleSpiSetConfigCommand(const bridge::router::CommandContext& ctx);
  
  void _handleReceivedFrame(etl::span<const uint8_t> p);
  void onUnknownCommand(const bridge::router::CommandContext& ctx);

  // [MEM-SAVE] Non-template helper to reduce binary bloat in _withPayloadAck.
  // Declared before templates to ensure visibility in template body.
  void _processAck(uint16_t command_id, uint16_t sequence_id);

  // [MEM-SAVE] Static wrapper type to avoid member function pointer overhead
  // and enable true constexpr/Flash placement of the dispatch table.
  using DispatchHandler = void (*)(BridgeClass&,
                                   const bridge::router::CommandContext&);

  // [SIL-2] [MEM-SAVE] Static O(1) jump table in Flash.
  static DispatchHandler _getHandler(uint16_t command_id);

  template <typename T, typename F>
  void _withPayload(const bridge::router::CommandContext& ctx,
                    const pb_msgdesc_t* fields, F handler) {
    auto res = rpc::Payload::parse<T>(*ctx.frame, fields);
    if (res) handler(res.value());
  }

  template <typename T, typename F>
  void _withPayloadAck(const bridge::router::CommandContext& ctx,
                       const pb_msgdesc_t* fields, F handler) {
    // [MEM-SAVE] Delegating ACK processing to non-template _processAck
    // reduces the code generated for each instantiation of this template.
    if (ctx.is_duplicate) {
      _processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    auto res = rpc::Payload::parse<T>(*ctx.frame, fields);
    if (res) {
      handler(res.value());
      if (ctx.requires_ack) _processAck(ctx.raw_command, ctx.sequence_id);
    } else
      emitStatus(rpc::StatusCode::STATUS_ERROR);
  }

  template <typename F>
  void _withResponse(const bridge::router::CommandContext& ctx, F handler) {
    if (ctx.is_duplicate) {
      _retransmitLastFrame();
      return;
    }
    handler();
  }

  template <typename T, typename F>
  void _handlePinRead(const bridge::router::CommandContext& ctx,
                      rpc::CommandId resp_cmd, F read_func,
                      const pb_msgdesc_t* fields) {
    _withResponse(ctx, [this, &ctx, resp_cmd, read_func, fields]() {
      auto res = rpc::Payload::parse<rpc_pb_PinRead>(*ctx.frame,
                                                      rpc_pb_PinRead_fields);
      if (res) {
        T resp = {};
        resp.value = read_func(res->pin);
        (void)send(resp_cmd, ctx.sequence_id, fields, resp);
      } else
        (void)sendFrame(rpc::StatusCode::STATUS_ERROR, ctx.sequence_id);
    });
  }

  void _notifyObservers(const MsgBridgeSynchronized& msg) {
    etl::for_each(_observers.begin(), _observers.end(),
                  [&msg](BridgeObserver* observer) {
                    if (observer != nullptr) observer->notification(msg);
                  });
  }

  void _notifyObservers(const MsgBridgeLost& msg) {
    etl::for_each(_observers.begin(), _observers.end(),
                  [&msg](BridgeObserver* observer) {
                    if (observer != nullptr) observer->notification(msg);
                  });
  }
};

extern BridgeClass Bridge;

#endif

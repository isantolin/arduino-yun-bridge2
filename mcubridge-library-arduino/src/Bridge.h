/**
 * @file Bridge.h
 * @brief Arduino MCU Bridge v2 - MCU-side RPC library.
 */

#ifndef BRIDGE_H
#define BRIDGE_H

#include <stdint.h>

#include "etl_profile.h"
#include "hal/hal.h"

namespace bridge::test {
class TestAccessor;
}

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
#include "protocol/BridgeEvents.h"
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
  const rpc::Frame* frame;
  uint16_t sequence_id;
  bool is_duplicate;
  bool requires_ack;
  CommandContext(const rpc::Frame* f, uint16_t seq, bool dup, bool ack)
      : frame(f), sequence_id(seq), is_duplicate(dup), requires_ack(ack) {}
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
  [[nodiscard]] bool send(rpc::StatusCode s, uint16_t seq, const T& packet) {
    rpc_pb_RpcPayload payload = rpc_pb_RpcPayload_init_default;
    payload.which_msg = static_cast<pb_size_t>(s);
    // Special handling for Empty or other types if needed
    // For statuses, they are typically Empty.
    
    pb_ostream_t stream =
        pb_ostream_from_buffer(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE);
    if (pb_encode(&stream, rpc_pb_RpcPayload_fields, &payload)) {
      return _sendFrame(static_cast<uint16_t>(s), seq,
                       etl::span<const uint8_t>(_transient_buffer.data(),
                                                stream.bytes_written));
    }
    return false;
  }

  template <typename T>
  [[nodiscard]] bool send(rpc::CommandId c, uint16_t seq, const T& packet) {
    rpc_pb_RpcPayload payload = rpc_pb_RpcPayload_init_default;
    payload.which_msg = static_cast<pb_size_t>(c);
    
    // We need to map T to the correct field in the union.
    // This is hard with templates without some mapping.
    // For now, let's use a manual mapping or a helper.
    if (static_cast<uint16_t>(c) == rpc_pb_RpcPayload_digital_read_resp_tag) {
        payload.msg.digital_read_resp = *reinterpret_cast<const rpc_pb_DigitalReadResponse*>(&packet);
    } else if (static_cast<uint16_t>(c) == rpc_pb_RpcPayload_analog_read_resp_tag) {
        payload.msg.analog_read_resp = *reinterpret_cast<const rpc_pb_AnalogReadResponse*>(&packet);
    } else if (static_cast<uint16_t>(c) == rpc_pb_RpcPayload_get_version_tag) {
        payload.msg.get_version = rpc_pb_Empty_init_default;
    } else if (static_cast<uint16_t>(c) == rpc_pb_RpcPayload_version_resp_tag) {
        payload.msg.version_resp = *reinterpret_cast<const rpc_pb_VersionResponse*>(&packet);
    }
    // ... we should ideally have a generated helper for this ...

    pb_ostream_t stream =
        pb_ostream_from_buffer(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE);
    if (pb_encode(&stream, rpc_pb_RpcPayload_fields, &payload)) {
      return _sendFrame(static_cast<uint16_t>(c), seq,
                       etl::span<const uint8_t>(_transient_buffer.data(),
                                                stream.bytes_written));
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

  static constexpr bool is_reliable_cmd(uint16_t id) {
    return rpc::requires_ack(id);
  }

#if defined(BRIDGE_HOST_TEST)
 public:
#else
 protected:
#endif

  struct TxPayloadBuffer {
    etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> data;
  };
  struct PendingTxFrame {
    uint16_t command_id;
    uint16_t sequence_id;
    TxPayloadBuffer* buffer;
    size_t length;
  };

  void _sendRawFrame(uint16_t sequence_id,
                     etl::span<const uint8_t> payload, bool do_encrypt);
  bool _sendFrame(uint16_t command_id, uint16_t sequence_id,
                  etl::span<const uint8_t> payload);
  void _initializeRuntime();

  // STRICT ORDER FOR CONSTRUCTOR
  Stream& _stream;
  HardwareSerial* _hardware_serial;
  CommandHandler _command_handler;
  StatusHandler _status_handler;
  uint16_t _last_command_id;
  uint16_t _last_sequence_id;
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

  etl::vector<BridgeObserver*, bridge::config::MAX_OBSERVERS> _observers;
  etl::pool<TxPayloadBuffer, bridge::config::MAX_PENDING_TX_FRAMES>
      _tx_payload_pool;
  etl::deque<PendingTxFrame, bridge::config::MAX_PENDING_TX_FRAMES>
      _pending_tx_queue;

  etl::circular_buffer<uint16_t, bridge::config::RX_HISTORY_SIZE> _rx_history;

  [[maybe_unused]] void _applyTimingConfig(
      const rpc::payload::HandshakeConfig& msg);

  void _dispatch(const rpc_pb_RpcPayload& payload, const bridge::router::CommandContext& ctx);

  void _handleSetBaudrateCommand(const bridge::router::CommandContext& ctx, const rpc_pb_SetBaudratePacket& m);
  void _handleEnterBootloaderCommand(const bridge::router::CommandContext& ctx, const rpc_pb_EnterBootloader& m);
  void _handleSetPinModeCommand(const bridge::router::CommandContext& ctx, const rpc_pb_PinMode& m);
  void _handleDigitalWriteCommand(const bridge::router::CommandContext& ctx, const rpc_pb_DigitalWrite& m);
  void _handleAnalogWriteCommand(const bridge::router::CommandContext& ctx, const rpc_pb_AnalogWrite& m);
  void _handleDigitalReadCommand(const bridge::router::CommandContext& ctx, const rpc_pb_PinRead& m);
  void _handleAnalogReadCommand(const bridge::router::CommandContext& ctx, const rpc_pb_PinRead& m);
  void _handleConsoleWriteCommand(const bridge::router::CommandContext& ctx, const rpc_pb_ConsoleWrite& m);
#if BRIDGE_ENABLE_DATASTORE
  void _handleDataStoreGetResponseCommand(
      const bridge::router::CommandContext& ctx, const rpc_pb_DatastoreGetResponse& m);
#endif
#if BRIDGE_ENABLE_MAILBOX
  void _handleMailboxPushCommand(const bridge::router::CommandContext& ctx, const rpc_pb_MailboxPush& m);
  void _handleMailboxReadResponseCommand(
      const bridge::router::CommandContext& ctx, const rpc_pb_MailboxReadResponse& m);
  void _handleMailboxAvailableResponseCommand(
      const bridge::router::CommandContext& ctx, const rpc_pb_MailboxAvailableResponse& m);
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  void _handleFileWriteCommand(const bridge::router::CommandContext& ctx, const rpc_pb_FileWrite& m);
  void _handleFileReadCommand(const bridge::router::CommandContext& ctx, const rpc_pb_FileRead& m);
  void _handleFileRemoveCommand(const bridge::router::CommandContext& ctx, const rpc_pb_FileRemove& m);
  void _handleFileReadResponseCommand(
      const bridge::router::CommandContext& ctx, const rpc_pb_FileReadResponse& m);
#endif
#if BRIDGE_ENABLE_PROCESS
  void _handleProcessRunAsyncResponseCommand(
      const bridge::router::CommandContext& ctx, const rpc_pb_ProcessRunAsyncResponse& m);
  void _handleProcessPollResponseCommand(
      const bridge::router::CommandContext& ctx, const rpc_pb_ProcessPollResponse& m);
  void _handleProcessKillCommand(const bridge::router::CommandContext& ctx, const rpc_pb_ProcessKill& m);
#endif
#if BRIDGE_ENABLE_SPI
  void _handleSpiSetConfigCommand(const bridge::router::CommandContext& ctx, const rpc_pb_SpiConfig& m);
#endif

  static void _handleStatusOk(const bridge::router::CommandContext& ctx);
  void _handleStatusMalformed(const bridge::router::CommandContext& ctx);
  void _handleAck(uint16_t command_id);
  void _handleStatusAck(const bridge::router::CommandContext& ctx, const rpc_pb_AckPacket& ack);
  void _handleGetVersion(const bridge::router::CommandContext& ctx);
  void _handleGetFreeMemory(const bridge::router::CommandContext& ctx);
  void _handleLinkSync(const bridge::router::CommandContext& ctx, const rpc_pb_LinkSync& m);
  void _handleLinkReset(const bridge::router::CommandContext& ctx, const rpc_pb_HandshakeConfig& m);
  void _handleGetCapabilities(const bridge::router::CommandContext& ctx);
  void _handleXoff(const bridge::router::CommandContext& ctx);
  void _handleXon(const bridge::router::CommandContext& ctx);
  void _handleSetBaudrate(const rpc::payload::SetBaudratePacket& msg);
  void _handleSetTiming(const rpc::payload::HandshakeConfig& msg);
  void _handleEnterBootloader(const rpc::payload::EnterBootloader& msg);
  void _handleSpiBegin(const bridge::router::CommandContext& ctx);
  void _handleSpiEnd(const bridge::router::CommandContext& ctx);
  void _handleSpiTransfer(const bridge::router::CommandContext& ctx, const rpc_pb_SpiTransfer& m);
  void _handleReceivedFrame(etl::span<const uint8_t> p);
  void onUnknownCommand(const bridge::router::CommandContext& ctx);

  // [MEM-SAVE] Non-template helper to reduce binary bloat in _withPayloadAck.
  // Declared before templates to ensure visibility in template body.
  void _processAck(uint16_t command_id, uint16_t sequence_id);

  template <typename TID, typename TRead>
  void _handlePinReadImpl(const bridge::router::CommandContext& ctx, TID resp_id,
                          const rpc_pb_PinRead& m, TRead read) {
    _withResponse(ctx, [this, &ctx, resp_id, &m, read]() {
        using T = rpc_pb_AnalogReadResponse; // Workaround since TID is used as CommandId
        T resp;
        resp.value = static_cast<uint32_t>(read(m.pin));
        (void)send(static_cast<rpc::CommandId>(resp_id), ctx.sequence_id, resp);
    });
  }

  void _clearPendingTxQueue();
  void _flushPendingTxQueue();

  template <typename F>
  void _withResponse(const bridge::router::CommandContext& ctx, F handler) {
    if (ctx.is_duplicate) {
      _retransmitLastFrame();
      return;
    }
    handler();
  }
};

extern BridgeClass Bridge;

#endif

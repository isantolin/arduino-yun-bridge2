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
#include "protocol/mcubridge.pb.h"
#include "protocol/rle.h"
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

#include "ErrorPolicy.h"

namespace rpc {
using namespace payload;
}

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

  [[nodiscard]] bool send(const rpc_pb_McuFrame& frame);
  [[nodiscard]] bool send(uint16_t tag, uint16_t seq, const void* struct_ptr, const pb_msgdesc_t* fields);

  template <typename T>
  [[nodiscard]] bool send(rpc::CommandId c, uint16_t seq, const T& packet) {
      using TTraits = rpc::Payload::Traits<T>;
      return send(static_cast<uint16_t>(c), seq, &packet, TTraits::fields);
  }

  using CommandHandler = etl::delegate<void(const rpc_pb_McuFrame&)>;
  using StatusHandler =
      etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;
  [[maybe_unused]] void onCommand(CommandHandler h) { _command_handler = h; }
  [[maybe_unused]] void onStatus(StatusHandler h) { _status_handler = h; }
  void flushStream() { _stream.flush(); }

  void _dispatchCommand(const rpc_pb_McuFrame& frame);
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

  void _sendRawFrame(uint16_t command_id, uint16_t sequence_id,
                     etl::span<const uint8_t> payload);
  bool _sendFrame(uint16_t command_id, uint16_t sequence_id,
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

  friend class bridge::test::TestAccessor;
  etl::vector<uint8_t, rpc::RPC_AEAD_KEY_SIZE> _shared_secret;
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

  rpc::FrameParser _frame_parser;
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

  void _handleSetBaudrateCommand(const rpc_pb_McuFrame& frame);
  void _handleEnterBootloaderCommand(const rpc_pb_McuFrame& frame);
  void _handleSetPinModeCommand(const rpc_pb_McuFrame& frame);
  void _handleDigitalWriteCommand(const rpc_pb_McuFrame& frame);
  void _handleAnalogWriteCommand(const rpc_pb_McuFrame& frame);
  void _handleDigitalReadCommand(const rpc_pb_McuFrame& frame);
  void _handleAnalogReadCommand(const rpc_pb_McuFrame& frame);
  void _handleConsoleWriteCommand(const rpc_pb_McuFrame& frame);
#if BRIDGE_ENABLE_DATASTORE
  void _handleDataStoreGetResponseCommand(const rpc_pb_McuFrame& frame);
#endif
#if BRIDGE_ENABLE_MAILBOX
  void _handleMailboxPushCommand(const rpc_pb_McuFrame& frame);
  void _handleMailboxReadResponseCommand(const rpc_pb_McuFrame& frame);
  void _handleMailboxAvailableResponseCommand(const rpc_pb_McuFrame& frame);
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  void _handleFileWriteCommand(const rpc_pb_McuFrame& frame);
  void _handleFileReadCommand(const rpc_pb_McuFrame& frame);
  void _handleFileRemoveCommand(const rpc_pb_McuFrame& frame);
  void _handleFileReadResponseCommand(const rpc_pb_McuFrame& frame);
#endif
#if BRIDGE_ENABLE_PROCESS
  void _handleProcessRunAsyncResponseCommand(const rpc_pb_McuFrame& frame);
  void _handleProcessPollResponseCommand(const rpc_pb_McuFrame& frame);
  void _handleProcessKillCommand(const rpc_pb_McuFrame& frame);
#endif
#if BRIDGE_ENABLE_SPI
  void _handleSpiSetConfigCommand(const rpc_pb_McuFrame& frame);
#endif

  static void _handleStatusOk(const rpc_pb_McuFrame& frame);
  void _handleStatusMalformed(const rpc_pb_McuFrame& frame);
  void _handleStatusAck(const rpc_pb_McuFrame& frame);
  void _handleGetVersion(const rpc_pb_McuFrame& frame);
  void _handleGetFreeMemory(const rpc_pb_McuFrame& frame);
  void _handleLinkSync(const rpc_pb_McuFrame& frame);
  void _handleLinkReset(const rpc_pb_McuFrame& frame);
  void _handleGetCapabilities(const rpc_pb_McuFrame& frame);
  void _handleXoff(const rpc_pb_McuFrame& frame);
  void _handleXon(const rpc_pb_McuFrame& frame);
  void _handleSetBaudrate(const rpc_pb_SetBaudratePacket& msg);
  void _handleSetTiming(const rpc_pb_HandshakeConfig& msg);
  void _handleEnterBootloader(const rpc_pb_EnterBootloader& msg);
  void _handleSpiBegin(const rpc_pb_McuFrame& frame);
  void _handleSpiEnd(const rpc_pb_McuFrame& frame);
  void _handleSpiTransfer(const rpc_pb_McuFrame& frame);
  void _handleReceivedFrame(etl::span<const uint8_t> p);
  void onUnknownCommand(const rpc_pb_McuFrame& frame);

  // [MEM-SAVE] Non-template helper to reduce binary bloat in _withPayloadAck.
  // Declared before templates to ensure visibility in template body.
  void _processAck(uint16_t command_id, uint16_t sequence_id);

  template <typename T, typename F>
  void _withPayload(const rpc_pb_McuFrame& frame, F handler) {
    (void)frame; // Payload is already in frame, used by specialized handlers
    // This template might be simplified further
  }

  template <typename T, typename F>
  void _withPayloadAck(const rpc_pb_McuFrame& frame, F handler) {
    handler();
  }

  template <typename F>
  void _withResponse(const rpc_pb_McuFrame& frame, F handler) {
    handler();
  }

  template <typename TID>
  void _handlePinRead(const rpc_pb_McuFrame& frame, TID resp_id,
                      bool digital) {
    (void)frame; (void)resp_id; (void)digital;
  }
  void _clearPendingTxQueue();
  void _flushPendingTxQueue();
  void _handleAck(uint16_t command_id);

  template <typename TMessage>
  void _notifyObservers(const TMessage& msg) {
    etl::for_each(_observers.begin(), _observers.end(),
                  [&msg](BridgeObserver* observer) {
                    if (observer != nullptr) observer->notification(msg);
                  });
  }
};

extern BridgeClass Bridge;

#endif

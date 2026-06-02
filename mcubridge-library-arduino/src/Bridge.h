/**
 * @file Bridge.h
 * @brief Arduino MCU Bridge v2 - MCU-side RPC library.
 */

#ifndef BRIDGE_H
#define BRIDGE_H

#include <stdint.h>
#include <SPI.h>

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
#include <etl/circular_buffer.h>
#include <etl/delegate.h>
#include <etl/deque.h>
#include <etl/expected.h>
#include <etl/flat_map.h>
#include <etl/fsm.h>
#include <etl/pool.h>
#include <etl/queue.h>
#include <etl/scheduler.h>
#include <etl/span.h>
#include <etl/string.h>
#include <etl/string_view.h>
#include <etl/task.h>
#include <etl/variant.h>
#include <etl/vector.h>

#include "config/bridge_config.h"
#include "etl_ext/CounterIterator.h"
#include "fsm/bridge_fsm.h"
#include "protocol/BridgeEvents.h"
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
    pb_ostream_t stream =
        pb_ostream_from_buffer(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE);
    if (rpc::Payload::encode(&stream, packet)) {
      return sendFrame(s, seq,
                       etl::span<const uint8_t>(_transient_buffer.data(),
                                                stream.bytes_written));
    }
    return false;
  }

  template <typename T>
  [[nodiscard]] bool send(rpc::CommandId c, uint16_t seq, const T& packet) {
    pb_ostream_t stream =
        pb_ostream_from_buffer(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE);
    if (rpc::Payload::encode(&stream, packet)) {
      return sendFrame(c, seq,
                       etl::span<const uint8_t>(_transient_buffer.data(),
                                                stream.bytes_written));
    }
    return false;
  }

  using CommandHandler = etl::delegate<void(const rpc_pb_RpcEnvelope&)>;
  using StatusHandler =
      etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;

  // --- Direct Services (Zero-Wrapper) ---
  
  // DataStore
  using DataStoreGetHandler = etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>;
  void datastorePut(etl::string_view key, etl::span<const uint8_t> value);
  void datastoreGet(etl::string_view key, DataStoreGetHandler handler);

  // FileSystem
  using FileReadHandler = etl::delegate<void(etl::span<const uint8_t>)>;
  void fileWrite(etl::string_view path, etl::span<const uint8_t> data);
  void fileRead(etl::string_view path, FileReadHandler handler);
  void fileRemove(etl::string_view path);

  // Mailbox
  void mailboxPush(etl::span<const uint8_t> data);
  void mailboxRequestRead();
  void mailboxRequestAvailable();
  void mailboxSignalProcessed();
  uint16_t mailboxAvailable() const { return _mailbox_available_count; }
  int mailboxRead();
  int mailboxPeek();

  // Process
  using ProcessRunHandler = etl::delegate<void(int32_t)>;
  using ProcessPollHandler = etl::delegate<void(rpc::StatusCode, uint16_t, etl::span<const uint8_t>, etl::span<const uint8_t>)>;
  void processRunAsync(etl::string_view cmd, etl::span<const etl::string_view> args, ProcessRunHandler handler);
  void pollProcess(int32_t pid, ProcessPollHandler handler);
  void processKill(int32_t pid);

  // Console
  size_t consoleWrite(uint8_t c);
  size_t consoleWrite(const uint8_t* buffer, size_t size);
  int consoleAvailable() const { return static_cast<int>(_console_rx_buffer.size()); }
  int consoleRead();
  int consolePeek();

  // SPI
  void spiBegin();
  void spiEnd();
  void spiSetConfig(const rpc::payload::SpiConfig& config);
  size_t spiTransfer(etl::span<uint8_t> buffer);

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
  [[maybe_unused]] static constexpr bool is_compressed_cmd(uint16_t id) {
    return (id & rpc::RPC_CMD_FLAG_COMPRESSED) != 0;
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

  // --- Services State ---
  
  // DataStore
  struct PendingDataStoreGet {
    etl::array<char, rpc::RPC_MAX_DATASTORE_KEY_LENGTH + 1U> key;
    DataStoreGetHandler handler;
  };
  etl::queue<PendingDataStoreGet, bridge::config::MAX_PENDING_DATASTORE> _pending_datastore_gets;

  // FileSystem
  FileReadHandler _fs_read_handler;

  // Mailbox
  etl::circular_buffer<uint8_t, bridge::config::MAILBOX_RX_BUFFER_SIZE> _mailbox_rx_buffer;
  uint16_t _mailbox_available_count;

  // Process
  struct PendingProcessRun {
    ProcessRunHandler handler;
  };
  struct PendingProcessPoll {
    int32_t pid;
    ProcessPollHandler handler;
  };
  etl::queue<PendingProcessRun, bridge::config::MAX_PENDING_PROCESS_POLLS> _pending_process_runs;
  etl::queue<PendingProcessPoll, bridge::config::MAX_PENDING_PROCESS_POLLS> _pending_process_polls;

  // Console
  etl::circular_buffer<uint8_t, bridge::config::CONSOLE_RX_BUFFER_SIZE> _console_rx_buffer;
  etl::vector<uint8_t, bridge::config::CONSOLE_TX_BUFFER_SIZE> _console_tx_buffer;

  // SPI
  bool _spi_initialized;
  SPISettings _spi_settings;

  etl::vector<BridgeObserver*, bridge::config::MAX_OBSERVERS> _observers;
  etl::pool<TxPayloadBuffer, bridge::config::MAX_PENDING_TX_FRAMES>
      _tx_payload_pool;
  etl::deque<PendingTxFrame, bridge::config::MAX_PENDING_TX_FRAMES>
      _pending_tx_queue;

  etl::circular_buffer<uint16_t, bridge::config::RX_HISTORY_SIZE> _rx_history;

  [[nodiscard]] etl::expected<void, rpc::FrameError> _decompressFrame(
      const rpc_pb_RpcEnvelope& in, rpc_pb_RpcEnvelope& out);
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
  
  void _handleDataStoreGetResponseCommand(const bridge::router::CommandContext& ctx);
  
  void _handleMailboxPushCommand(const bridge::router::CommandContext& ctx);
  void _handleMailboxReadResponseCommand(const bridge::router::CommandContext& ctx);
  void _handleMailboxAvailableResponseCommand(const bridge::router::CommandContext& ctx);
  
  void _handleFileWriteCommand(const bridge::router::CommandContext& ctx);
  void _handleFileReadCommand(const bridge::router::CommandContext& ctx);
  void _handleFileRemoveCommand(const bridge::router::CommandContext& ctx);
  void _handleFileReadResponseCommand(const bridge::router::CommandContext& ctx);
  
  void _handleProcessRunAsyncResponseCommand(const bridge::router::CommandContext& ctx);
  void _handleProcessPollResponseCommand(const bridge::router::CommandContext& ctx);
  void _handleProcessKillCommand(const bridge::router::CommandContext& ctx);
  
  void _handleSpiSetConfigCommand(const bridge::router::CommandContext& ctx);

  static void _handleStatusOk(const bridge::router::CommandContext& ctx);
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

  void _handleAck(uint16_t cmd);
  void _clearPendingTxQueue();
  void _flushPendingTxQueue();
  void _processAck(uint16_t command_id, uint16_t sequence_id);

  using DispatchHandler = void (*)(BridgeClass&, const bridge::router::CommandContext&);
  DispatchHandler _getHandler(uint16_t command_id);

  template <typename TMessage>
  void _notifyObservers(const TMessage& msg) {
    etl::for_each(_observers.begin(), _observers.end(),
                  [&msg](BridgeObserver* observer) {
                    if (observer != nullptr) observer->notification(msg);
                  });
  }

  // --- Helpers ---
  template <typename T, typename F>
  void _withPayload(const bridge::router::CommandContext& ctx, F lambda) {
    auto res = rpc::Payload::parse<T>(*ctx.envelope);
    if (res) lambda(res.value());
    else emitStatus(rpc::StatusCode::STATUS_ERROR);
  }

  template <typename F>
  void _withResponse(const bridge::router::CommandContext& /*ctx*/, F lambda) {
    lambda();
  }

  template <typename T, typename F>
  void _withPayloadAck(const bridge::router::CommandContext& ctx, F lambda) {
    auto res = rpc::Payload::parse<T>(*ctx.envelope);
    if (res) { lambda(res.value()); _processAck(ctx.raw_command, ctx.sequence_id); }
    else emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
};

extern BridgeClass Bridge;

#endif

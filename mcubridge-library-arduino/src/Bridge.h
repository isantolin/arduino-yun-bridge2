/**
 * @file Bridge.h
 * @brief Arduino MCU Bridge v2 - MCU-side RPC library.
 */
#ifndef BRIDGE_H
#define BRIDGE_H

#include "etl_profile.h"
#include <stdint.h>

#if defined(ARDUINO_ARCH_AVR)
#include <avr/wdt.h>
#endif

#if !defined(BRIDGE_HOST_TEST)
extern "C" unsigned long millis(void);
#else
extern unsigned long millis(void);
#endif

namespace bridge {
#if defined(BRIDGE_HOST_TEST)
namespace test {
  class TestAccessor;
  class ConsoleTestAccessor;
  class DataStoreTestAccessor;
  class MailboxTestAccessor;
  class FileSystemTestAccessor;
  class ProcessTestAccessor;
}
#endif
inline uint32_t now_ms() { return static_cast<uint32_t>(::millis()); }
}  // namespace bridge

#include <Arduino.h>
#include <Stream.h>

#undef min
#undef max

#include "config/bridge_config.h"
#include <Embedded_Template_Library.h>
#include "etl_profile.h"
#include <etl/algorithm.h>
#include <etl/crc32.h>
#include <etl/observer.h>
#include <etl/random.h>
#include "hal/hal.h"

#include <PacketSerial.h>
#include <Codecs/COBS.h>
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "util/string_copy.h"
#include <etl/bitset.h>
#include <etl/callback_timer.h>
#include <etl/circular_buffer.h>
#include <etl/delegate.h>
#include <etl/expected.h>
#include <etl/flat_map.h>
#include <etl/optional.h>
#include <etl/queue.h>
#include <etl/pool.h>
#include <etl/variant.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <etl/vector.h>

#include "fsm/bridge_fsm.h"
#include "router/command_router.h"

static_assert(rpc::MAX_PAYLOAD_SIZE <= 1024,
              "Payload size exceeds safety limits for small RAM targets");

inline uint16_t getFreeMemory() { return bridge::hal::getFreeMemory(); }

namespace bridge {

enum FlagId : uint8_t {
  FRAME_RECEIVED = 0,
  BRIDGE_BEGUN = 1,
  NUM_FLAGS = 2
};

}  // namespace bridge

// [SIL-2] Serial Port Selection logic
#if defined(BRIDGE_FORCE_SERIAL0) || defined(BRIDGE_EMULATION)
#define BRIDGE_DEFAULT_SERIAL_PORT Serial
#elif defined(ARDUINO_ARCH_AVR) && (defined(ARDUINO_AVR_YUN) || defined(ARDUINO_AVR_MEGA2560) || defined(ARDUINO_AVR_LEONARDO))
#define BRIDGE_DEFAULT_SERIAL_PORT Serial1
#elif defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM)
#define BRIDGE_DEFAULT_SERIAL_PORT Serial1
#else
#define BRIDGE_DEFAULT_SERIAL_PORT Serial
#endif

#include "protocol/BridgeEvents.h"

class BridgeClass
    : public bridge::router::ICommandHandler,
      public etl::observable<BridgeObserver, bridge::config::MAX_OBSERVERS> {
  friend class ConsoleClass;
#if BRIDGE_ENABLE_DATASTORE
  friend class DataStoreClass;
#endif
#if BRIDGE_ENABLE_MAILBOX
  friend class MailboxClass;
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  friend class FileSystemClass;
#endif
#if BRIDGE_ENABLE_PROCESS
  friend class ProcessClass;
#endif
#if BRIDGE_ENABLE_SPI
  friend class SPIServiceClass;
#endif
#if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::TestAccessor;
  friend class bridge::test::ConsoleTestAccessor;
  friend class bridge::test::DataStoreTestAccessor;
  friend class bridge::test::MailboxTestAccessor;
  friend class bridge::test::FileSystemTestAccessor;
  friend class bridge::test::ProcessTestAccessor;
#endif
 public:
  using CommandHandler = etl::delegate<void(const rpc::Frame&)>;
  using DigitalReadHandler = etl::delegate<void(uint8_t)>;
  using AnalogReadHandler = etl::delegate<void(uint16_t)>;
  using GetFreeMemoryHandler = etl::delegate<uint16_t()>;
  using StatusHandler =
      etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;

  explicit BridgeClass(HardwareSerial& serial);
  explicit BridgeClass(Stream& stream);

  using etl::observable<BridgeObserver, bridge::config::MAX_OBSERVERS>::add_observer;
  using etl::observable<BridgeObserver, bridge::config::MAX_OBSERVERS>::remove_observer;

  void begin(unsigned long baudrate =
#ifdef BRIDGE_BAUDRATE
                 BRIDGE_BAUDRATE
#else
                 rpc::RPC_DEFAULT_BAUDRATE
#endif
             ,
             etl::string_view secret = {}, size_t secret_len = 0);
  [[maybe_unused]] void process();
  bool isSynchronized() const { return _fsm.isSynchronized(); }
  bool isUnsynchronized() const { return _fsm.isUnsynchronized(); }
  bool isSyncing() const { return _fsm.isSyncing(); }
  bool isAwaitingAck() const { return _fsm.isAwaitingAck(); }
  bool isIdle() const { return _fsm.isIdle(); }
  bool isFault() const { return _fsm.isFault(); }

  void enterSafeState();
  void forceSafeState();

  /**
   * @brief Manually signal the Linux side to stop sending data.
   * [SIL-2] Only permitted if the bridge is initialized.
   */
  [[maybe_unused]] void sendXoff() {
    if (isSynchronized()) (void)sendFrame(rpc::CommandId::CMD_XOFF, 0);
  }

  /**
   * @brief Manually signal the Linux side to resume sending data.
   * [SIL-2] Only permitted if the bridge is initialized.
   */
  [[maybe_unused]] void sendXon() {
    if (isSynchronized()) (void)sendFrame(rpc::CommandId::CMD_XON, 0);
  }
  void emitStatus(rpc::StatusCode status_code, etl::string_view message = {});
  void emitStatus(rpc::StatusCode status_code,
                  etl::span<const uint8_t> payload);
  void emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message);

  template <typename T>
  void emitStatus(rpc::StatusCode status_code, const T& msg) {
    msgpack::Encoder enc(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE);
    if (msg.encode(enc)) {
      emitStatus(status_code, enc.result());
    }
  }

  [[nodiscard]] bool sendFrame(rpc::StatusCode status_code, uint16_t sequence_id = 0, etl::span<const uint8_t> payload = {});
  [[nodiscard]] bool sendFrame(rpc::CommandId command_id, uint16_t sequence_id = 0, etl::span<const uint8_t> payload = {});

  template <typename T>
  bool sendPbCommand(rpc::CommandId command_id, uint16_t sequence_id, const T& msg) {
    msgpack::Encoder enc(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE);
    if (!msg.encode(enc)) {
      return false; // GCOVR_EXCL_LINE — defensive: encode failure requires corrupt message
    }
    return sendFrame(command_id, sequence_id, enc.result());
  }

  template <typename T>
  bool sendPbFrame(rpc::StatusCode status_code, uint16_t sequence_id, const T& msg) {
    msgpack::Encoder enc(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE);
    if (!msg.encode(enc)) {
      return false; // GCOVR_EXCL_LINE — defensive: encode failure requires corrupt message
    }
    _sendRawFrame(rpc::to_underlying(status_code), sequence_id, enc.result());
    return true;
  }

  [[maybe_unused]] inline void onCommand(CommandHandler handler) { _command_handler = handler; }
  [[maybe_unused]] inline void onDigitalReadResponse(DigitalReadHandler handler) {
    _digital_read_handler = handler;
  }
  [[maybe_unused]] inline void onAnalogReadResponse(AnalogReadHandler handler) {
    _analog_read_handler = handler;
  }
  [[maybe_unused]] inline void onGetFreeMemoryResponse(GetFreeMemoryHandler handler) {
    _get_free_memory_handler = handler;
  }
  [[maybe_unused]] inline void onStatus(StatusHandler handler) { _status_handler = handler; }

  // Stream helpers
  inline void flushStream() { _stream.flush(); }

 protected:
  void onStatusCommand(const bridge::router::CommandContext& ctx) override;
  void onSystemCommand(const bridge::router::CommandContext& ctx) override;
  void onGpioCommand(const bridge::router::CommandContext& ctx) override;
  void onConsoleCommand(const bridge::router::CommandContext& ctx) override;
  void onDataStoreCommand(const bridge::router::CommandContext& ctx) override;
  void onMailboxCommand(const bridge::router::CommandContext& ctx) override;
  void onFileSystemCommand(const bridge::router::CommandContext& ctx) override;
  void onProcessCommand(const bridge::router::CommandContext& ctx) override;
  void onSpiCommand(const bridge::router::CommandContext& ctx) override;

  void _handleGpioMessage(const bridge::router::CommandContext& ctx, etl::monostate);
  void _handleGpioMessage(const bridge::router::CommandContext& ctx, const rpc::payload::PinMode& msg);
  void _handleGpioMessage(const bridge::router::CommandContext& ctx, const rpc::payload::DigitalWrite& msg);
  void _handleGpioMessage(const bridge::router::CommandContext& ctx, const rpc::payload::AnalogWrite& msg);
  void _handleGpioMessage(const bridge::router::CommandContext& ctx, const rpc::payload::PinRead& msg);

  void _handleSystemMessage(const bridge::router::CommandContext& ctx, etl::monostate);
  void _handleSystemMessage(const bridge::router::CommandContext& ctx, const rpc::payload::SetBaudratePacket& msg);
  void _handleSystemMessage(const bridge::router::CommandContext& ctx, const rpc::payload::EnterBootloader& msg);

  void onUnknownCommand(const bridge::router::CommandContext& ctx) override;

  // PacketSerial2 callback
  void _onPacketReceived(etl::span<const uint8_t> packet);

 private:
  void _handleStatusAck(const bridge::router::CommandContext& ctx);
  void _handleStatusMalformed(const bridge::router::CommandContext& ctx);
  void _unusedCommandSlot(const bridge::router::CommandContext& ctx);
  void _handleGetVersion(const bridge::router::CommandContext& ctx);
  void _handleGetFreeMemory(const bridge::router::CommandContext& ctx);
  void _handleGetCapabilities(const bridge::router::CommandContext& ctx);
  void _handleSetBaudrate(const bridge::router::CommandContext& ctx);
  void _handleEnterBootloader(const bridge::router::CommandContext& ctx);
  void _handleLinkSync(const bridge::router::CommandContext& ctx);
  void _handleLinkReset(const bridge::router::CommandContext& ctx);
  void _applyTimingConfig(const rpc::payload::HandshakeConfig& msg);
  void _handleSetPinMode(const bridge::router::CommandContext& ctx);
  void _handleDigitalWrite(const bridge::router::CommandContext& ctx);
  void _handleAnalogWrite(const bridge::router::CommandContext& ctx);
  void _handleDigitalRead(const bridge::router::CommandContext& ctx);
  void _handleAnalogRead(const bridge::router::CommandContext& ctx);
  void _handleProcessKill(const bridge::router::CommandContext& ctx);
  void _handleSpiBegin(const bridge::router::CommandContext& ctx);
  void _handleSpiEnd(const bridge::router::CommandContext& ctx);
  void _handleSpiTransfer(const bridge::router::CommandContext& ctx);
  void _handleSpiSetConfig(const bridge::router::CommandContext& ctx);

  template <typename TResponse, typename TValid, typename TFunc>
  void _handlePinRead(const bridge::router::CommandContext& ctx, rpc::CommandId resp_cmd, TValid valid_func, TFunc read_func) {
    _withPayload<rpc::payload::PinRead>(ctx, [&](const rpc::payload::PinRead& msg) {
      if (valid_func(msg.pin)) {
        TResponse resp = {};
        resp.value = read_func(msg.pin);
        _sendPbResponse(resp_cmd, ctx.sequence_id, resp);
      } else {
        emitStatus(rpc::StatusCode::STATUS_ERROR);
      }
    });
  }

  void _handleConsoleWrite(const bridge::router::CommandContext& ctx);

  void _handleDatastoreGetResp(const bridge::router::CommandContext& ctx);

  void _handleMailboxPush(const bridge::router::CommandContext& ctx);
  void _handleMailboxReadResp(const bridge::router::CommandContext& ctx);
  void _handleMailboxAvailableResp(const bridge::router::CommandContext& ctx);

  void _handleFileWrite(const bridge::router::CommandContext& ctx);
  void _handleFileRead(const bridge::router::CommandContext& ctx);
  void _handleFileRemove(const bridge::router::CommandContext& ctx);
  void _handleFileReadResp(const bridge::router::CommandContext& ctx);

  void _handleProcessRunAsyncResp(const bridge::router::CommandContext& ctx);
  void _handleProcessPollResp(const bridge::router::CommandContext& ctx);

  void _markRxProcessed(const rpc::Frame& frame);
  void _dispatchCommand(const rpc::Frame& frame, uint16_t sequence_id);
  void _sendRawFrame(uint16_t command_id, uint16_t sequence_id, etl::span<const uint8_t> payload);
  void _handleReceivedFrame(etl::span<const uint8_t> decoded_payload);
  etl::expected<void, rpc::FrameError> _decompressFrame(const rpc::Frame& original, rpc::Frame& effective);
  bool _isHandshakeCommand(uint16_t command_id) const;
  bool _isSecurityCheckPassed(uint16_t command_id) const;
  bool _sendFrame(uint16_t command_id, uint16_t sequence_id, etl::span<const uint8_t> payload);

  template <typename TPacket, typename TFunc>
  void _handlePinSetter(const bridge::router::CommandContext& ctx, TFunc func) {
    _withPayloadAck<TPacket>(ctx, [this, func](const TPacket& msg) {
      if (bridge::hal::isValidPin(msg.pin)) func(msg);
      else emitStatus(rpc::StatusCode::STATUS_ERROR);
    });
  }

  template <typename TBuffer>
  static void safePush(TBuffer& buffer, etl::span<const uint8_t> data) {
    if (data.empty()) return;
    BRIDGE_ATOMIC_BLOCK {
      const size_t space = buffer.capacity() - buffer.size();
      const size_t to_copy = etl::min(data.size(), space);
      buffer.push(data.begin(), data.begin() + to_copy);
    }
  }

  template <typename F> void _withAck(const bridge::router::CommandContext& ctx, F handler) {
    if (!ctx.is_duplicate) handler();
    if (ctx.requires_ack) _sendAckAndFlush(ctx.raw_command, ctx.sequence_id);
  }

  template <typename F> void _withResponse(const bridge::router::CommandContext& ctx, F handler) {
    if (!ctx.is_duplicate) handler();
  }

  template <typename TPacket, typename F> void _withPayloadAck(const bridge::router::CommandContext& ctx, F handler) { // GCOVR_EXCL_START — per-instantiation template: gcovr counts each specialization separately
    if (!ctx.is_duplicate) {
      auto res = rpc::Payload::parse<TPacket>(*ctx.frame);
      if (res.has_value()) handler(res.value());
    }
    if (ctx.requires_ack) _sendAckAndFlush(ctx.raw_command, ctx.sequence_id);
  } // GCOVR_EXCL_STOP // GCOVR_EXCL_LINE

  template <typename TPacket, typename F> void _withPayload(const bridge::router::CommandContext& ctx, F handler) { // GCOVR_EXCL_START — per-instantiation parse branch
    auto res = rpc::Payload::parse<TPacket>(*ctx.frame);
    if (res.has_value()) handler(res.value());
  } // GCOVR_EXCL_STOP // GCOVR_EXCL_LINE

  template <typename T> void _sendPbResponse(rpc::CommandId cmd, uint16_t sequence_id, const T& msg) {
    sendPbCommand(cmd, sequence_id, msg);
  }

  template <typename T> void _sendPbResponse(rpc::StatusCode status, uint16_t sequence_id, const T& msg) {
    sendPbFrame(status, sequence_id, msg);
  }

  void _onAckTimeout();
  void _onBaudrateChange();
  void _onRxDedupe();
  void _onStartupStabilized();
  void _handleAck(uint16_t command_id);
  void _handleMalformed(uint16_t command_id);
  void _sendAckAndFlush(uint16_t command_id, uint16_t sequence_id);
  void _sendError(rpc::StatusCode status, uint16_t command_id = 0, uint16_t sequence_id = 0);
  void _computeHandshakeTag(etl::span<const uint8_t> nonce, etl::span<uint8_t> out_tag);
  void _flushPendingTxQueue();
  void _clearPendingTxQueue();
  void _clearAckState();
  void _retransmitLastFrame();

  bool _isQueueFull() const {
    bool full = false;
    BRIDGE_ATOMIC_BLOCK { full = _pending_tx_queue.full(); }
    return full;
  }

  Stream& _stream;
  HardwareSerial* _hardware_serial;
  etl::vector<uint8_t, 32> _shared_secret;
  
  // PacketSerial2 integration with SIL-2 policies
  using PacketSerialType = PacketSerial2::PacketSerial<
      PacketSerial2::COBS, 
      PacketSerial2::NoCRC, 
      PacketSerial2::NoLock, 
      PacketSerial2::NoWatchdog // [SIL-2] BridgeClass centralizes WDT management
  >;

  rpc::FrameBuilder _frame_builder;
  rpc::FrameError _last_parse_error;

  etl::bitset<bridge::NUM_FLAGS> _flags;

  rpc::Frame _rx_frame;
  etl::random_xorshift _rng;
  uint16_t _last_command_id;
  uint16_t _tx_sequence_id;
  uint8_t _retry_count;
  uint32_t _pending_baudrate;

  // RX raw packet storage (COBS unencoded bytes)
  etl::array<uint8_t, bridge::config::RX_BUFFER_SIZE> _rx_storage;

  struct RxHistory {
    etl::circular_buffer<uint16_t, bridge::config::RX_HISTORY_SIZE> buffer;
    bool contains(uint16_t id) const {
      return etl::find(buffer.begin(), buffer.end(), id) != buffer.end();
    }
    void push(uint16_t id) {
      if (buffer.full()) buffer.pop();
      buffer.push(id);
    }
    void clear() { buffer.clear(); }
  };
  RxHistory _rx_history;

  uint16_t _consecutive_crc_errors;
  uint16_t _ack_timeout_ms;
  uint8_t _ack_retry_limit;
  uint32_t _response_timeout_ms;
  CommandHandler _command_handler;
  DigitalReadHandler _digital_read_handler;
  AnalogReadHandler _analog_read_handler;
  GetFreeMemoryHandler _get_free_memory_handler;
  StatusHandler _status_handler;

  // [SIL-2] Dedicated buffers to ensure memory integrity during dispatch.
  // Using independent arrays avoids aliasing/overlap during decompression and decoding.
  etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE + 2> _transient_buffer;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> _decompression_buffer;

  struct TxPayloadBuffer {
    etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> data;
  };

  struct PendingTxFrame {
    uint16_t command_id;
    uint16_t payload_length;
    TxPayloadBuffer* buffer;
  };
  etl::queue<PendingTxFrame, bridge::config::MAX_PENDING_TX_FRAMES> _pending_tx_queue;

  #if defined(ARDUINO_ARCH_AVR)
  etl::pool<TxPayloadBuffer, 1> _tx_payload_pool;
  #else
  etl::pool<TxPayloadBuffer, bridge::config::MAX_PENDING_TX_FRAMES> _tx_payload_pool;
  #endif

  using GpioCommandVariant = etl::variant<
      etl::monostate,
      rpc::payload::PinMode,
      rpc::payload::DigitalWrite,
      rpc::payload::AnalogWrite,
      rpc::payload::PinRead
  >;

  using SystemCommandVariant = etl::variant<
      etl::monostate,
      rpc::payload::SetBaudratePacket,
      rpc::payload::EnterBootloader
  >;

  bridge::fsm::BridgeFsm _fsm;
  etl::array<etl::timer::id::type, bridge::scheduler::NUMBER_OF_TIMERS> _timer_ids;
  etl::callback_timer<bridge::scheduler::NUMBER_OF_TIMERS> _timers;
  etl::icallback_timer::callback_type _on_ack_timeout_delegate;
  etl::icallback_timer::callback_type _on_rx_dedupe_delegate;
  etl::icallback_timer::callback_type _on_baudrate_change_delegate;
  etl::icallback_timer::callback_type _on_startup_stabilized_delegate;
  uint32_t _last_tick_millis;

  PacketSerialType _packet_serial;
  };
extern BridgeClass Bridge;

// Include services at the end to ensure BridgeClass is defined
#include "services/Console.h"
#if BRIDGE_ENABLE_DATASTORE
#include "services/DataStore.h"
#endif
#if BRIDGE_ENABLE_MAILBOX
#include "services/Mailbox.h"
#endif
#if BRIDGE_ENABLE_FILESYSTEM
#include "services/FileSystem.h"
#endif
#if BRIDGE_ENABLE_PROCESS
#include "services/Process.h"
#endif

#endif

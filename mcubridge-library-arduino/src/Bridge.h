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
#include "etl/algorithm.h"
#include "etl/crc32.h"
#include "etl/observer.h"
#include "etl/random.h"
#include "hal/hal.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_cobs.h"
#include "protocol/rpc_structs.h"
#include "util/pb_copy.h"

#include "nanopb/pb_common.h"
#include "nanopb/pb_decode.h"
#include "nanopb/pb_encode.h"
#include "etl/array.h"
#include "etl/circular_buffer.h"
#include "etl/delegate.h"
#include "etl/expected.h"
#include "etl/optional.h"
#include "etl/queue.h"
#include "etl/span.h"
#include "etl/string_view.h"
#include "etl/vector.h"

#include "fsm/bridge_fsm.h"
#include "router/command_router.h"

static_assert(rpc::MAX_PAYLOAD_SIZE <= 1024,
              "Payload size exceeds safety limits for small RAM targets");

inline uint16_t getFreeMemory() { return bridge::hal::getFreeMemory(); }

namespace bridge {
namespace config {

#if defined(ARDUINO_ARCH_AVR) && BRIDGE_ENABLE_WATCHDOG
static constexpr uint16_t WATCHDOG_TIMEOUT_VAL = WDTO_2S;
#elif (defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_ESP8266)) && BRIDGE_ENABLE_WATCHDOG
static constexpr uint32_t WATCHDOG_TIMEOUT_MS = 2000UL;
#endif

#ifdef BRIDGE_FIRMWARE_VERSION_MAJOR
static constexpr uint8_t FIRMWARE_VERSION_MAJOR = BRIDGE_FIRMWARE_VERSION_MAJOR;
#else
static constexpr uint8_t FIRMWARE_VERSION_MAJOR = 2;
#endif

#ifdef BRIDGE_FIRMWARE_VERSION_MINOR
static constexpr uint8_t FIRMWARE_VERSION_MINOR = BRIDGE_FIRMWARE_VERSION_MINOR;
#else
static constexpr uint8_t FIRMWARE_VERSION_MINOR = 8;
#endif

}  // namespace config
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

namespace rpc {
struct CobsState {
  uint16_t block_len;
  uint16_t bytes_received;
  uint16_t decoded_len;
  uint8_t code;
  bool in_sync;
  etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE + 2> buffer;
};
}

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
  void process();
  bool isSynchronized() const { return _fsm.isSynchronized(); }
  bool isUnsynchronized() const { return _fsm.isUnsynchronized(); }
  bool isSyncing() const { return _fsm.isSyncing(); }
  bool isAwaitingAck() const { return _fsm.isAwaitingAck(); }
  bool isIdle() const { return _fsm.isIdle(); }
  bool isFault() const { return _fsm.isFault(); }

  void enterSafeState();
  void emitStatus(rpc::StatusCode status_code, etl::string_view message = {});
  void emitStatus(rpc::StatusCode status_code,
                  etl::span<const uint8_t> payload);
  void emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message);

  bool sendFrame(rpc::StatusCode status_code, etl::span<const uint8_t> payload = {});
  bool sendFrame(rpc::CommandId command_id, etl::span<const uint8_t> payload = {});
  bool sendChunkyFrame(rpc::CommandId command_id, etl::span<const uint8_t> header, etl::span<const uint8_t> data) {
    size_t len = etl::min(header.size() + data.size(), sizeof(_transient_buffer));
    etl::copy_n(header.data(), etl::min(header.size(), len), _transient_buffer);
    if (len > header.size()) etl::copy_n(data.data(), len - header.size(), _transient_buffer + header.size());
    return sendFrame(command_id, etl::span<const uint8_t>(_transient_buffer, len));
  }

  template <typename T>
  bool sendPbCommand(rpc::CommandId command_id, const T& msg) {
    pb_ostream_t stream = pb_ostream_from_buffer(_transient_buffer, sizeof(_transient_buffer));
    if (!pb_encode(&stream, rpc::Payload::Descriptor<T>::fields(), &msg)) {
      return false;
    }
    return sendFrame(command_id,
                       etl::span<const uint8_t>(_transient_buffer, stream.bytes_written));
  }

  // [SIL-2] Boilerplate reduction helpers for services
  template <typename T, typename KeyField>
  bool sendKeyCommand(rpc::CommandId cmd, etl::string_view key, KeyField T::*field) {
    T msg = {};
    rpc::util::pb_copy_string(key, msg.*field, sizeof(msg.*field));
    return sendPbCommand(cmd, msg);
  }

  template <typename T, typename KeyField, typename DataField>
  bool sendKeyDataCommand(rpc::CommandId cmd, etl::string_view key, KeyField T::*k_field,
                          etl::span<const uint8_t> data, DataField T::*d_field) {
    T msg = {};
    rpc::util::pb_copy_string(key, msg.*k_field, sizeof(msg.*k_field));
    rpc::util::pb_setup_encode_span(msg.*d_field, data);
    return sendPbCommand(cmd, msg);
  }

  template <typename T, typename DataField>
  bool sendDataCommand(rpc::CommandId cmd, etl::span<const uint8_t> data, DataField T::*field) {
    T msg = {};
    rpc::util::pb_setup_encode_span(msg.*field, data);
    return sendPbCommand(cmd, msg);
  }

  template <typename T>
  bool sendPbFrame(rpc::StatusCode status_code, const T& msg) {
    pb_ostream_t stream = pb_ostream_from_buffer(_transient_buffer, sizeof(_transient_buffer));
    if (!pb_encode(&stream, rpc::Payload::Descriptor<T>::fields(), &msg)) {
      return false;
    }
    _sendRawFrame(rpc::to_underlying(status_code),
                  etl::span<const uint8_t>(_transient_buffer, stream.bytes_written));
    return true;
  }

  inline void onCommand(CommandHandler handler) { _command_handler = handler; }
  inline void onDigitalReadResponse(DigitalReadHandler handler) {
    _digital_read_handler = handler;
  }
  inline void onAnalogReadResponse(AnalogReadHandler handler) {
    _analog_read_handler = handler;
  }
  inline void onGetFreeMemoryResponse(GetFreeMemoryHandler handler) {
    _get_free_memory_handler = handler;
  }
  inline void onStatus(StatusHandler handler) { _status_handler = handler; }

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
  void onUnknownCommand(const bridge::router::CommandContext& ctx) override;

 private:
  struct RxHistoryItem {
    uint32_t crc;
    uint32_t timestamp;
  };

  void _handleStatusAck(const bridge::router::CommandContext& ctx);
  void _handleStatusMalformed(const bridge::router::CommandContext& ctx);
  void _handleGetVersion(const bridge::router::CommandContext& ctx);
  void _handleGetFreeMemory(const bridge::router::CommandContext& ctx);
  void _handleGetCapabilities(const bridge::router::CommandContext& ctx);
  void _handleSetBaudrate(const bridge::router::CommandContext& ctx);
  void _handleLinkSync(const bridge::router::CommandContext& ctx);
  void _handleLinkReset(const bridge::router::CommandContext& ctx);
  void _handleSetPinMode(const bridge::router::CommandContext& ctx);
  void _handleDigitalWrite(const bridge::router::CommandContext& ctx);
  void _handleAnalogWrite(const bridge::router::CommandContext& ctx);
  void _handleDigitalRead(const bridge::router::CommandContext& ctx);
  void _handleAnalogRead(const bridge::router::CommandContext& ctx);

  template <typename TResponse, typename TValid, typename TFunc>
  void _handlePinRead(const bridge::router::CommandContext& ctx, rpc::CommandId resp_cmd, TValid valid_func, TFunc read_func) {
    _withPayload<rpc::payload::PinRead>(ctx, [&](const rpc::payload::PinRead& msg) {
      if (valid_func(msg.pin)) {
        TResponse resp = {};
        resp.value = static_cast<uint32_t>(read_func(msg.pin));
        _sendPbResponse(resp_cmd, resp);
      } else {
        emitStatus(rpc::StatusCode::STATUS_ERROR);
      }
    });
  }

  void _handleConsoleWrite(const bridge::router::CommandContext& ctx);

#if BRIDGE_ENABLE_DATASTORE
  void _handleDatastoreGetResp(const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_MAILBOX
  void _handleMailboxPush(const bridge::router::CommandContext& ctx);
  void _handleMailboxReadResp(const bridge::router::CommandContext& ctx);
  void _handleMailboxAvailableResp(const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  void _handleFileWrite(const bridge::router::CommandContext& ctx);
  void _handleFileReadResp(const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_PROCESS
  void _handleProcessRunAsyncResp(const bridge::router::CommandContext& ctx);
  void _handleProcessPollResp(const bridge::router::CommandContext& ctx);
#endif

  void _markRxProcessed(const rpc::Frame& frame);
  bool _isRecentDuplicateRx(const rpc::Frame& frame) const;
  void dispatch(const rpc::Frame& frame);
  void _sendRawFrame(uint16_t command_id, etl::span<const uint8_t> payload);
  void _sendFrameInternal(const rpc::Frame& frame);
  void _processIncomingByte(uint8_t byte);
  void _handleReceivedFrame();
  etl::expected<void, rpc::FrameError> _decompressFrame(const rpc::Frame& original, rpc::Frame& effective);
  bool _isHandshakeCommand(uint16_t command_id) const;
  bool _isSecurityCheckPassed(uint16_t command_id) const;
  bool _sendFrame(uint16_t command_id, etl::span<const uint8_t> payload);

  using CmdHandler = void (BridgeClass::*)(const bridge::router::CommandContext&);
  void _dispatchJumpTable(const bridge::router::CommandContext& ctx, uint16_t min_id, const CmdHandler* handlers, uint8_t count, uint8_t stride = 1);

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

  template <typename TPacket, typename TDelegate, typename TFunc>
  void _dispatchResponse(const bridge::router::CommandContext& ctx, TDelegate& delegate, TFunc func) {
    _withPayload<TPacket>(ctx, [this, &delegate, func](const TPacket& msg) {
      if (delegate.is_valid()) func(delegate, msg);
    });
  }

  template <typename F> void _withAck(const bridge::router::CommandContext& ctx, F handler) {
    if (!ctx.is_duplicate) handler();
    if (ctx.requires_ack) _sendAckAndFlush(ctx.frame->header.command_id);
  }

  template <typename F> void _withResponse(const bridge::router::CommandContext& ctx, F handler) {
    if (!ctx.is_duplicate) handler();
  }

  template <typename TPacket, typename F> void _withPayloadAck(const bridge::router::CommandContext& ctx, F handler, TPacket msg = {}) {
    if (!ctx.is_duplicate) {
      auto res = rpc::Payload::parse<TPacket>(*ctx.frame, msg);
      if (res.has_value()) handler(res.value());
    }
    if (ctx.requires_ack) _sendAckAndFlush(ctx.frame->header.command_id);
  }

  template <typename TPacket, typename F> void _withPayloadResponse(const bridge::router::CommandContext& ctx, F handler, TPacket msg = {}) {
    if (!ctx.is_duplicate) {
      auto res = rpc::Payload::parse<TPacket>(*ctx.frame, msg);
      if (res.has_value()) handler(res.value());
    }
  }

  template <typename TPacket, typename F, typename TField>
  void _dispatchWithBytes(const bridge::router::CommandContext& ctx, TField TPacket::*field, F handler, bool ack = false) {
    etl::span<uint8_t> span(_transient_buffer, sizeof(_transient_buffer));
    TPacket msg = {};
    rpc::util::pb_setup_decode_span(msg.*field, span);

    auto logic = [&handler, &span](const TPacket&) {
      handler(etl::span<const uint8_t>(span.data(), span.size()));
    };

    if (ack) _withPayloadAck<TPacket>(ctx, logic, msg);
    else _withPayload<TPacket>(ctx, logic, msg);
  }

  template <typename TPacket, typename F> void _withPayload(const bridge::router::CommandContext& ctx, F handler, TPacket msg = {}) {
    auto res = rpc::Payload::parse<TPacket>(*ctx.frame, msg);
    if (res.has_value()) handler(res.value());
  }

  template <typename T> void _sendPbResponse(rpc::CommandId cmd, const T& msg) {
    sendPbCommand(cmd, msg);
  }

  template <typename T> void _sendPbResponse(rpc::StatusCode status, const T& msg) {
    sendPbFrame(status, msg);
  }

  void _onAckTimeout();
  void _onBaudrateChange();
  void _onRxDedupe();
  void _onStartupStabilized();
  void _handleAck(uint16_t command_id);
  void _handleMalformed(uint16_t command_id);
  void _sendAckAndFlush(uint16_t command_id);
  void _doEmitStatus(rpc::StatusCode status_code, etl::span<const uint8_t> payload);
  void _computeHandshakeTag(etl::span<const uint8_t> nonce, uint8_t* out_tag);
  void _applyTimingConfig(etl::span<const uint8_t> payload);
  void _flushPendingTxQueue();
  void _clearPendingTxQueue();
  void _clearAckState();
  void _retransmitLastFrame();

  bool _isQueueFull() const {
    bool full = false;
    BRIDGE_ATOMIC_BLOCK { full = _pending_tx_queue.full(); }
    return full;
  }

  bool _isQueueEmpty() const {
    bool empty = true;
    BRIDGE_ATOMIC_BLOCK { empty = _pending_tx_queue.empty(); }
    return empty;
  }

  Stream& _stream;
  HardwareSerial* _hardware_serial;
  etl::vector<uint8_t, 32> _shared_secret;
  rpc::CobsState _cobs;
  rpc::FrameBuilder _frame_builder;
  etl::optional<rpc::FrameError> _last_parse_error;
  
  struct {
    uint8_t frame_received : 1;
    uint8_t startup_stabilized : 1;
    uint8_t reserved : 6;
  } _flags;

  rpc::Frame _rx_frame;
  etl::random_xorshift _rng;
  uint16_t _last_command_id;
  uint8_t _retry_count;
  uint32_t _pending_baudrate;
  
  etl::circular_buffer<RxHistoryItem, bridge::config::RX_HISTORY_SIZE> _rx_history;
  
  uint16_t _consecutive_crc_errors;
  uint16_t _ack_timeout_ms;
  uint8_t _ack_retry_limit;
  uint32_t _response_timeout_ms;
  CommandHandler _command_handler;
  DigitalReadHandler _digital_read_handler;
  AnalogReadHandler _analog_read_handler;
  GetFreeMemoryHandler _get_free_memory_handler;
  StatusHandler _status_handler;

  // [OPTIMIZATION] Shared buffer for transient operations (TX encoding, RX processing)
  // Large enough for MAX_RAW_FRAME_SIZE + 2 (COBS overhead)
  uint8_t _transient_buffer[rpc::MAX_RAW_FRAME_SIZE + 2];

  struct PendingTxFrame {
    uint16_t command_id;
    uint16_t payload_length;
    uint16_t buffer_offset;
  };
  etl::queue<PendingTxFrame, bridge::config::MAX_PENDING_TX_FRAMES> _pending_tx_queue;
#if defined(ARDUINO_ARCH_AVR)
  // On AVR, we only have space for 1 pending frame anyway.
  uint8_t _tx_payload_pool[rpc::MAX_PAYLOAD_SIZE];
#else
  uint8_t _tx_payload_pool[bridge::config::MAX_PENDING_TX_FRAMES * rpc::MAX_PAYLOAD_SIZE];
#endif
  uint16_t _tx_pool_head;

  bridge::fsm::BridgeFsm _fsm;
  bridge::scheduler::SimpleTimer<4> _timers;
  uint32_t _last_tick_millis;
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

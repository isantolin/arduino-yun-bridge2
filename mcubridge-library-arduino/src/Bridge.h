/**
 * @file Bridge.h
 * @brief Arduino MCU Bridge v2 - MCU-side RPC library.
 */
#ifndef BRIDGE_H
#define BRIDGE_H

#include "etl_profile.h"
#include <Arduino.h>
#include <Stream.h>

#include "config/bridge_config.h"
#include "protocol/BridgeEvents.h" 
#include "etl/algorithm.h"
#include "etl/crc32.h"
#include "etl/observer.h"
#include "etl/random.h"
#include "hal/hal.h"
#include "protocol/PacketBuilder.h"
#include "protocol/rpc_cobs.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#undef min
#undef max
#include "etl/array.h"
#include "etl/bitset.h"
#include "etl/circular_buffer.h"
#include "etl/delegate.h"
#include "etl/message_bus.h"
#include "etl/message_router.h"
#include "etl/optional.h"
#include "etl/queue.h"
#include "etl/span.h"
#include "etl/string_view.h"
#include "etl/vector.h"
#include "etl/callback_timer.h"

#include "fsm/bridge_fsm.h"
#include "router/command_router.h"

namespace bridge {
namespace test {
class TestAccessor;
class ConsoleTestAccessor;
class DataStoreTestAccessor;
class ProcessTestAccessor;
}
}

inline uint16_t getFreeMemory() { return bridge::hal::getFreeMemory(); }

#ifndef BRIDGE_ENABLE_WATCHDOG
constexpr bool kBridgeEnableWatchdog = true;
#else
constexpr bool kBridgeEnableWatchdog = (BRIDGE_ENABLE_WATCHDOG != 0);
#endif

#ifdef BRIDGE_FIRMWARE_VERSION_MAJOR
constexpr uint8_t kDefaultFirmwareVersionMajor = BRIDGE_FIRMWARE_VERSION_MAJOR;
#else
constexpr uint8_t kDefaultFirmwareVersionMajor = 2;
#endif

#ifdef BRIDGE_FIRMWARE_VERSION_MINOR
constexpr uint8_t kDefaultFirmwareVersionMinor = BRIDGE_FIRMWARE_VERSION_MINOR;
#else
constexpr uint8_t kDefaultFirmwareVersionMinor = 6;
#endif

#ifndef BRIDGE_MAX_OBSERVERS
#if defined(ARDUINO_ARCH_AVR)
#define BRIDGE_MAX_OBSERVERS 2
#else
#define BRIDGE_MAX_OBSERVERS 4
#endif
#endif

class BridgeClass
    : public etl::imessage_router,
      public etl::observable<BridgeObserver, BRIDGE_MAX_OBSERVERS> {
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
  friend class ConsoleClass;
  friend class bridge::test::TestAccessor;
  friend class bridge::test::ConsoleTestAccessor;
  friend class bridge::test::DataStoreTestAccessor;
  friend class bridge::test::ProcessTestAccessor;

 public:
  using CommandHandler = etl::delegate<void(const rpc::Frame&)>;
  using DigitalReadHandler = etl::delegate<void(uint8_t)>;
  using AnalogReadHandler = etl::delegate<void(uint16_t)>;
  using GetFreeMemoryHandler = etl::delegate<void(uint16_t)>;
  using StatusHandler = etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;

  explicit BridgeClass(Stream& stream);

  void begin(unsigned long baudrate = rpc::RPC_DEFAULT_BAUDRATE,
             etl::string_view secret = {}, size_t secret_len = 0);
  void process();
  bool isSynchronized() const { return _fsm.isSynchronized(); }
  bool isAwaitingAck() const { return _fsm.isAwaitingAck(); }
  bool isIdle() const { return _fsm.isIdle(); }
  bool isFault() const { return _fsm.isFault(); }

  // [SIL-2] imessage_router interface
  void receive(const etl::imessage& msg) override;
  bool accepts(etl::message_id_t id) const override;
  bool is_null_router() const override { return false; }
  bool is_producer() const override { return true; }
  bool is_consumer() const override { return true; }

  // [SIL-2] Core Command Handlers
  void onStatusCommand(const bridge::router::CommandMessage& msg);
  void onSystemCommand(const bridge::router::CommandMessage& msg);
  void onGpioCommand(const bridge::router::CommandMessage& msg);
  void onUnknownCommand(const bridge::router::CommandMessage& msg);

  // Timer Handlers
  void onAckTimeout();
  void onBaudrateChange();
  void onRxDedupe();
  void onStartupStabilized();

  bool sendFrame(rpc::CommandId command_id, etl::span<const uint8_t> payload = etl::span<const uint8_t>());
  bool sendFrame(rpc::StatusCode status_code, etl::span<const uint8_t> payload = etl::span<const uint8_t>());

  bool sendStringCommand(rpc::CommandId command_id, etl::string_view str, size_t max_len);
  bool sendKeyValCommand(rpc::CommandId command_id, etl::string_view key, size_t max_key, etl::string_view val, size_t max_val);

  template <typename T>
  bool sendValue(rpc::CommandId command_id, T value) {
    etl::array<uint8_t, sizeof(T)> payload;
    if (sizeof(T) == 1) {
      payload[0] = static_cast<uint8_t>(value);
    } else if (sizeof(T) == 2) {
      rpc::write_u16_be(payload.data(), static_cast<uint16_t>(value));
    } else if (sizeof(T) == 4) {
      rpc::write_u32_be(payload.data(), static_cast<uint32_t>(value));
    } else {
      static_assert(sizeof(T) <= 4, "Unsupported value size for sendValue");
      return false;
    }
    return sendFrame(command_id, etl::span<const uint8_t>(payload.data(), payload.size()));
  }

  void flushStream() { _stream.flush(); }
  void enterSafeState();
  void emitStatus(rpc::StatusCode status_code, etl::string_view message = {});
  void emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message);

  void onStatus(StatusHandler handler) { _status_handler = handler; }
  void onDigitalReadResponse(DigitalReadHandler handler) { _digital_read_handler = handler; }
  void onAnalogReadResponse(AnalogReadHandler handler) { _analog_read_handler = handler; }
  void onGetFreeMemoryResponse(GetFreeMemoryHandler handler) { _get_free_memory_handler = handler; }
  void onCommand(CommandHandler handler) { _command_handler = handler; }

  bool sendChunkyFrame(rpc::CommandId command_id, etl::span<const uint8_t> header, etl::span<const uint8_t> data);

  // [SIL-2] DRY Command Helpers
  template <typename F>
  void _withAck(const bridge::router::CommandMessage& msg, F handler) {
    if (msg.is_duplicate) {
      _sendAckAndFlush(msg.raw_command);
    } else {
      handler();
      _markRxProcessed(*msg.frame);
      _sendAck(msg.raw_command);
    }
  }

  template <typename F>
  void _withResponse(const bridge::router::CommandMessage& msg, F handler) {
    handler();
    if (!msg.is_duplicate) {
      _markRxProcessed(*msg.frame);
    }
  }

  template <typename T, typename F>
  void _withPayloadResponse(const bridge::router::CommandMessage& msg, F handler) {
    auto pl_res = rpc::Payload::parse<T>(*msg.frame);
    if (pl_res) {
      handler(*pl_res);
      if (!msg.is_duplicate) _markRxProcessed(*msg.frame);
    }
  }

  template <typename T, typename F>
  void _withPayloadAck(const bridge::router::CommandMessage& msg, F handler) {
    _withAck(msg, [&]() {
      auto pl_res = rpc::Payload::parse<T>(*msg.frame);
      if (pl_res) handler(*pl_res);
    });
  }

  template <typename T, typename F>
  void _withPayload(const bridge::router::CommandMessage& msg, F handler) {
    if (msg.is_duplicate) return;
    auto pl_res = rpc::Payload::parse<T>(*msg.frame);
    if (pl_res) {
      handler(*pl_res);
      _markRxProcessed(*msg.frame);
    }
  }

  template <typename T, typename... Args>
  void _sendResponse(rpc::CommandId cmd, Args&&... args) {
    _sendResponse<T>(rpc::to_underlying(cmd), etl::forward<Args>(args)...);
  }

  template <typename T, typename... Args>
  void _sendResponse(rpc::StatusCode status, Args&&... args) {
    _sendResponse<T>(rpc::to_underlying(status), etl::forward<Args>(args)...);
  }

  template <typename T, typename... Args>
  void _sendResponse(uint16_t cmd_raw, Args&&... args) {
    T resp{etl::forward<Args>(args)...};
    etl::array<uint8_t, T::SIZE> buffer;
    resp.encode(buffer.data());
    (void)_sendFrame(cmd_raw, etl::span<const uint8_t>(buffer.data(), T::SIZE));
  }

  void _retransmitLastFrame();
  void _handleAck(uint16_t command_id);
  void _handleMalformed(uint16_t command_id);
  void _sendAck(uint16_t command_id);
  void _sendAckAndFlush(uint16_t command_id);
  void _markRxProcessed(const rpc::Frame& frame);
  bool _isRecentDuplicateRx(const rpc::Frame& frame) const;
  void _applyTimingConfig(const rpc::Frame& frame);
  void _computeHandshakeTag(etl::span<const uint8_t> nonce, uint8_t* out_tag);
  void _doEmitStatus(rpc::StatusCode status_code, etl::span<const uint8_t> payload);

  Stream& _stream;
  HardwareSerial* _hardware_serial;
  etl::vector<uint8_t, 32> _shared_secret;

  struct LastFrame {
    uint16_t command_id;
    etl::vector<uint8_t, rpc::MAX_RAW_FRAME_SIZE> raw;
  } _last_frame;

  struct CobsState {
    uint16_t bytes_received;
    uint8_t block_len;
    uint8_t code;
    uint8_t code_prev;
    bool in_sync;
    etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE> buffer;
  } _cobs;

  volatile bool _frame_received;
  rpc::Frame _rx_frame;
  etl::optional<rpc::FrameError> _last_parse_error;
  etl::random_xorshift _rng;
  uint16_t _last_command_id;
  uint8_t _retry_count;
  uint32_t _pending_baudrate;

  struct RxHistory {
    uint32_t crc;
    uint32_t timestamp;
  };
  etl::circular_buffer<RxHistory, BRIDGE_RX_HISTORY_SIZE> _rx_history;
  volatile uint8_t _consecutive_crc_errors;

  uint16_t _ack_timeout_ms;
  uint8_t _ack_retry_limit;
  uint32_t _response_timeout_ms;

  DigitalReadHandler _digital_read_handler;
  AnalogReadHandler _analog_read_handler;
  GetFreeMemoryHandler _get_free_memory_handler;
  StatusHandler _status_handler;
  CommandHandler _command_handler;

  bridge::fsm::BridgeFsm _fsm;
  etl::callback_timer<4> _timers;
  etl::timer::id::type _timer_ids[4];
  etl::delegate<void()> _timer_callbacks[4]; 
  volatile bool _startup_stabilizing;
  bool _subscribed;
  etl::message_bus<8> _bus;

  template <typename TResp, typename TFunc, typename TValid, typename... Args>
  void _handlePinRead(const bridge::router::CommandMessage& msg,
                      rpc::CommandId resp_cmd, TValid valid_func,
                      TFunc read_func, Args&&... args) {
    auto pl_res = rpc::Payload::parse<rpc::payload::PinRead>(*msg.frame);
    if (pl_res) {
      const rpc::payload::PinRead& pl = *pl_res;
      if (valid_func(pl.pin)) {
        _sendResponse<TResp>(resp_cmd, pl.pin,
                             read_func(pl.pin, etl::forward<Args>(args)...));
      } else {
        (void)sendFrame(rpc::StatusCode::STATUS_MALFORMED);
      }
    }
  }

  void _handleLinkSync(const bridge::router::CommandMessage& msg);
  bool _isHandshakeCommand(uint16_t command_id) const;
  void dispatch(const rpc::Frame& frame);
  bool _sendFrame(uint16_t command_id, etl::span<const uint8_t> payload);
  void _sendRawFrame(uint16_t command_id, etl::span<const uint8_t> payload);
  void _clearAckState();
};

extern BridgeClass Bridge;

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

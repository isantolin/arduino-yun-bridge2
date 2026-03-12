/**
 * @file Bridge.h
 * @brief Arduino MCU Bridge v2 - MCU-side RPC library.
 *
 * This file is part of Arduino MCU Ecosystem v2.
 * (C) 2025-2026 Ignacio Santolin and contributors.
 *
 * [SIL-2 COMPLIANCE - IEC 61508]
 * This library is designed following functional safety guidelines:
 * - No STL usage (prevents heap fragmentation on AVR)
 * - No recursion (deterministic stack usage)
 * - No dynamic allocation post-initialization
 * - All inputs validated against safe ranges
 * - CRC32 integrity on all frames
 * - Defined fail-safe state on error conditions
 *
 * [STRICT NO-STL POLICY]
 * The use of Standard Template Library (STL) headers (e.g., <vector>, <string>,
 * <map>) is STRICTLY PROHIBITED to prevent heap fragmentation and
 * non-deterministic behavior. Reviewers must reject any PR including these
 * headers.
 *
 * @see docs/PROTOCOL.md for protocol specification
 * @see tools/protocol/spec.toml for machine-readable contract
 */
#ifndef BRIDGE_H
#define BRIDGE_H

// [SIL-2] ETL Configuration MUST be first to ensure consistent profile (e.g. no
// CRC tables)
#include "etl_profile.h"
#include <stdint.h>

// Forward declaration of millis() since we might not include Arduino.h yet
#if !defined(BRIDGE_HOST_TEST)
extern "C" unsigned long millis(void);
#else
extern unsigned long millis(void);
#endif

namespace bridge {
/**
 * @brief Helper to cast millis() to strictly typed uint32_t.
 * Prevents repeating static_cast<uint32_t>(millis()) throughout the codebase.
 */
inline uint32_t now_ms() { return static_cast<uint32_t>(::millis()); }
}  // namespace bridge

// [SIL-2] Centralized configuration for class layout consistency (ODR)
#include <Arduino.h>
#include <Stream.h>

#include "config/bridge_config.h"
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
#include "etl/expected.h"
#include "etl/optional.h"
#include "etl/queue.h"
#include "etl/span.h"
#include "etl/string_view.h"
#include "etl/vector.h"

// [SIL-2] Lightweight FSM + Scheduler for deterministic state transitions
#include "fsm/bridge_fsm.h"

// [SIL-2] ETL Message Router for command dispatch
#include "router/command_router.h"

// [SIL-2] Static Constraints
static_assert(rpc::MAX_PAYLOAD_SIZE <= 1024,
              "Payload size exceeds safety limits for small RAM targets");

/**
 * @brief Get free RAM.
 * @return Bytes free.
 */
inline uint16_t getFreeMemory() { return bridge::hal::getFreeMemory(); }

// --- Configuration ---

#ifndef BRIDGE_ENABLE_WATCHDOG
constexpr bool kBridgeEnableWatchdog = true;
#else
constexpr bool kBridgeEnableWatchdog = (BRIDGE_ENABLE_WATCHDOG != 0);

#endif

#if defined(ARDUINO_ARCH_AVR) && BRIDGE_ENABLE_WATCHDOG
#ifndef BRIDGE_WATCHDOG_TIMEOUT
#define BRIDGE_WATCHDOG_TIMEOUT WDTO_2S

#endif

#endif

// [SIL-2] Multi-platform watchdog support
#if defined(ARDUINO_ARCH_ESP32) && BRIDGE_ENABLE_WATCHDOG
#include <esp_task_wdt.h>
#ifndef BRIDGE_WATCHDOG_TIMEOUT_MS
#define BRIDGE_WATCHDOG_TIMEOUT_MS 2000

#endif

#endif

#if defined(ARDUINO_ARCH_ESP8266) && BRIDGE_ENABLE_WATCHDOG
// ESP8266 uses yield() for watchdog - software WDT

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

// [RAM-OPT] Reduced from 4 to 2 observers on AVR for SRAM savings.
#ifndef BRIDGE_MAX_OBSERVERS
#if defined(ARDUINO_ARCH_AVR)
#define BRIDGE_MAX_OBSERVERS 2
#else
#define BRIDGE_MAX_OBSERVERS 4
#endif
#endif

// --- Subsystem Enablement (RAM Optimization) ---
// Note: Macros are now centralized in config/bridge_config.h

// [SIL-2] Resource Allocation Tuning
// Note: BRIDGE_MAX_PENDING_TX_FRAMES moved to bridge_config.h

// [SIL-2] Serial Port Selection logic
// Priority 1: Manual override via BRIDGE_FORCE_SERIAL0 or BRIDGE_EMULATION
// (Always UART0) Priority 2: Boards with dedicated Bridge port (Yun/Mega/Zero
// use Serial1) Priority 3: Standard boards (Uno/Nano use Serial)
#if defined(BRIDGE_FORCE_SERIAL0) || defined(BRIDGE_EMULATION)
#define BRIDGE_DEFAULT_SERIAL_PORT Serial
#elif defined(ARDUINO_ARCH_AVR) &&                                   \
    (defined(__AVR_ATmega32U4__) || defined(__AVR_ATmega2560__) ||   \
     defined(__AVR_ATmega1280__) || defined(ARDUINO_ARCH_SAMD) ||    \
     defined(ARDUINO_ARCH_SAM) || defined(_VARIANT_ARDUINO_ZERO_) || \
     defined(HAVE_HWSERIAL1))
// Boards with multiple UARTs or native USB (Yun, Mega, Zero, Leonardo)
#define BRIDGE_DEFAULT_SERIAL_PORT Serial1
#else
// Standard boards (Uno, Pro Mini)
#define BRIDGE_DEFAULT_SERIAL_PORT Serial

#endif

#if defined(BRIDGE_HOST_TEST)
namespace bridge {
namespace test {
class TestAccessor;
class ConsoleTestAccessor;
class DataStoreTestAccessor;
class ProcessTestAccessor;
}  // namespace test
}  // namespace bridge

#endif

#include "protocol/BridgeEvents.h"

class BridgeClass
    : public bridge::router::ICommandHandler,
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
#if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::TestAccessor;
#endif
 public:
  // Callbacks - [SIL-2] Using etl::delegate for safer, object-oriented
  // callbacks
  using CommandHandler = etl::delegate<void(const rpc::Frame&)>;
  using DigitalReadHandler = etl::delegate<void(uint8_t)>;
  using AnalogReadHandler = etl::delegate<void(uint16_t)>;
  using GetFreeMemoryHandler = etl::delegate<void(uint16_t)>;
  using StatusHandler =
      etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;

#if BRIDGE_ENABLE_DATASTORE
  using DataStoreGetHandler =
      etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>;
#endif

#if BRIDGE_ENABLE_MAILBOX
  using MailboxHandler = etl::delegate<void(etl::span<const uint8_t>)>;
  using MailboxAvailableHandler = etl::delegate<void(uint16_t)>;
#endif

#if BRIDGE_ENABLE_FILESYSTEM
  using FileSystemReadHandler = etl::delegate<void(etl::span<const uint8_t>)>;
#endif

#if BRIDGE_ENABLE_PROCESS
  using ProcessRunHandler =
      etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>,
                         etl::span<const uint8_t>)>;
  using ProcessPollHandler =
      etl::delegate<void(rpc::StatusCode, uint8_t, etl::span<const uint8_t>,
                         etl::span<const uint8_t>)>;
  using ProcessRunAsyncHandler = etl::delegate<void(int16_t)>;
#endif

  explicit BridgeClass(HardwareSerial& serial);
  explicit BridgeClass(Stream& stream);

  // [SIL-2] Observable Management
  using etl::observable<BridgeObserver, BRIDGE_MAX_OBSERVERS>::add_observer;
  using etl::observable<BridgeObserver, BRIDGE_MAX_OBSERVERS>::remove_observer;

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

  // [SIL-2] FSM state accessors
  bool isUnsynchronized() const { return _fsm.isUnsynchronized(); }
  bool isIdle() const { return _fsm.isIdle(); }
  bool isAwaitingAck() const { return _fsm.isAwaitingAck(); }
  bool isFault() const { return _fsm.isFault(); }
  bridge::fsm::StateId getStateId() const {
    return static_cast<bridge::fsm::StateId>(_fsm.get_state_id());
  }

  // [SIL-2] ETL Timer Callbacks
  void _onAckTimeout();
  void _onBaudrateChange();
  void _onRxDedupe();
  void _onStartupStabilized();

  // Events
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

  // Internal / Lower Level
  bool sendFrame(rpc::CommandId command_id,
                 etl::span<const uint8_t> payload = etl::span<const uint8_t>());
  bool sendFrame(rpc::StatusCode status_code,
                 etl::span<const uint8_t> payload = etl::span<const uint8_t>());

  // [SIL-2] Consolidated String Command Helpers (DRY)
  // These helpers automatically emit STATUS_OVERFLOW if constraints are violated.
  bool sendStringCommand(rpc::CommandId command_id, etl::string_view str,
                         size_t max_len);
  bool sendKeyValCommand(rpc::CommandId command_id, etl::string_view key,
                         size_t max_key, etl::string_view val, size_t max_val);

  // [SIL-2] Generic Value Sender (Boilerplate reduction)
  template <typename T>
  bool sendValue(rpc::CommandId command_id, T value) {
    etl::array<uint8_t, sizeof(T)> payload;
    if (sizeof(T) == 1) {
      payload[0] = static_cast<uint8_t>(value);
    } else if (sizeof(T) == 2) {
      rpc::write_u16_be(etl::span<uint8_t>(payload.data(), 2),
                        static_cast<uint16_t>(value));
    } else if (sizeof(T) == 4) {
      rpc::write_u32_be(etl::span<uint8_t>(payload.data(), 4),
                        static_cast<uint32_t>(value));
    } else {
      // Static protection for unsupported types
      static_assert(sizeof(T) <= 4, "Unsupported value size for sendValue");
      return false;
    }
    return sendFrame(command_id,
                     etl::span<const uint8_t>(payload.data(), payload.size()));
  }

  inline void flushStream() { _stream.flush(); }
  void enterSafeState();  // [SIL-2] Force system into fail-safe state
  void emitStatus(rpc::StatusCode status_code, etl::string_view message = {});
  void emitStatus(rpc::StatusCode status_code,
                  const __FlashStringHelper* message);

  // [SIL-2] Large Payload Support
  bool sendChunkyFrame(rpc::CommandId command_id,
                       etl::span<const uint8_t> header,
                       etl::span<const uint8_t> data);

 private:
  Stream& _stream;
  HardwareSerial* _hardware_serial;

  etl::vector<uint8_t, 32> _shared_secret;

  // Protocol Engine
  // [SIL-2] Streaming COBS Decoder Zero-Copy State
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
  etl::optional<rpc::FrameError>
      _last_parse_error;  // [SIL-2] Type-safe error tracking

  etl::random_xorshift
      _rng;  // [SIL-2] Deterministic Random Generator for Nonces
  // State
  uint16_t _last_command_id;
  uint8_t _retry_count;

  uint32_t _pending_baudrate;

  // Incoming deduplication (idempotency for retries)
  struct RxHistory {
    uint32_t crc;
    uint32_t timestamp;
  };
  etl::circular_buffer<RxHistory, BRIDGE_RX_HISTORY_SIZE> _rx_history;
  volatile uint8_t _consecutive_crc_errors;

  // Config
  uint16_t _ack_timeout_ms;
  uint8_t _ack_retry_limit;
  uint32_t _response_timeout_ms;

  // Handlers
  CommandHandler _command_handler;
  DigitalReadHandler _digital_read_handler;
  AnalogReadHandler _analog_read_handler;
  GetFreeMemoryHandler _get_free_memory_handler;
  StatusHandler _status_handler;

  // Pending Queues
  struct PendingTxFrame {
    uint16_t command_id;
    uint16_t payload_length;
    etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload;
  };
  // [SIL-2] Use queue adapter over deque for strict FIFO semantics
  etl::queue<PendingTxFrame, BRIDGE_MAX_PENDING_TX_FRAMES> _pending_tx_queue;

  // [SIL-2] ETL FSM replaces manual state tracking
  bridge::fsm::BridgeFsm _fsm;

  // [RAM-OPT] SimpleTimer replaces etl::callback_timer<4> + 4 delegates
  // Saves ~116-135 bytes RAM on AVR (etl::callback_timer overhead eliminated)
  bridge::scheduler::SimpleTimer _timers;
  uint32_t _last_tick_millis;

  volatile bool _startup_stabilizing;

  // [RAM-OPT] ICommandHandler is a pure virtual interface (no ETL overhead)
  // See router/command_router.h for rationale

  // [SIL-2] ICommandHandler interface implementation
  void onStatusCommand(const bridge::router::CommandContext& ctx) override;
  void onSystemCommand(const bridge::router::CommandContext& ctx) override;
  void onGpioCommand(const bridge::router::CommandContext& ctx) override;
  void onConsoleCommand(const bridge::router::CommandContext& ctx) override;
  void onDataStoreCommand(const bridge::router::CommandContext& ctx) override;
  void onMailboxCommand(const bridge::router::CommandContext& ctx) override;
  void onFileSystemCommand(const bridge::router::CommandContext& ctx) override;
  void onProcessCommand(const bridge::router::CommandContext& ctx) override;
  void onUnknownCommand(const bridge::router::CommandContext& ctx) override;

  // [SIL-2] Individual Command Handlers for O(1) Dispatch
  // Status
  void _handleStatusAck(const bridge::router::CommandContext& ctx);
  void _handleStatusMalformed(const bridge::router::CommandContext& ctx);

  // System
  void _handleGetVersion(const bridge::router::CommandContext& ctx);
  void _handleGetFreeMemory(const bridge::router::CommandContext& ctx);
  void _handleGetCapabilities(const bridge::router::CommandContext& ctx);
  void _handleSetBaudrate(const bridge::router::CommandContext& ctx);
  void _handleLinkSync(const bridge::router::CommandContext& ctx);
  void _handleLinkReset(const bridge::router::CommandContext& ctx);

  // GPIO
  void _handleSetPinMode(const bridge::router::CommandContext& ctx);
  void _handleDigitalWrite(const bridge::router::CommandContext& ctx);
  void _handleAnalogWrite(const bridge::router::CommandContext& ctx);
  void _handleDigitalRead(const bridge::router::CommandContext& ctx);
  void _handleAnalogRead(const bridge::router::CommandContext& ctx);

  template <typename TResp, typename TFunc, typename TValid, typename... Args>
  void _handlePinRead(const bridge::router::CommandContext& ctx, rpc::CommandId resp_cmd, TValid valid_func, TFunc read_func, Args&&... args) {
    _withPayload<rpc::payload::PinRead>(
        ctx, [this, resp_cmd, valid_func, read_func, &args...](const rpc::payload::PinRead& msg) {
          if (valid_func(msg.pin)) {
            _sendResponse<TResp>(resp_cmd, read_func(msg.pin, etl::forward<Args>(args)...));
          } else {
            (void)sendFrame(rpc::StatusCode::STATUS_MALFORMED);
          }
        });
  }

  // Console
  void _handleConsoleWrite(const bridge::router::CommandContext& ctx);

  // DataStore
  void _handleDatastoreGetResp(const bridge::router::CommandContext& ctx);

  // Mailbox
  void _handleMailboxPush(const bridge::router::CommandContext& ctx);
  void _handleMailboxReadResp(const bridge::router::CommandContext& ctx);
  void _handleMailboxAvailableResp(const bridge::router::CommandContext& ctx);

  // FileSystem
  void _handleFileWrite(const bridge::router::CommandContext& ctx);
  void _handleFileReadResp(const bridge::router::CommandContext& ctx);

  // Process
  void _handleProcessRunAsyncResp(const bridge::router::CommandContext& ctx);
  void _handleProcessPollResp(const bridge::router::CommandContext& ctx);

  bool _isRecentDuplicateRx(const rpc::Frame& frame) const;
  void _markRxProcessed(const rpc::Frame& frame);
  bool _isHandshakeCommand(uint16_t command_id) const;

  void dispatch(const rpc::Frame& frame);
  bool _sendFrame(uint16_t command_id, etl::span<const uint8_t> payload);
  void _sendRawFrame(uint16_t command_id, etl::span<const uint8_t> payload);

  // [SIL-2] Private Refactoring Helpers
  void _processIncomingByte(uint8_t byte);
  void _handleReceivedFrame();
  etl::expected<void, rpc::FrameError> _decompressFrame(
      const rpc::Frame& original, rpc::Frame& effective);
  bool _isSecurityCheckPassed(uint16_t command_id) const;

  // [SIL-2] Unified Jump Table Dispatch Template
  template <typename THandler, size_t N>
  void _dispatchJumpTable(const bridge::router::CommandContext& ctx, uint16_t min_id, const THandler (&handlers)[N], uint8_t stride = 1) {
    if (ctx.raw_command < min_id) return;
    const uint16_t index = (ctx.raw_command - min_id) / stride;
    if (index < N && handlers[index]) {
      (this->*handlers[index])(ctx);
    }
  }

  // [SIL-2] DRY Pin Setter Template
  template <typename TPacket, typename TFunc>
  void _handlePinSetter(const bridge::router::CommandContext& ctx, TFunc func) {
    _withPayloadAck<TPacket>(ctx, [this, func](const TPacket& msg) {
      if (bridge::hal::isValidPin(msg.pin)) func(msg);
    });
  }

  // [SIL-2] Safe Atomic Buffer Push Template
  template <typename TBuffer>
  static void safePush(TBuffer& buffer, etl::span<const uint8_t> data) {
    if (data.empty()) return;
    BRIDGE_ATOMIC_BLOCK {
      const size_t space = buffer.capacity() - buffer.size();
      const size_t to_copy = etl::min(data.size(), space);
      buffer.push(data.begin(), data.begin() + to_copy);
    }
  }

  // [SIL-2] Unified Response Dispatch Template
  template <typename TPacket, typename TDelegate, typename TFunc>
  void _dispatchResponse(const bridge::router::CommandContext& ctx, TDelegate& delegate, TFunc func) {
    _withPayload<TPacket>(ctx, [this, &delegate, func](const TPacket& msg) {
      if (delegate.is_valid()) func(delegate, msg);
    });
  }

  // [SIL-2] DRY Command Helpers with Lambdas
  template <typename F>
  void _withAck(const bridge::router::CommandContext& ctx, F handler) {
    if (ctx.is_duplicate) {
      _sendAckAndFlush(ctx.raw_command);
    } else {
      handler();
      _markRxProcessed(*ctx.frame);
      _sendAck(ctx.raw_command);
    }
  }

  template <typename F>
  void _withResponse(const bridge::router::CommandContext& ctx, F handler) {
    handler();
    if (!ctx.is_duplicate) {
      _markRxProcessed(*ctx.frame);
    }
  }

  template <typename T, typename F>
  void _withPayloadResponse(const bridge::router::CommandContext& ctx,
                            F handler) {
    auto msg = rpc::Payload::parse<T>(*ctx.frame);
    if (msg) {
      handler(*msg);
      if (!ctx.is_duplicate) {
        _markRxProcessed(*ctx.frame);
      }
    }
  }

  template <typename T, typename F>
  void _withPayloadAck(const bridge::router::CommandContext& ctx, F handler) {
    _withAck(ctx, [&]() {
      auto msg = rpc::Payload::parse<T>(*ctx.frame);
      if (msg) handler(*msg);
    });
  }

  template <typename T, typename F>
  void _withPayload(const bridge::router::CommandContext& ctx, F handler) {
    if (ctx.is_duplicate) return;
    auto msg = rpc::Payload::parse<T>(*ctx.frame);
    if (msg) {
      handler(*msg);
      _markRxProcessed(*ctx.frame);
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
  void _sendAck(uint16_t command_id);  // Send ACK without flush
  void _sendAckAndFlush(
      uint16_t command_id);  // Encapsulates ACK + flush sequence
  void _doEmitStatus(rpc::StatusCode status_code,
                     etl::span<const uint8_t> payload);
  void _computeHandshakeTag(etl::span<const uint8_t> nonce, uint8_t* out_tag);
  void _applyTimingConfig(etl::span<const uint8_t> payload);

  void _flushPendingTxQueue();
  void _clearPendingTxQueue();
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

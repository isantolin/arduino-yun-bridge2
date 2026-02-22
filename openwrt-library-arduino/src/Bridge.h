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
 * The use of Standard Template Library (STL) headers (e.g., <vector>, <string>, <map>)
 * is STRICTLY PROHIBITED to prevent heap fragmentation and non-deterministic behavior.
 * Reviewers must reject any PR including these headers.
 * 
 * @see docs/PROTOCOL.md for protocol specification
 * @see tools/protocol/spec.toml for machine-readable contract
 */
#ifndef BRIDGE_H
#define BRIDGE_H

// [SIL-2] ETL Configuration MUST be first to ensure consistent profile (e.g. no CRC tables)
#include "etl_profile.h"

// [SIL-2] Centralized configuration for class layout consistency (ODR)
#include "config/bridge_config.h"

#include <Arduino.h>
#include <Stream.h>
#include <PacketSerial.h>
#include "etl/algorithm.h"

// [SIL-2] ISR Safety: Atomic Blocks
#if defined(ARDUINO_ARCH_AVR)
  #include <util/atomic.h>
  #define BRIDGE_ATOMIC_BLOCK ATOMIC_BLOCK(ATOMIC_RESTORESTATE)
  // [Compatibility] Polyfill for boards missing SERIAL_PORT_USBVIRTUAL (e.g. Mega 2560)
  #ifndef SERIAL_PORT_USBVIRTUAL
    #define SERIAL_PORT_USBVIRTUAL Serial
  #endif
#else
  // Fallback for non-AVR architectures: use interrupts() / noInterrupts()
  // This is a simplified version of ATOMIC_BLOCK for portability.
  struct BridgeAtomicGuard {
    BridgeAtomicGuard() { 
      noInterrupts(); 
      asm volatile("" ::: "memory");
    }
    ~BridgeAtomicGuard() { 
      asm volatile("" ::: "memory");
      interrupts(); 
    }
  };
  #define BRIDGE_ATOMIC_BLOCK for (int _guard_active = 1; _guard_active; _guard_active = 0) \
                               for (BridgeAtomicGuard _guard; _guard_active; _guard_active = 0)
#endif

#include "config/bridge_config.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "protocol/PacketBuilder.h"

#undef min
#undef max
#include "etl/array.h"
#include "etl/queue.h"
#include "etl/circular_buffer.h"
#include "etl/vector.h"
#include "etl/delegate.h"
#include "etl/optional.h"
#include "etl/string_view.h"
#include "etl/span.h"
#include "etl/observer.h"

// [SIL-2] Lightweight FSM + Scheduler for deterministic state transitions
#include "fsm/bridge_fsm.h"

// [SIL-2] ETL Message Router for command dispatch
#include "router/command_router.h"

// [SIL-2] Static Constraints
static_assert(rpc::MAX_PAYLOAD_SIZE <= 1024, "Payload size exceeds safety limits for small RAM targets");

#if defined(ARDUINO_ARCH_AVR)
extern "C" char __heap_start;
extern "C" char* __brkval;
#endif

/**
 * @brief Get free RAM (AVR specific).
 * @return Bytes free or 0 on non-AVR.
 */
inline uint16_t getFreeMemory() {
#if defined(ARDUINO_ARCH_AVR)
  char stack_top;
  char* heap_end = __brkval ? __brkval : &__heap_start;
  intptr_t free_bytes = &stack_top - heap_end;
  if (free_bytes < 0) {
    free_bytes = 0;
  }
  if (free_bytes > UINT16_MAX) {
    free_bytes = UINT16_MAX;
  }
  return static_cast<uint16_t>(free_bytes);
#else
  return 0;
#endif
}

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
constexpr uint8_t kDefaultFirmwareVersionMinor = 5;
#endif

#ifndef BRIDGE_MAX_OBSERVERS
#define BRIDGE_MAX_OBSERVERS 4
#endif

// --- Subsystem Enablement (RAM Optimization) ---
// Note: Macros are now centralized in config/bridge_config.h

// [SIL-2] Resource Allocation Tuning
// Note: BRIDGE_MAX_PENDING_TX_FRAMES moved to bridge_config.h

using BridgePacketSerial = PacketSerial;

#if defined(BRIDGE_HOST_TEST)
namespace bridge {
namespace test {
  class TestAccessor;
  class ConsoleTestAccessor;
  class DataStoreTestAccessor;
  class ProcessTestAccessor;
}
}
#endif

// [SIL-2] Observer Interface for System Events
struct BridgeObserver {
  virtual ~BridgeObserver() = default;
  virtual void onBridgeSynchronized() {}
  virtual void onBridgeLost() {}
  virtual void onBridgeError(rpc::StatusCode code) { (void)code; }
};

class BridgeClass : public bridge::router::ICommandHandler {
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
  // Callbacks - [SIL-2] Using etl::delegate for safer, object-oriented callbacks
  using CommandHandler = etl::delegate<void(const rpc::Frame&)>;
  using DigitalReadHandler = etl::delegate<void(uint8_t)>;
  using AnalogReadHandler = etl::delegate<void(uint16_t)>;
  using GetFreeMemoryHandler = etl::delegate<void(uint16_t)>;
  using StatusHandler = etl::delegate<void(rpc::StatusCode, const uint8_t*, uint16_t)>;

  #if BRIDGE_ENABLE_DATASTORE
  using DataStoreGetHandler = etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>;
  #endif

  #if BRIDGE_ENABLE_MAILBOX
  using MailboxHandler = etl::delegate<void(const uint8_t*, uint16_t)>;
  using MailboxAvailableHandler = etl::delegate<void(uint16_t)>;
  #endif

  #if BRIDGE_ENABLE_FILESYSTEM
  using FileSystemReadHandler = etl::delegate<void(const uint8_t*, uint16_t)>;
  #endif

  #if BRIDGE_ENABLE_PROCESS
  using ProcessRunHandler = etl::delegate<void(rpc::StatusCode, const uint8_t*, uint16_t, const uint8_t*, uint16_t)>;
  using ProcessPollHandler = etl::delegate<void(rpc::StatusCode, uint8_t, const uint8_t*, uint16_t, const uint8_t*, uint16_t)>;
  using ProcessRunAsyncHandler = etl::delegate<void(int16_t)>;
  #endif
  
  explicit BridgeClass(HardwareSerial& serial);
  explicit BridgeClass(Stream& stream);

  // [SIL-2] Manual Observer Management (etl::observable is too strict for lambdas)
  void add_observer(BridgeObserver& obs) {
    if (!_observers.full()) _observers.push_back(&obs);
  }
  
  void remove_observer(BridgeObserver& obs) {
    const auto it = etl::find(_observers.begin(), _observers.end(), &obs);
    if (it != _observers.end()) {
      _observers.erase(it);
    }
  }

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
  bridge::fsm::StateId getStateId() const { return static_cast<bridge::fsm::StateId>(_fsm.get_state_id()); }

  // [SIL-2] ETL Timer Callbacks
  void _onAckTimeout();
  void _onBaudrateChange();
  void _onRxDedupe();
  void _onStartupStabilized();

  // Events
  inline void onCommand(CommandHandler handler) { _command_handler = handler; }
  inline void onDigitalReadResponse(DigitalReadHandler handler) { _digital_read_handler = handler; }
  inline void onAnalogReadResponse(AnalogReadHandler handler) { _analog_read_handler = handler; }
  inline void onGetFreeMemoryResponse(GetFreeMemoryHandler handler) { _get_free_memory_handler = handler; }
  inline void onStatus(StatusHandler handler) { _status_handler = handler; }

  #if BRIDGE_ENABLE_DATASTORE
  inline void onDataStoreGetResponse(DataStoreGetHandler handler) { _datastore_get_handler = handler; }
  #endif

  #if BRIDGE_ENABLE_MAILBOX
  inline void onMailboxMessage(MailboxHandler handler) { _mailbox_handler = handler; }
  inline void onMailboxAvailableResponse(MailboxAvailableHandler handler) { _mailbox_available_handler = handler; }
  #endif

  #if BRIDGE_ENABLE_FILESYSTEM
  inline void onFileSystemReadResponse(FileSystemReadHandler handler) { _file_system_read_handler = handler; }
  #endif

  #if BRIDGE_ENABLE_PROCESS
  inline void onProcessRunResponse(ProcessRunHandler handler) { _process_run_handler = handler; }
  inline void onProcessPollResponse(ProcessPollHandler handler) { _process_poll_handler = handler; }
  inline void onProcessRunAsyncResponse(ProcessRunAsyncHandler handler) { _process_run_async_handler = handler; }
  #endif

  // Internal / Lower Level
  bool sendFrame(rpc::CommandId command_id, const uint8_t* payload = nullptr, size_t length = 0);
  bool sendFrame(rpc::StatusCode status_code, const uint8_t* payload = nullptr, size_t length = 0);
  
  // [SIL-2] Consolidated String Command Helpers (DRY)
  bool sendStringCommand(rpc::CommandId command_id, etl::string_view str, size_t max_len);
  bool sendKeyValCommand(rpc::CommandId command_id, etl::string_view key, size_t max_key, etl::string_view val, size_t max_val);

  void flushStream();
  void enterSafeState(); // [SIL-2] Force system into fail-safe state
  void _emitStatus(rpc::StatusCode status_code, etl::string_view message = {});
  void _emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message);
  
  // [SIL-2] Large Payload Support (Application-Level Chunking)
  // Sends data larger than MAX_PAYLOAD_SIZE by fragmenting it into multiple frames.
  // Handles flow control (back-pressure) to ensure delivery on constrained queues.
  bool sendChunkyFrame(rpc::CommandId command_id, 
                       const uint8_t* header, size_t header_len, 
                       const uint8_t* data, size_t data_len);

  // Internal Callback Trampoline for PacketSerial
  static void onPacketReceived(const uint8_t* buffer, size_t size);

 protected:
  // [SIL-2] Internal notification helper
  template<typename F>
  void notify_observers(F f) {
    for (auto* obs : _observers) {
      if (obs) f(*obs);
    }
  }

 private:
  Stream& _stream;
  HardwareSerial* _hardware_serial;
  BridgePacketSerial _packetSerial;
  
  etl::vector<uint8_t, 32> _shared_secret;

  // [SIL-2] Observers container
  etl::vector<BridgeObserver*, BRIDGE_MAX_OBSERVERS> _observers;

  // Protocol Engine
  rpc::Frame* _target_frame;
  volatile bool _frame_received;
  rpc::FrameParser _parser;
  rpc::Frame _rx_frame;
  etl::optional<rpc::FrameError> _last_parse_error;  // [SIL-2] Type-safe error tracking
  // State
  uint16_t _last_command_id;
  uint8_t _retry_count;
  
  uint32_t _pending_baudrate;

  // Incoming deduplication (idempotency for retries)
  uint32_t _last_rx_crc;
  unsigned long _last_rx_crc_millis;
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

  #if BRIDGE_ENABLE_DATASTORE
  DataStoreGetHandler _datastore_get_handler;
  #endif

  #if BRIDGE_ENABLE_MAILBOX
  etl::delegate<void(const uint8_t*, uint16_t)> _mailbox_handler;
  etl::delegate<void(uint16_t)> _mailbox_available_handler;
  #endif

  #if BRIDGE_ENABLE_FILESYSTEM
  etl::delegate<void(const uint8_t*, uint16_t)> _file_system_read_handler;
  #endif

  #if BRIDGE_ENABLE_PROCESS
  etl::delegate<void(rpc::StatusCode, const uint8_t*, uint16_t, const uint8_t*, uint16_t)> _process_run_handler;
  etl::delegate<void(rpc::StatusCode, uint8_t, const uint8_t*, uint16_t, const uint8_t*, uint16_t)> _process_poll_handler;
  etl::delegate<void(int16_t)> _process_run_async_handler;
  #endif
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

  // [SIL-2] ETL Timer Service (Native)
  bridge::scheduler::BridgeTimerService _timer_service;
  unsigned long _last_tick_millis;
  
  // [SIL-2] Timer callback delegates - must persist for object lifetime
  // ETL callback_timer stores pointer to delegate, so they cannot be stack locals
  etl::delegate<void()> _cb_ack_timeout;
  etl::delegate<void()> _cb_rx_dedupe;
  etl::delegate<void()> _cb_baudrate_change;
  etl::delegate<void()> _cb_startup_stabilized;

  // [SIL-2] Non-blocking startup stabilization flag.
  // Rationale: marked `volatile` because it is written from a timer/callback
  // context (startup-stabilization timer expiry) and read from the main
  // loop context (BridgeClass::update / FSM transitions).  Without the
  // volatile qualifier the compiler may cache the value in a register and
  // the main loop would never observe the flag change, violating the
  // single-writer / single-reader safety contract required at SIL-2.
  // No additional synchronisation primitive is needed: the variable is a
  // single bool (atomic on all supported AVR / ARM-M targets) with exactly
  // one writer (timer callback) and one reader (main loop).
  volatile bool _startup_stabilizing;

  // [SIL-2] ETL Message Router for flattened command dispatch
  bridge::router::CommandRouter _command_router;

  // Methods
  void _handleSystemCommand(const rpc::Frame& frame);
  void _handleGpioCommand(const rpc::Frame& frame);

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

  bool _isRecentDuplicateRx(const rpc::Frame& frame) const;
  void _markRxProcessed(const rpc::Frame& frame);
  bool _isHandshakeCommand(uint16_t command_id) const;

  void dispatch(const rpc::Frame& frame);
  bool _sendFrame(uint16_t command_id, const uint8_t* payload, size_t length);
  bool _requiresAck(uint16_t command_id) const;
  void _retransmitLastFrame();
  void _handleAck(uint16_t command_id);
  void _handleMalformed(uint16_t command_id);
  void _sendAck(uint16_t command_id);          // Send ACK without flush
  void _sendAckAndFlush(uint16_t command_id);  // Encapsulates ACK + flush sequence
  template <typename Handler>
  void _handleDedupAck(const bridge::router::CommandContext& ctx, Handler handler, bool flush_on_duplicate) {
    if (ctx.is_duplicate) {
      if (flush_on_duplicate) {
        _sendAckAndFlush(ctx.raw_command);
      } else {
        _sendAck(ctx.raw_command);
      }
      return;
    }
    handler();
    _markRxProcessed(*ctx.frame);
    _sendAck(ctx.raw_command);
  }
  void _doEmitStatus(rpc::StatusCode status_code, const uint8_t* payload, uint16_t length);
  void _computeHandshakeTag(const uint8_t* nonce, size_t nonce_len, uint8_t* out_tag);
  void _applyTimingConfig(const uint8_t* payload, size_t length);

  void _flushPendingTxQueue();
  void _clearPendingTxQueue();
  void _clearAckState();
};

extern BridgeClass Bridge;

class ConsoleClass : public Stream {
  #if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::ConsoleTestAccessor;
  #endif
 public:
  ConsoleClass();
  void begin();
  
  size_t write(uint8_t c) override;
  size_t write(const uint8_t *buffer, size_t size) override;
  
  void _push(etl::span<const uint8_t> data);
  
  int available() override;
  int read() override;
  int peek() override;
  void flush() override;

 private:
  bool _begun;
  bool _xoff_sent;
  
  // [SIL-2] Use ETL containers for safe buffer management
  etl::circular_buffer<uint8_t, BRIDGE_CONSOLE_RX_BUFFER_SIZE> _rx_buffer;
  etl::vector<uint8_t, BRIDGE_CONSOLE_TX_BUFFER_SIZE> _tx_buffer;
};
extern ConsoleClass Console;

#if BRIDGE_ENABLE_DATASTORE
#include "etl/string.h"
class DataStoreClass {
  friend class BridgeClass;
  #if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::DataStoreTestAccessor;
  #endif
 public:
  using DataStoreGetHandler = BridgeClass::DataStoreGetHandler;

  DataStoreClass();
  void reset();
  void put(etl::string_view key, etl::string_view value);
  void requestGet(etl::string_view key);
  inline void onDataStoreGetResponse(DataStoreGetHandler handler) {
    Bridge.onDataStoreGetResponse(handler);
  }

 private:
  bool _trackPendingDatastoreKey(etl::string_view key);
  const char* _popPendingDatastoreKey();

  // [SIL-2] Use queue adapter for strict FIFO semantics
  etl::queue<etl::string<rpc::RPC_MAX_DATASTORE_KEY_LENGTH>, BRIDGE_MAX_PENDING_DATASTORE> _pending_datastore_keys;
  etl::string<rpc::RPC_MAX_DATASTORE_KEY_LENGTH> _last_datastore_key;
};
extern DataStoreClass DataStore;
#endif

#if BRIDGE_ENABLE_MAILBOX
class MailboxClass {
 public:
  using MailboxHandler = etl::delegate<void(const uint8_t*, uint16_t)>;
  using MailboxAvailableHandler = etl::delegate<void(uint16_t)>;

  MailboxClass() {}
  
  // [SIL-2] Inlined for optimization (-Os)
  inline void send(etl::string_view message) {
    if (message.empty()) return;
    send(reinterpret_cast<const uint8_t*>(message.data()), message.length());
  }

  inline void send(const uint8_t* data, size_t length) {
    if (!data || length == 0) return;

    // [SIL-2] Large Message Support
    // We remove the explicit 2-byte length prefix that was present in the old implementation
    // because the Frame Header already contains the payload length.
    // This allows us to use standard chunking for messages > 64 bytes.
    // Note: The receiving side (Python) will receive these as separate messages.
    // Reassembly is up to the application layer if needed.
    Bridge.sendChunkyFrame(rpc::CommandId::CMD_MAILBOX_PUSH, 
                           nullptr, 0, 
                           data, length);
  }

  inline void requestRead() {
    (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_READ);
  }

  inline void requestAvailable() {
    (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
  }

  inline void onMailboxMessage(MailboxHandler handler) {
    Bridge.onMailboxMessage(handler);
  }
  inline void onMailboxAvailableResponse(MailboxAvailableHandler handler) {
    Bridge.onMailboxAvailableResponse(handler);
  }
};
extern MailboxClass Mailbox;
#endif

#if BRIDGE_ENABLE_FILESYSTEM
class FileSystemClass {
 public:
  using FileSystemReadHandler = etl::delegate<void(const uint8_t*, uint16_t)>;

  FileSystemClass() {}

  inline void write(etl::string_view filePath, const uint8_t* data, size_t length) {
    if (filePath.empty() || !data) return;
    
    if (filePath.length() > rpc::RPC_MAX_FILEPATH_LENGTH - 1) {
      Bridge._emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
      return;
    }

    etl::vector<uint8_t, rpc::RPC_MAX_FILEPATH_LENGTH + 1> header;
    rpc::PacketBuilder(header).add_pascal_string(filePath);

    Bridge.sendChunkyFrame(rpc::CommandId::CMD_FILE_WRITE, 
                           header.data(), header.size(), 
                           data, length);
  }
  
  // [SIL-2] Inlined for optimization (-Os)
  inline void remove(etl::string_view filePath) {
    if (filePath.empty()) return;
    if (!Bridge.sendStringCommand(rpc::CommandId::CMD_FILE_REMOVE, 
                                  filePath, rpc::RPC_MAX_FILEPATH_LENGTH - 1)) {
      Bridge._emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
    }
  }

  inline void read(etl::string_view filePath) {
    if (filePath.empty()) return;
    if (!Bridge.sendStringCommand(rpc::CommandId::CMD_FILE_READ, 
                                  filePath, rpc::RPC_MAX_FILEPATH_LENGTH - 1)) {
      Bridge._emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
    }
  }

  inline void onFileSystemReadResponse(FileSystemReadHandler handler) {
    Bridge.onFileSystemReadResponse(handler);
  }
};
extern FileSystemClass FileSystem;
#endif

#if BRIDGE_ENABLE_PROCESS
class ProcessClass {
  #if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::ProcessTestAccessor;
  #endif
 public:
  using ProcessRunHandler = etl::delegate<void(rpc::StatusCode, const uint8_t*, uint16_t, const uint8_t*, uint16_t)>;
  using ProcessPollHandler = etl::delegate<void(rpc::StatusCode, uint8_t, const uint8_t*, uint16_t, const uint8_t*, uint16_t)>;
  using ProcessRunAsyncHandler = etl::delegate<void(int16_t)>;

  ProcessClass();
  void reset();
  void run(etl::string_view command);
  void runAsync(etl::string_view command);
  void poll(int16_t pid);
  void kill(int16_t pid);

  inline void onProcessRunResponse(ProcessRunHandler handler) {
    Bridge.onProcessRunResponse(handler);
  }
  inline void onProcessPollResponse(ProcessPollHandler handler) {
    Bridge.onProcessPollResponse(handler);
  }
  inline void onProcessRunAsyncResponse(ProcessRunAsyncHandler handler) {
    Bridge.onProcessRunAsyncResponse(handler);
  }

 private:
  friend class BridgeClass;
  bool _pushPendingProcessPid(uint16_t pid);
  uint16_t _popPendingProcessPid();

  // [SIL-2] Use circular buffer for safe PID tracking
  etl::circular_buffer<uint16_t, BRIDGE_MAX_PENDING_PROCESS_POLLS> _pending_process_pids;
};
extern ProcessClass Process;
#endif

#endif

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

#include <Arduino.h>
#include <Stream.h>
#include <PacketSerial.h>

// [SIL-2] ISR Safety: Atomic Blocks
#if defined(ARDUINO_ARCH_AVR)
  #include <util/atomic.h>
  #define BRIDGE_ATOMIC_BLOCK ATOMIC_BLOCK(ATOMIC_RESTORESTATE)
#else
  // Fallback for non-AVR architectures: use interrupts() / noInterrupts()
  // This is a simplified version of ATOMIC_BLOCK for portability.
  struct BridgeAtomicGuard {
    BridgeAtomicGuard() { noInterrupts(); }
    ~BridgeAtomicGuard() { interrupts(); }
  };
  #define BRIDGE_ATOMIC_BLOCK for (int _guard_active = 1; _guard_active; _guard_active = 0) \
                               for (BridgeAtomicGuard _guard; _guard_active; _guard_active = 0)
#endif

#include "config/bridge_config.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"

#undef min
#undef max
#include "etl/array.h"
#include "etl/queue.h"
#include "etl/circular_buffer.h"
#include "etl/vector.h"
#include "etl/delegate.h"
#include "etl/optional.h"
#include "etl/string_view.h"

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

// --- Subsystem Enablement (RAM Optimization) ---

#ifndef BRIDGE_ENABLE_DATASTORE
#define BRIDGE_ENABLE_DATASTORE 1
#endif

#ifndef BRIDGE_ENABLE_FILESYSTEM
#define BRIDGE_ENABLE_FILESYSTEM 1
#endif

#ifndef BRIDGE_ENABLE_MAILBOX
#define BRIDGE_ENABLE_MAILBOX 1
#endif

#ifndef BRIDGE_ENABLE_PROCESS
#define BRIDGE_ENABLE_PROCESS 1
#endif

// [SIL-2] Resource Allocation Tuning
// On memory constrained AVR (Mega/Yun), we limit the pending queue to 2 frames (1 Active + 1 Pending).
// Previously this was 1, but we merged the active frame buffer into the queue.
#if defined(ARDUINO_ARCH_AVR)
  #ifndef BRIDGE_MAX_PENDING_TX_FRAMES
    #define BRIDGE_MAX_PENDING_TX_FRAMES 2
  #endif
#else
  #ifndef BRIDGE_MAX_PENDING_TX_FRAMES
    #define BRIDGE_MAX_PENDING_TX_FRAMES (rpc::RPC_MAX_PENDING_TX_FRAMES + 1)
  #endif
#endif

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
  // Callbacks
  using CommandHandler = void (*)(const rpc::Frame&);
  using DigitalReadHandler = void (*)(uint8_t);
  using AnalogReadHandler = void (*)(uint16_t);
  using GetFreeMemoryHandler = void (*)(uint16_t);
  using StatusHandler = void (*)(rpc::StatusCode, const uint8_t*, uint16_t);

  explicit BridgeClass(HardwareSerial& serial);
  explicit BridgeClass(Stream& stream);

  void begin(
      unsigned long baudrate =
#ifdef BRIDGE_BAUDRATE
          BRIDGE_BAUDRATE
#else
          rpc::RPC_DEFAULT_BAUDRATE
#endif
      ,
             const char* secret = nullptr, size_t secret_len = 0);
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

  // Internal / Lower Level
  bool sendFrame(rpc::CommandId command_id, const uint8_t* payload = nullptr, size_t length = 0);
  bool sendFrame(rpc::StatusCode status_code, const uint8_t* payload = nullptr, size_t length = 0);
  
  // [SIL-2] Consolidated String Command Helpers (DRY)
  bool sendStringCommand(rpc::CommandId command_id, etl::string_view str, size_t max_len);
  bool sendKeyValCommand(rpc::CommandId command_id, etl::string_view key, size_t max_key, etl::string_view val, size_t max_val);

  void flushStream();
  void enterSafeState(); // [SIL-2] Force system into fail-safe state
  void _emitStatus(rpc::StatusCode status_code, const char* message = nullptr);
  void _emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message);
  
  // [SIL-2] Large Payload Support (Application-Level Chunking)
  // Sends data larger than MAX_PAYLOAD_SIZE by fragmenting it into multiple frames.
  // Handles flow control (back-pressure) to ensure delivery on constrained queues.
  void sendChunkyFrame(rpc::CommandId command_id, 
                       const uint8_t* header, size_t header_len, 
                       const uint8_t* data, size_t data_len);

  // Internal Callback Trampoline for PacketSerial
  static void onPacketReceived(const uint8_t* buffer, size_t size);

 private:
  Stream& _stream;
  HardwareSerial* _hardware_serial;
  BridgePacketSerial _packetSerial;
  
  etl::vector<uint8_t, 32> _shared_secret;

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
  void _handleConsoleCommand(const rpc::Frame& frame);

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
  
  void _push(const uint8_t* data, size_t length);
  
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
  #if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::DataStoreTestAccessor;
  #endif
 public:
  using DataStoreGetHandler = void (*)(const char*, const uint8_t*, uint16_t);

  DataStoreClass();
  void put(const char* key, const char* value);
  void requestGet(const char* key);
  void handleResponse(const rpc::Frame& frame);
  inline void onDataStoreGetResponse(DataStoreGetHandler handler) {
    _datastore_get_handler = handler;
  }

 private:
  bool _trackPendingDatastoreKey(const char* key);
  const char* _popPendingDatastoreKey();

  // [SIL-2] Use queue adapter for strict FIFO semantics
  etl::queue<etl::string<rpc::RPC_MAX_DATASTORE_KEY_LENGTH>, BRIDGE_MAX_PENDING_DATASTORE> _pending_datastore_keys;
  etl::string<rpc::RPC_MAX_DATASTORE_KEY_LENGTH> _last_datastore_key;
  DataStoreGetHandler _datastore_get_handler;
};
extern DataStoreClass DataStore;
#endif

#if BRIDGE_ENABLE_MAILBOX
class MailboxClass {
 public:
  using MailboxHandler = void (*)(const uint8_t*, uint16_t);
  using MailboxAvailableHandler = void (*)(uint16_t);

  MailboxClass();
  void send(const char* message);
  void send(const uint8_t* data, size_t length);
  void requestRead();
  void requestAvailable();
  void handleResponse(const rpc::Frame& frame);
  inline void onMailboxMessage(MailboxHandler handler) {
    _mailbox_handler = handler;
  }
  inline void onMailboxAvailableResponse(MailboxAvailableHandler handler) {
    _mailbox_available_handler = handler;
  }

 private:
  MailboxHandler _mailbox_handler;
  MailboxAvailableHandler _mailbox_available_handler;
};
extern MailboxClass Mailbox;
#endif

#if BRIDGE_ENABLE_FILESYSTEM
class FileSystemClass {
 public:
  using FileSystemReadHandler = void (*)(const uint8_t*, uint16_t);

  FileSystemClass() : _file_system_read_handler(nullptr) {}

  void write(const char* filePath, const uint8_t* data, size_t length);
  void remove(const char* filePath);
  void read(const char* filePath);
  void handleResponse(const rpc::Frame& frame);
  inline void onFileSystemReadResponse(FileSystemReadHandler handler) {
    _file_system_read_handler = handler;
  }

 private:
  FileSystemReadHandler _file_system_read_handler;
};
extern FileSystemClass FileSystem;
#endif

#if BRIDGE_ENABLE_PROCESS
class ProcessClass {
  #if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::ProcessTestAccessor;
  #endif
 public:
  using ProcessRunHandler = void (*)(rpc::StatusCode, const uint8_t*, uint16_t,
                                     const uint8_t*, uint16_t);
  using ProcessPollHandler = void (*)(rpc::StatusCode, uint8_t, const uint8_t*,
                                      uint16_t, const uint8_t*, uint16_t);
  using ProcessRunAsyncHandler = void (*)(int16_t);  // PID from daemon (signed for error sentinel)

  ProcessClass();
  void run(const char* command);
  void runAsync(const char* command);
  void poll(int16_t pid);
  void kill(int16_t pid);
  void handleResponse(const rpc::Frame& frame);
  
  inline void onProcessRunResponse(ProcessRunHandler handler) {
    _process_run_handler = handler;
  }
  inline void onProcessPollResponse(ProcessPollHandler handler) {
    _process_poll_handler = handler;
  }
  inline void onProcessRunAsyncResponse(ProcessRunAsyncHandler handler) {
    _process_run_async_handler = handler;
  }

 private:
  bool _pushPendingProcessPid(uint16_t pid);
  uint16_t _popPendingProcessPid();

  // [SIL-2] Use circular buffer for safe PID tracking
  etl::circular_buffer<uint16_t, BRIDGE_MAX_PENDING_PROCESS_POLLS> _pending_process_pids;
  
  ProcessRunHandler _process_run_handler;
  ProcessPollHandler _process_poll_handler;
  ProcessRunAsyncHandler _process_run_async_handler;
};
extern ProcessClass Process;
#endif

#endif

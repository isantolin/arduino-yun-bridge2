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
#include "etl/observer.h"

// [SIL-2] ISR Safety: Atomic Blocks
#if defined(ARDUINO_ARCH_AVR)
  #include <util/atomic.h>
  #define BRIDGE_ATOMIC_BLOCK ATOMIC_BLOCK(ATOMIC_RESTORESTATE)
  // [Compatibility] Polyfill for boards missing SERIAL_PORT_USBVIRTUAL (e.g. Mega 2560)
  #ifndef SERIAL_PORT_USBVIRTUAL
    #define SERIAL_PORT_USBVIRTUAL Serial
  #include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

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
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

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
#include "etl/bitset.h"

// [SIL-2] Lightweight FSM + Scheduler for deterministic state transitions
#include "fsm/bridge_fsm.h"

// [SIL-2] ETL Message Router for command dispatch
#include "router/command_router.h"

// [SIL-2] Static Constraints
static_assert(rpc::MAX_PAYLOAD_SIZE <= 1024, "Payload size exceeds safety limits for small RAM targets");

#if defined(ARDUINO_ARCH_AVR)
extern "C" char __heap_start;
extern "C" char* __brkval;
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

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
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif
}

// --- Configuration ---

#ifndef BRIDGE_ENABLE_WATCHDOG
constexpr bool kBridgeEnableWatchdog = true;
#else
constexpr bool kBridgeEnableWatchdog = (BRIDGE_ENABLE_WATCHDOG != 0);
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif

#if defined(ARDUINO_ARCH_AVR) && BRIDGE_ENABLE_WATCHDOG
#ifndef BRIDGE_WATCHDOG_TIMEOUT
#define BRIDGE_WATCHDOG_TIMEOUT WDTO_2S
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif

// [SIL-2] Multi-platform watchdog support
#if defined(ARDUINO_ARCH_ESP32) && BRIDGE_ENABLE_WATCHDOG
#include <esp_task_wdt.h>
#ifndef BRIDGE_WATCHDOG_TIMEOUT_MS
#define BRIDGE_WATCHDOG_TIMEOUT_MS 2000
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif

#if defined(ARDUINO_ARCH_ESP8266) && BRIDGE_ENABLE_WATCHDOG
// ESP8266 uses yield() for watchdog - software WDT
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif

#ifdef BRIDGE_FIRMWARE_VERSION_MAJOR
constexpr uint8_t kDefaultFirmwareVersionMajor = BRIDGE_FIRMWARE_VERSION_MAJOR;
#else
constexpr uint8_t kDefaultFirmwareVersionMajor = 2;
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif

#ifdef BRIDGE_FIRMWARE_VERSION_MINOR
constexpr uint8_t kDefaultFirmwareVersionMinor = BRIDGE_FIRMWARE_VERSION_MINOR;
#else
constexpr uint8_t kDefaultFirmwareVersionMinor = 5;
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif

#ifndef BRIDGE_MAX_OBSERVERS
#define BRIDGE_MAX_OBSERVERS 4
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif

// --- Subsystem Enablement (RAM Optimization) ---
// Note: Macros are now centralized in config/bridge_config.h

// [SIL-2] Resource Allocation Tuning
// Note: BRIDGE_MAX_PENDING_TX_FRAMES moved to bridge_config.h

// [SIL-2] Serial Port Selection logic
// Priority 1: Manual override via BRIDGE_FORCE_SERIAL0 or BRIDGE_EMULATION (Always UART0)
// Priority 2: Boards with dedicated Bridge port (Yun/Mega/Zero use Serial1)
// Priority 3: Standard boards (Uno/Nano use Serial)
#if defined(BRIDGE_FORCE_SERIAL0) || defined(BRIDGE_EMULATION)
  #define BRIDGE_DEFAULT_SERIAL_PORT Serial
#elif defined(ARDUINO_ARCH_AVR) && (defined(__AVR_ATmega32U4__) || defined(__AVR_ATmega2560__) || defined(__AVR_ATmega1280__) || defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM) || defined(_VARIANT_ARDUINO_ZERO_) || defined(HAVE_HWSERIAL1))
  // Boards with multiple UARTs or native USB (Yun, Mega, Zero, Leonardo)
  #define BRIDGE_DEFAULT_SERIAL_PORT Serial1
#else
  // Standard boards (Uno, Pro Mini)
  #define BRIDGE_DEFAULT_SERIAL_PORT Serial
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

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
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif

// [SIL-2] Observer Event Types
struct MsgBridgeSynchronized {};
struct MsgBridgeLost {};
struct MsgBridgeError { rpc::StatusCode code; };

// [SIL-2] Observer Interface for System Events
struct BridgeObserver : public etl::observer<MsgBridgeSynchronized, MsgBridgeLost, MsgBridgeError> {
  virtual ~BridgeObserver() = default;
  virtual void notification(MsgBridgeSynchronized) {}
  virtual void notification(MsgBridgeLost) {}
  virtual void notification(MsgBridgeError) {}
};

/**
 * @brief Helper for fragmented transmissions.
 * Encapsulates chunking, flow control, and safety checks.
 */
class BridgeWriter {
public:
    static bool send(rpc::CommandId command_id, 
                    const uint8_t* header, size_t header_len, 
                    const uint8_t* data, size_t data_len);
};

class BridgeClass : public bridge::router::ICommandHandler, 
                    public etl::observable<BridgeObserver, BRIDGE_MAX_OBSERVERS> {
  #if BRIDGE_ENABLE_DATASTORE
  friend class DataStoreClass;
  #include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif
  #if BRIDGE_ENABLE_MAILBOX
  friend class MailboxClass;
  #include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif
  #if BRIDGE_ENABLE_FILESYSTEM
  friend class FileSystemClass;
  #include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif
  #if BRIDGE_ENABLE_PROCESS
  friend class ProcessClass;
  #include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif
  #if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::TestAccessor;
  #include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

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
  #include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif

  #if BRIDGE_ENABLE_MAILBOX
  using MailboxHandler = etl::delegate<void(const uint8_t*, uint16_t)>;
  using MailboxAvailableHandler = etl::delegate<void(uint16_t)>;
  #include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif

  #if BRIDGE_ENABLE_FILESYSTEM
  using FileSystemReadHandler = etl::delegate<void(const uint8_t*, uint16_t)>;
  #include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif

  #if BRIDGE_ENABLE_PROCESS
  using ProcessRunHandler = etl::delegate<void(rpc::StatusCode, const uint8_t*, uint16_t, const uint8_t*, uint16_t)>;
  using ProcessPollHandler = etl::delegate<void(rpc::StatusCode, uint8_t, const uint8_t*, uint16_t, const uint8_t*, uint16_t)>;
  using ProcessRunAsyncHandler = etl::delegate<void(int16_t)>;
  #include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif
  
  explicit BridgeClass(HardwareSerial& serial);
  explicit BridgeClass(Stream& stream);
  
  // [SIL-2] Observable Management
  void add_observer(BridgeObserver& obs) { 
    etl::observable<BridgeObserver, BRIDGE_MAX_OBSERVERS>::add_observer(obs); 
  }
  void remove_observer(BridgeObserver& obs) { 
    etl::observable<BridgeObserver, BRIDGE_MAX_OBSERVERS>::remove_observer(obs); 
  }

  void begin(unsigned long baudrate = 
#ifdef BRIDGE_BAUDRATE
          BRIDGE_BAUDRATE
#else
          rpc::RPC_DEFAULT_BAUDRATE
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

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
  
  // [SIL-2] Large Payload Support
  bool sendChunkyFrame(rpc::CommandId command_id, 
                       const uint8_t* header, size_t header_len, 
                       const uint8_t* data, size_t data_len);

  // Internal Callback Trampoline for PacketSerial
  static void onPacketReceived(const uint8_t* buffer, size_t size);

 protected:
  // [SIL-2] Internal notification helper
  template<typename T>
  void notify_system(const T& msg) {
    notify_observers(msg);
  }

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
  etl::delegate<void()> _cb_ack_timeout;
  etl::delegate<void()> _cb_rx_dedupe;
  etl::delegate<void()> _cb_baudrate_change;
  etl::delegate<void()> _cb_startup_stabilized;

  volatile bool _startup_stabilizing;

  // [SIL-2] ETL Message Router for flattened command dispatch
  bridge::router::CommandRouter _command_router;

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

  etl::bitset<64> _pin_states;
};

extern BridgeClass Bridge;











#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

#endif

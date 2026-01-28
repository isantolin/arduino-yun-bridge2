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

#include <Arduino.h>
#include <Stream.h>

#include "bridge_config.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "arduino/BridgeTransport.h"

#undef min
#undef max
#include "etl/array.h"
#include "etl/deque.h"
#include "etl/queue.h"
#include "etl/string.h"
#include "etl/circular_buffer.h"
#include "etl/vector.h"

// [SIL-2] Static Constraints
static_assert(rpc::MAX_PAYLOAD_SIZE <= 1024, "Payload size exceeds safety limits for small RAM targets");

// --- Configuration ---


#ifndef BRIDGE_DEBUG_FRAMES
constexpr bool kBridgeDebugFrames = false;
#else
constexpr bool kBridgeDebugFrames = (BRIDGE_DEBUG_FRAMES != 0);
#endif

#ifndef BRIDGE_DEBUG_IO
constexpr bool kBridgeDebugIo = false;
#else
constexpr bool kBridgeDebugIo = (BRIDGE_DEBUG_IO != 0);
#endif

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
constexpr uint8_t kDefaultFirmwareVersionMinor = 0;
#endif

class BridgeClass {
  friend class DataStoreClass;
  friend class MailboxClass;
  friend class FileSystemClass;
  friend class ProcessClass;
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
  bool isSynchronized() const { return _synchronized; }

  // Events
  void onCommand(CommandHandler handler);
  void onDigitalReadResponse(DigitalReadHandler handler);
  void onAnalogReadResponse(AnalogReadHandler handler);
  void onGetFreeMemoryResponse(GetFreeMemoryHandler handler);
  void onStatus(StatusHandler handler);

  // Internal / Lower Level
  bool sendFrame(rpc::CommandId command_id, const uint8_t* payload = nullptr, size_t length = 0);
  bool sendFrame(rpc::StatusCode status_code, const uint8_t* payload = nullptr, size_t length = 0);
  void flushStream();
  void enterSafeState(); // [SIL-2] Force system into fail-safe state
  uint8_t* getScratchBuffer() { return _scratch_payload.data(); }
  void _emitStatus(rpc::StatusCode status_code, const char* message = nullptr);
  void _emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message);

#if BRIDGE_DEBUG_FRAMES
  struct FrameDebugSnapshot {
    uint16_t tx_count;
    uint16_t build_failures;
    uint16_t write_shortfall_events;
    uint16_t last_command_id;
    uint16_t payload_length;
    uint16_t raw_length;
    uint16_t cobs_length;
    uint16_t expected_serial_bytes;
    uint16_t last_write_return;
    uint16_t last_shortfall;
    uint16_t crc;
  };

  FrameDebugSnapshot getTxDebugSnapshot() const;
  void resetTxDebugStats();
#endif

 private:
  bridge::BridgeTransport _transport;
  const uint8_t* _shared_secret;
  size_t _shared_secret_len;

  // Protocol Engine
  rpc::Frame _rx_frame;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> _scratch_payload;

  // State
  bool _awaiting_ack;
  uint16_t _last_command_id;
  uint8_t _retry_count;
  unsigned long _last_send_millis;

  // Incoming deduplication (idempotency for retries)
  uint32_t _last_rx_crc;
  unsigned long _last_rx_crc_millis;

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
  etl::queue<PendingTxFrame, rpc::RPC_MAX_PENDING_TX_FRAMES> _pending_tx_queue;
  bool _synchronized;

#if BRIDGE_DEBUG_FRAMES
  mutable FrameDebugSnapshot _tx_debug;
#endif

  // Methods
  void _handleSystemCommand(const rpc::Frame& frame);
  void _handleGpioCommand(const rpc::Frame& frame);
  void _handleConsoleCommand(const rpc::Frame& frame);

  bool _isRecentDuplicateRx(const rpc::Frame& frame) const;
  void _markRxProcessed(const rpc::Frame& frame);

  void dispatch(const rpc::Frame& frame);
  bool _sendFrame(uint16_t command_id, const uint8_t* payload, size_t length);
  bool _sendFrameImmediate(uint16_t command_id, const uint8_t* payload, size_t length);
  bool _requiresAck(uint16_t command_id) const;
  void _retransmitLastFrame();
  void _processAckTimeout();
  void _handleAck(uint16_t command_id);
  void _handleMalformed(uint16_t command_id);
  void _resetLinkState();
  void _sendAckAndFlush(uint16_t command_id);  // Encapsulates ACK + flush sequence
  void _computeHandshakeTag(const uint8_t* nonce, size_t nonce_len, uint8_t* out_tag);
  void _applyTimingConfig(const uint8_t* payload, size_t length);

  void _flushPendingTxQueue();
  void _clearPendingTxQueue();
  bool _enqueuePendingTx(uint16_t command_id, const uint8_t* payload, size_t length);
  bool _dequeuePendingTx(PendingTxFrame& frame);
  void _clearAckState();
};

extern BridgeClass Bridge;

class ConsoleClass : public Stream {
 public:
  ConsoleClass();
  void begin();
  void end() {}
  void buffer(uint8_t size) { (void)size; }
  void noBuffer() {}
  bool connected() { return true; }
  
  size_t write(uint8_t c) override;
  size_t write(const uint8_t *buffer, size_t size) override;
  
  void _push(const uint8_t* data, size_t length);
  
  int available() override;
  int read() override;
  int peek() override;
  void flush() override;
  
  operator bool() { return connected(); }

 private:
  bool _begun;
  bool _xoff_sent;
  
  // [SIL-2] Use ETL containers for safe buffer management
  // Replaces manual circular buffer logic
  etl::circular_buffer<uint8_t, BRIDGE_CONSOLE_RX_BUFFER_SIZE> _rx_buffer;
  etl::vector<uint8_t, BRIDGE_CONSOLE_TX_BUFFER_SIZE> _tx_buffer;
};
extern ConsoleClass Console;

class DataStoreClass {
 public:
  using DataStoreGetHandler = void (*)(const char*, const uint8_t*, uint16_t);

  DataStoreClass();
  void put(const char* key, const char* value);
  void requestGet(const char* key);
  void handleResponse(const rpc::Frame& frame);
  void onDataStoreGetResponse(DataStoreGetHandler handler);

 private:
  bool _trackPendingDatastoreKey(const char* key);
  const char* _popPendingDatastoreKey();

  etl::deque<etl::string<rpc::RPC_MAX_DATASTORE_KEY_LENGTH>, BRIDGE_MAX_PENDING_DATASTORE> _pending_datastore_keys;
  DataStoreGetHandler _datastore_get_handler;
};
extern DataStoreClass DataStore;

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
  void onMailboxMessage(MailboxHandler handler);
  void onMailboxAvailableResponse(MailboxAvailableHandler handler);

 private:
  MailboxHandler _mailbox_handler;
  MailboxAvailableHandler _mailbox_available_handler;
};
extern MailboxClass Mailbox;

class FileSystemClass {
 public:
  using FileSystemReadHandler = void (*)(const uint8_t*, uint16_t);

  void write(const char* filePath, const uint8_t* data, size_t length);
  void remove(const char* filePath);
  void read(const char* filePath);
  void handleResponse(const rpc::Frame& frame);
  void onFileSystemReadResponse(FileSystemReadHandler handler);

 private:
  FileSystemReadHandler _file_system_read_handler;
};
extern FileSystemClass FileSystem;

class ProcessClass {
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
  
  void onProcessRunResponse(ProcessRunHandler handler);
  void onProcessPollResponse(ProcessPollHandler handler);
  void onProcessRunAsyncResponse(ProcessRunAsyncHandler handler);

 private:
  bool _pushPendingProcessPid(uint16_t pid);
  uint16_t _popPendingProcessPid();

  uint16_t _pending_process_pids[BRIDGE_MAX_PENDING_PROCESS_POLLS];
  uint8_t _pending_process_poll_head;
  uint8_t _pending_process_poll_count;
  
  ProcessRunHandler _process_run_handler;
  ProcessPollHandler _process_poll_handler;
  ProcessRunAsyncHandler _process_run_async_handler;
};
extern ProcessClass Process;

#endif
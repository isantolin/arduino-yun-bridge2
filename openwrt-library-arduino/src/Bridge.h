/*
 * This file is part of Arduino Yun Ecosystem v2.
 * (C) 2025 Ignacio Santolin
 */
#ifndef BRIDGE_H
#define BRIDGE_H

#include <Arduino.h>
#include <Stream.h>

#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "arduino/BridgeTransport.h"

// --- Configuration ---
#ifdef BRIDGE_BAUDRATE
constexpr unsigned long kBridgeBaudrate = BRIDGE_BAUDRATE;
#else
constexpr unsigned long kBridgeBaudrate = rpc::RPC_DEFAULT_BAUDRATE;
#endif

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
  // Constants
  static constexpr uint8_t kFirmwareVersionMajor = kDefaultFirmwareVersionMajor;
  static constexpr uint8_t kFirmwareVersionMinor = kDefaultFirmwareVersionMinor;

  static constexpr size_t kMaxFilePathLength = rpc::RPC_MAX_FILEPATH_LENGTH;
  static constexpr size_t kMaxDatastoreKeyLength = rpc::RPC_MAX_DATASTORE_KEY_LENGTH;
  static constexpr uint8_t kMaxPendingTxFrames = rpc::RPC_MAX_PENDING_TX_FRAMES;
  static constexpr unsigned int kAckTimeoutMs = rpc::RPC_DEFAULT_ACK_TIMEOUT_MS;
  static constexpr uint8_t kMaxAckRetries = rpc::RPC_DEFAULT_RETRY_LIMIT;

  // Flow Control Thresholds (assuming 64 byte hardware buffer)
  static constexpr int kRxHighWaterMark = 48; // 75% full -> Send XOFF
  static constexpr int kRxLowWaterMark = 16;  // 25% full -> Send XON

  // Callbacks
  using CommandHandler = void (*)(const rpc::Frame&);
  using DigitalReadHandler = void (*)(uint8_t);
  using AnalogReadHandler = void (*)(uint16_t);
  using GetFreeMemoryHandler = void (*)(uint16_t);
  using StatusHandler = void (*)(rpc::StatusCode, const uint8_t*, uint16_t);

  explicit BridgeClass(HardwareSerial& serial);
  explicit BridgeClass(Stream& stream);

  void begin(unsigned long baudrate = kBridgeBaudrate,
             const char* secret = nullptr, size_t secret_len = 0);
  void process();
  bool isSynchronized() const { return _synchronized; }

  // API
  void pinMode(uint8_t pin, uint8_t mode);
  void digitalWrite(uint8_t pin, uint8_t value);
  void analogWrite(uint8_t pin, int value);
  
  // Request Methods
  void requestDigitalRead(uint8_t pin);
  void requestAnalogRead(uint8_t pin);
  void requestGetFreeMemory();

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
  uint8_t* getScratchBuffer() { return _scratch_payload; }
  void _emitStatus(rpc::StatusCode status_code, const char* message = nullptr);
  void _emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message);

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

#if BRIDGE_DEBUG_FRAMES
  FrameDebugSnapshot getTxDebugSnapshot() const;
  void resetTxDebugStats();
#else
  FrameDebugSnapshot getTxDebugSnapshot() const { return {}; }
  void resetTxDebugStats() {}
#endif

#if defined(BRIDGE_HOST_TEST)
 public:
#else
 private:
#endif
  bridge::BridgeTransport _transport;
  const uint8_t* _shared_secret;
  size_t _shared_secret_len;

  // Protocol Engine
  rpc::Frame _rx_frame;
  uint8_t _scratch_payload[rpc::MAX_PAYLOAD_SIZE];

  // State
  bool _awaiting_ack;
  uint16_t _last_command_id;
  uint8_t _retry_count;
  unsigned long _last_send_millis;

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
    uint8_t payload[rpc::MAX_PAYLOAD_SIZE];
  };
  PendingTxFrame _pending_tx_frames[kMaxPendingTxFrames];
  uint8_t _pending_tx_head;
  uint8_t _pending_tx_count;
  bool _synchronized;

#if BRIDGE_DEBUG_FRAMES
  mutable FrameDebugSnapshot _tx_debug;
#endif

  // Methods
  void _handleSystemCommand(const rpc::Frame& frame);
  void _handleGpioCommand(const rpc::Frame& frame);
  void _handleConsoleCommand(const rpc::Frame& frame);

  void dispatch(const rpc::Frame& frame);
  bool _sendFrame(uint16_t command_id, const uint8_t* payload, size_t length);
  bool _sendFrameImmediate(uint16_t command_id, const uint8_t* payload, size_t length);
  bool _requiresAck(uint16_t command_id) const;
  void _retransmitLastFrame();
  void _processAckTimeout();
  void _handleAck(uint16_t command_id);
  void _handleMalformed(uint16_t command_id);
  void _resetLinkState();
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
  static constexpr size_t kTxBufferSize = 64;
  static constexpr size_t kRxBufferSize = 64;
  static constexpr size_t kBufferLowWater = 16;
  static constexpr size_t kBufferHighWater = 48;

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
  size_t _rx_buffer_head;
  size_t _rx_buffer_tail;
  size_t _tx_buffer_pos;
  bool _xoff_sent;
  uint8_t _rx_buffer[kRxBufferSize];
  uint8_t _tx_buffer[kTxBufferSize];
};
extern ConsoleClass Console;

class DataStoreClass {
 public:
  static constexpr uint8_t kMaxPendingDatastore = 2;
  using DataStoreGetHandler = void (*)(const char*, const uint8_t*, uint16_t);

  DataStoreClass();
  void put(const char* key, const char* value);
  void requestGet(const char* key);
  void handleResponse(const rpc::Frame& frame);
  void onDataStoreGetResponse(DataStoreGetHandler handler);

#if defined(BRIDGE_HOST_TEST)
 public:
#else
 private:
#endif
  bool _trackPendingDatastoreKey(const char* key);
  const char* _popPendingDatastoreKey();

  char _pending_datastore_keys[kMaxPendingDatastore][BridgeClass::kMaxDatastoreKeyLength + 1];
  uint8_t _pending_datastore_key_lengths[kMaxPendingDatastore];
  uint8_t _pending_datastore_head;
  uint8_t _pending_datastore_count;
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

#if defined(BRIDGE_HOST_TEST)
 public:
#else
 private:
#endif
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

#if defined(BRIDGE_HOST_TEST)
 public:
#else
 private:
#endif
  FileSystemReadHandler _file_system_read_handler;
};
extern FileSystemClass FileSystem;

class ProcessClass {
 public:
  static constexpr uint8_t kMaxPendingProcessPolls = 2;
  using ProcessRunHandler = void (*)(rpc::StatusCode, const uint8_t*, uint16_t,
                                     const uint8_t*, uint16_t);
  using ProcessPollHandler = void (*)(rpc::StatusCode, uint8_t, const uint8_t*,
                                      uint16_t, const uint8_t*, uint16_t);
  using ProcessRunAsyncHandler = void (*)(int);

  ProcessClass();
  void run(const char* command);
  void runAsync(const char* command);
  void poll(int pid);
  void kill(int pid);
  void handleResponse(const rpc::Frame& frame);
  
  void onProcessRunResponse(ProcessRunHandler handler);
  void onProcessPollResponse(ProcessPollHandler handler);
  void onProcessRunAsyncResponse(ProcessRunAsyncHandler handler);

#if defined(BRIDGE_HOST_TEST)
 public:
#else
 private:
#endif
  bool _pushPendingProcessPid(uint16_t pid);
  uint16_t _popPendingProcessPid();

  uint16_t _pending_process_pids[kMaxPendingProcessPolls];
  uint8_t _pending_process_poll_head;
  uint8_t _pending_process_poll_count;
  
  ProcessRunHandler _process_run_handler;
  ProcessPollHandler _process_poll_handler;
  ProcessRunAsyncHandler _process_run_async_handler;
};
extern ProcessClass Process;

#endif

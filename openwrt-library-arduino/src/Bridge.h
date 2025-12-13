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

// --- Configuration ---
#define BRIDGE_BAUDRATE RPC_DEFAULT_BAUDRATE

#ifndef BRIDGE_DEBUG_FRAMES
#define BRIDGE_DEBUG_FRAMES 0
#endif

#ifndef BRIDGE_DEBUG_IO
#define BRIDGE_DEBUG_IO 0
#endif

#ifndef BRIDGE_FIRMWARE_VERSION_MAJOR
#define BRIDGE_FIRMWARE_VERSION_MAJOR 2
#endif

#ifndef BRIDGE_FIRMWARE_VERSION_MINOR
#define BRIDGE_FIRMWARE_VERSION_MINOR 0
#endif

class BridgeClass {
  friend class DataStoreClass;
  friend class MailboxClass;
  friend class FileSystemClass;
  friend class ProcessClass;
 public:
  // Constants
  static constexpr size_t kMaxFilePathLength = 64;
  static constexpr size_t kMaxDatastoreKeyLength = 32;
  static constexpr uint8_t kMaxPendingDatastore = 2;
  static constexpr uint8_t kMaxPendingProcessPolls = 2;
  static constexpr uint8_t kMaxPendingTxFrames = 2;
  static constexpr unsigned int kAckTimeoutMs = 200;
  static constexpr uint8_t kMaxAckRetries = 5;

  // Flow Control Thresholds (assuming 64 byte hardware buffer)
  static constexpr int kRxHighWaterMark = 48; // 75% full -> Send XOFF
  static constexpr int kRxLowWaterMark = 16;  // 25% full -> Send XON

  // Callbacks
  using CommandHandler = void (*)(const rpc::Frame&);
  using DataStoreGetHandler = void (*)(const char*, const uint8_t*, uint16_t);
  using MailboxHandler = void (*)(const uint8_t*, uint16_t);
  using MailboxAvailableHandler = void (*)(uint16_t);
  using ProcessRunHandler = void (*)(rpc::StatusCode, const uint8_t*, uint16_t,
                                     const uint8_t*, uint16_t);
  using ProcessPollHandler = void (*)(rpc::StatusCode, uint8_t, const uint8_t*,
                                      uint16_t, const uint8_t*, uint16_t);
  using ProcessRunAsyncHandler = void (*)(int);
  using FileSystemReadHandler = void (*)(const uint8_t*, uint16_t);
  using DigitalReadHandler = void (*)(uint8_t);
  using AnalogReadHandler = void (*)(uint16_t);
  using GetFreeMemoryHandler = void (*)(uint16_t);
  using StatusHandler = void (*)(rpc::StatusCode, const uint8_t*, uint16_t);

  explicit BridgeClass(HardwareSerial& serial);
  explicit BridgeClass(Stream& stream);

  void begin(unsigned long baudrate = BRIDGE_BAUDRATE,
             const char* secret = nullptr, size_t secret_len = 0);
  void process();

  // API
  void pinMode(uint8_t pin, uint8_t mode);
  void digitalWrite(uint8_t pin, uint8_t value);
  void analogWrite(uint8_t pin, int value);
  
  // Request Methods
  void requestDigitalRead(uint8_t pin);
  void requestAnalogRead(uint8_t pin);
  void requestProcessRun(const char* command);
  void requestProcessRunAsync(const char* command);
  void requestProcessPoll(int pid);
  void requestFileSystemRead(const char* filePath);
  void requestGetFreeMemory();

  // Events
  void onCommand(CommandHandler handler);
  void onDataStoreGetResponse(DataStoreGetHandler handler);
  void onMailboxMessage(MailboxHandler handler);
  void onMailboxAvailableResponse(MailboxAvailableHandler handler);
  void onProcessRunResponse(ProcessRunHandler handler);
  void onProcessPollResponse(ProcessPollHandler handler);
  void onProcessRunAsyncResponse(ProcessRunAsyncHandler handler);
  void onFileSystemReadResponse(FileSystemReadHandler handler);
  void onDigitalReadResponse(DigitalReadHandler handler);
  void onAnalogReadResponse(AnalogReadHandler handler);
  void onGetFreeMemoryResponse(GetFreeMemoryHandler handler);
  void onStatus(StatusHandler handler);

  // Internal / Lower Level
  bool sendFrame(rpc::CommandId command_id, const uint8_t* payload = nullptr, size_t length = 0);
  bool sendFrame(rpc::StatusCode status_code, const uint8_t* payload = nullptr, size_t length = 0);
  void flushStream();
  uint8_t* getScratchBuffer() { return _scratch_payload; }

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

 private:
  Stream& _stream;
  HardwareSerial* _hardware_serial;
  const uint8_t* _shared_secret;
  size_t _shared_secret_len;

  // Protocol Engine
  rpc::FrameParser _parser;
  rpc::FrameBuilder _builder;
  rpc::Frame _rx_frame;
  uint8_t _raw_frame_buffer[rpc::MAX_RAW_FRAME_SIZE];
  uint8_t _last_cobs_frame[rpc::COBS_BUFFER_SIZE];
  uint8_t _scratch_payload[rpc::MAX_PAYLOAD_SIZE];

  // State
  bool _awaiting_ack;
  bool _flow_paused; // New: Flow Control State
  uint16_t _last_command_id;
  uint16_t _last_cobs_length;
  uint8_t _retry_count;
  unsigned long _last_send_millis;

  // Config
  uint16_t _ack_timeout_ms;
  uint8_t _ack_retry_limit;
  uint32_t _response_timeout_ms;

  // Handlers
  CommandHandler _command_handler;
  DataStoreGetHandler _datastore_get_handler;
  MailboxHandler _mailbox_handler;
  MailboxAvailableHandler _mailbox_available_handler;
  ProcessRunHandler _process_run_handler;
  ProcessPollHandler _process_poll_handler;
  DigitalReadHandler _digital_read_handler;
  AnalogReadHandler _analog_read_handler;
  ProcessRunAsyncHandler _process_run_async_handler;
  FileSystemReadHandler _file_system_read_handler;
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

  char _pending_datastore_keys[kMaxPendingDatastore][kMaxDatastoreKeyLength + 1];
  uint8_t _pending_datastore_key_lengths[kMaxPendingDatastore];
  uint8_t _pending_datastore_head;
  uint8_t _pending_datastore_count;

  uint16_t _pending_process_pids[kMaxPendingProcessPolls];
  uint8_t _pending_process_poll_head;
  uint8_t _pending_process_poll_count;

#if BRIDGE_DEBUG_FRAMES
  mutable FrameDebugSnapshot _tx_debug;
#endif

  // Methods
  void dispatch(const rpc::Frame& frame);
  bool _sendFrame(uint16_t command_id, const uint8_t* payload, size_t length);
  bool _sendFrameImmediate(uint16_t command_id, const uint8_t* payload, size_t length);
  void _emitStatus(rpc::StatusCode status_code, const char* message = nullptr);
  void _emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message);
  size_t _writeFrameBytes(const uint8_t* data, size_t length);
  bool _requiresAck(uint16_t command_id) const;
  void _recordLastFrame(uint16_t command_id, const uint8_t* cobs_frame, size_t cobs_len);
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

  bool _trackPendingDatastoreKey(const char* key);
  const char* _popPendingDatastoreKey();
  bool _pushPendingProcessPid(uint16_t pid);
  uint16_t _popPendingProcessPid();
  void _clearAckState();
};

extern BridgeClass Bridge;

// These classes are wrappers around Bridge calls usually
#define CONSOLE_TX_BUFFER_SIZE 64
#define CONSOLE_RX_BUFFER_SIZE 64
#define CONSOLE_BUFFER_LOW_WATER 16
#define CONSOLE_BUFFER_HIGH_WATER 48

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
  size_t _rx_buffer_head;
  size_t _rx_buffer_tail;
  size_t _tx_buffer_pos;
  bool _xoff_sent;
  uint8_t _rx_buffer[CONSOLE_RX_BUFFER_SIZE];
  uint8_t _tx_buffer[CONSOLE_TX_BUFFER_SIZE];
};
extern ConsoleClass Console;

// Placeholder classes to satisfy dependencies if they were used in sketches
// In a full implementation these would have methods mapping to Bridge calls
class DataStoreClass {
 public:
  DataStoreClass();
  void put(const char* key, const char* value);
  void requestGet(const char* key);
};
extern DataStoreClass DataStore;

class MailboxClass {
 public:
  MailboxClass();
  void send(const char* message);
  void send(const uint8_t* data, size_t length);
  void requestRead();
  void requestAvailable();
};
extern MailboxClass Mailbox;

class FileSystemClass {
 public:
  void write(const char* filePath, const uint8_t* data, size_t length);
  void remove(const char* filePath);
};
extern FileSystemClass FileSystem;

class ProcessClass {
 public:
  ProcessClass();
  void kill(int pid);
};
extern ProcessClass Process;

#endif
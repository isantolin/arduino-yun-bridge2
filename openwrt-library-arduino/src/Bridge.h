/*
 * Bridge.h - Main header for the Arduino Yun Ecosystem v2.
 *
 * This header defines the public API for the Bridge library and its components.
 * It includes declarations for BridgeClass, ConsoleClass, ProcessClass, and others.
 *
 * Copyright (c) 2024 Arduino Yun Ecosystem v2
 */

#ifndef BRIDGE_H_
#define BRIDGE_H_

#include "Arduino.h"
#include "Stream.h"
#include "arduino/StringUtils.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"

// Forward declarations of mock/test dependencies
// In a real build, these would be standard library headers or platform specific.

// Constants for configuration
#ifndef BRIDGE_BAUDRATE
#define BRIDGE_BAUDRATE 250000
#endif

namespace rpc {
// Forward declaration
struct Frame;
// Define local max frame size if not in protocol
// Payload + Header (4) + CRC (4) + COBS Overhead (~1 + len/254) + 2 (0x00 delimiters)
constexpr size_t K_FRAME_OVERHEAD = 16;
constexpr size_t K_MAX_FRAME_BUFFER_SIZE = MAX_PAYLOAD_SIZE + K_FRAME_OVERHEAD;
}

/*
 * BridgeClass
 * Manages the serial link to the OpenWrt side.
 * Handles framing, dispatching, and low-level protocol details.
 */
class BridgeClass {
 public:
  static constexpr uint8_t kFirmwareVersionMajor = 2;
  static constexpr uint8_t kFirmwareVersionMinor = 0;

  // Constructors
  explicit BridgeClass(HardwareSerial& serial);
  explicit BridgeClass(Stream& stream);

  // Initialization
  void begin(unsigned long baudrate = BRIDGE_BAUDRATE,
             const char* secret = nullptr, size_t secret_len = 0);
  
  // Main loop task - MUST be called frequently
  void process();

  // Low-level frame sending
  bool sendFrame(rpc::CommandId command_id, const uint8_t* payload = nullptr,
                 size_t length = 0);
  bool sendFrame(rpc::StatusCode status_code, const uint8_t* payload = nullptr,
                 size_t length = 0);

  // Arduino API shims
  void put(const char* key, const char* value);
  unsigned int get(const char* key, uint8_t* buff, unsigned int size);

  // Deprecated / Legacy API support (mapped to new protocol where possible)
  void pinMode(uint8_t pin, uint8_t mode);
  void digitalWrite(uint8_t pin, uint8_t value);
  int digitalRead(uint8_t pin);
  void analogWrite(uint8_t pin, int value);
  int analogRead(uint8_t pin);
  
  // Bridge.transfer is not supported in v2 as it was raw efficient transfer.
  // Use specific components instead.

  // Callback types
  typedef void (*CommandHandler)(const rpc::Frame& frame);
  typedef void (*DigitalReadHandler)(uint8_t pin, int value);
  typedef void (*AnalogReadHandler)(uint8_t pin, int value);
  typedef void (*GetFreeMemoryHandler)(uint16_t free_memory);
  typedef void (*StatusHandler)(rpc::StatusCode code, const uint8_t* msg, uint16_t len);

  // Registration for callbacks
  void onCommand(CommandHandler handler);
  void onDigitalReadResponse(DigitalReadHandler handler);
  void onAnalogReadResponse(AnalogReadHandler handler);
  void onGetFreeMemoryResponse(GetFreeMemoryHandler handler);
  void onStatus(StatusHandler handler);
  
  // Internal or Advanced helpers
  void flushStream(); // Flush the underlying transport stream
  
  // Request methods (MCU -> Linux)
  void requestDigitalRead(uint8_t pin);
  void requestAnalogRead(uint8_t pin);
  void requestGetFreeMemory();

#if BRIDGE_DEBUG_FRAMES
  struct FrameDebugSnapshot {
      uint32_t rx_frames;
      uint32_t tx_frames;
      uint32_t rx_bytes;
      uint32_t tx_bytes;
      uint32_t crc_errors;
      uint32_t framing_errors;
      uint32_t serial_overflows;
  };
  FrameDebugSnapshot getTxDebugSnapshot() const;
  void resetTxDebugStats();
#endif
  
  // Internal helper to emit statuses easily
  void _emitStatus(rpc::StatusCode status_code, const char* message);
  void _emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message);


 private:
  // Transport Layer
  class BridgeTransport {
   public:
    BridgeTransport(Stream& stream, HardwareSerial* hw_serial);
    void begin(unsigned long baudrate);
    void setBaudrate(unsigned long baudrate);
    bool processInput(rpc::Frame& out_frame);
    bool sendFrame(uint16_t command_id, const uint8_t* payload, size_t length);
    bool retransmitLastFrame();
    void reset();
    void flush();
    
    // Error handling
    void clearError();
    void clearOverflow();
    rpc::FrameParser::Error getLastError() const;

   private:
    Stream& _stream;
    HardwareSerial* _hw_serial;
    rpc::FrameParser _parser;
    uint8_t _tx_buffer[rpc::K_MAX_FRAME_BUFFER_SIZE];
    uint8_t _rx_buffer[rpc::K_MAX_FRAME_BUFFER_SIZE]; // Only if needed by parser, parser might own it
  };

  BridgeTransport _transport;
  const uint8_t* _shared_secret;
  size_t _shared_secret_len;

  // RX/TX State
  rpc::Frame _rx_frame; // Current frame being processed
  
  // ACK / Retransmission Logic
  static constexpr uint16_t kAckTimeoutMs = 200;
  static constexpr uint8_t kMaxAckRetries = 3;
  
  bool _awaiting_ack;
  uint16_t _last_command_id;
  uint8_t _retry_count;
  unsigned long _last_send_millis;
  uint16_t _ack_timeout_ms;
  uint8_t _ack_retry_limit;
  uint32_t _response_timeout_ms;

  // Callbacks
  CommandHandler _command_handler;
  DigitalReadHandler _digital_read_handler;
  AnalogReadHandler _analog_read_handler;
  GetFreeMemoryHandler _get_free_memory_handler;
  StatusHandler _status_handler;

  // Pending TX Queue (for when awaiting ACK)
  static constexpr uint8_t kMaxPendingTxFrames = 4;
  
  // Replacement for std::array<uint8_t, size> to avoid STL dependency
  struct PendingTxFrame {
      uint16_t command_id;
      uint16_t payload_length;
      uint8_t payload[rpc::MAX_PAYLOAD_SIZE];
  };
  
  PendingTxFrame _pending_tx_frames[kMaxPendingTxFrames];
  uint8_t _pending_tx_head;
  uint8_t _pending_tx_count;

  // Handshake State
  bool _synchronized;
  uint8_t _scratch_payload[rpc::MAX_PAYLOAD_SIZE]; // Temp buffer for constructing payloads

#if BRIDGE_DEBUG_FRAMES
  FrameDebugSnapshot _tx_debug;
#endif

  // Internal Helpers
  void dispatch(const rpc::Frame& frame);
  void _handleSystemCommand(const rpc::Frame& frame);
  void _handleGpioCommand(const rpc::Frame& frame);
  void _handleConsoleCommand(const rpc::Frame& frame);
  
  bool _sendFrame(uint16_t command_id, const uint8_t* payload, size_t length);
  bool _sendFrameImmediate(uint16_t command_id, const uint8_t* payload, size_t length);
  
  bool _requiresAck(uint16_t command_id) const;
  void _handleAck(uint16_t command_id);
  void _handleMalformed(uint16_t command_id);
  void _retransmitLastFrame();
  void _processAckTimeout();
  
  void _resetLinkState();
  void _computeHandshakeTag(const uint8_t* nonce, size_t nonce_len, uint8_t* out_tag);
  void _applyTimingConfig(const uint8_t* payload, size_t length);

  bool _enqueuePendingTx(uint16_t command_id, const uint8_t* payload, size_t length);
  bool _dequeuePendingTx(PendingTxFrame& frame);
  void _flushPendingTxQueue();
  void _clearPendingTxQueue();
  void _clearAckState();
};

/*
 * ConsoleClass
 * Provides a Serial-like interface over the Bridge.
 */
class ConsoleClass : public Stream {
 public:
  ConsoleClass();

  void begin();
  void end();

  // Stream implementation
  virtual int available(void);
  virtual int peek(void);
  virtual int read(void);
  virtual void flush(void);
  virtual size_t write(uint8_t c);
  virtual size_t write(const uint8_t *buffer, size_t size);
  
  // Bridge hook
  void _push(const uint8_t* data, size_t len);

  // Allow C++ style bool check "if (Console)"
  operator bool();

 private:
  static constexpr size_t kRxBufferSize = 64;
  uint8_t _rx_buffer[kRxBufferSize];
  uint16_t _rx_head;
  uint16_t _rx_tail;
  bool _connected;
};

/*
 * ProcessClass
 * Launches and controls processes on the Linux side.
 */
class ProcessClass {
 public:
  ProcessClass();
  
  // Basic API
  void run(const char* command);     // Blocking run (wait for completion)
  void runAsync(const char* command); // Fire and forget (or wait for async output)
  void poll(int pid);                 // Request output for a PID
  void kill(int pid);                 // Kill a PID

  // Internal dispatch
  void handleResponse(const rpc::Frame& frame);

  // Callbacks
  typedef void (*ProcessRunHandler)(rpc::StatusCode status, const uint8_t* stdout_data, uint16_t stdout_len, const uint8_t* stderr_data, uint16_t stderr_len);
  typedef void (*ProcessRunAsyncHandler)(int pid);
  typedef void (*ProcessPollHandler)(rpc::StatusCode status, uint8_t running, const uint8_t* stdout_data, uint16_t stdout_len, const uint8_t* stderr_data, uint16_t stderr_len);

  void onProcessRunResponse(ProcessRunHandler handler);
  void onProcessRunAsyncResponse(ProcessRunAsyncHandler handler);
  void onProcessPollResponse(ProcessPollHandler handler);

 private:
  static constexpr uint8_t kMaxPendingProcessPolls = 8;
  uint16_t _pending_process_pids[kMaxPendingProcessPolls];
  uint8_t _pending_process_poll_head;
  uint8_t _pending_process_poll_count;

  ProcessRunHandler _process_run_handler;
  ProcessPollHandler _process_poll_handler;
  ProcessRunAsyncHandler _process_run_async_handler;

  bool _pushPendingProcessPid(uint16_t pid);
  uint16_t _popPendingProcessPid();
  
  // Helper for backpressure handling
  bool _sendWithRetry(rpc::CommandId cmd, const uint8_t* payload, size_t len);
};

/*
 * DataStoreClass
 * Key-Value store interface.
 */
class DataStoreClass {
  // Simplistic implementation for now, mirroring basic Bridge.put/get
 public:
  void put(const char* key, const char* value);
  // Get is async in this protocol, so we request it and provide a callback
  void get(const char* key); 
  
  typedef void (*DataStoreGetHandler)(const char* key, const char* value);
  void onGet(DataStoreGetHandler handler);
  
  void handleResponse(const rpc::Frame& frame);
 
 private:
  DataStoreGetHandler _get_handler;
};

/*
 * FileSystemClass
 * Basic file operations.
 */
class FileSystemClass {
 public:
  // Write content to a file. Mode can be "w" (overwrite) or "a" (append).
  void write(const char* path, const char* content, const char* mode = "w");
  void read(const char* path); // Request file content

  typedef void (*FileReadHandler)(const char* path, const uint8_t* content, uint16_t len);
  void onRead(FileReadHandler handler);

  void handleResponse(const rpc::Frame& frame);

 private:
  FileReadHandler _read_handler;
};

/*
 * MailboxClass
 * Message passing interface.
 */
class MailboxClass {
 public:
  void write(const char* message);
  void read(); // Check for messages

  typedef void (*MailboxReadHandler)(const char* message, uint16_t len);
  void onRead(MailboxReadHandler handler);
  
  void handleResponse(const rpc::Frame& frame);

 private:
  MailboxReadHandler _read_handler;
};

// Global Instances
extern BridgeClass Bridge;
extern ConsoleClass Console;
extern ProcessClass Process;
extern DataStoreClass DataStore;
extern FileSystemClass FileSystem;
extern MailboxClass Mailbox;

#endif // BRIDGE_H_

/**
 * @file Bridge.h
 * @brief Librería principal del Arduino Yun Bridge v2 (Runtime Secret Support + Console Fix).
 */
#ifndef BRIDGE_V2_H
#define BRIDGE_V2_H

#if defined(ARDUINO)
#include <Arduino.h>
#include <Print.h>
#else
#include "arduino/ArduinoCompat.h"
#endif
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "arduino/BufferView.h"

class HardwareSerial;

constexpr uint8_t BRIDGE_DATASTORE_PENDING_MAX = 1;
constexpr size_t BRIDGE_DATASTORE_KEY_MAX_LEN = 48;
constexpr uint8_t BRIDGE_PROCESS_PENDING_MAX = 2;
constexpr uint8_t BRIDGE_TX_QUEUE_MAX = 1;

#ifndef BRIDGE_FIRMWARE_VERSION_MAJOR
#define BRIDGE_FIRMWARE_VERSION_MAJOR 2
#endif

#ifndef BRIDGE_FIRMWARE_VERSION_MINOR
#define BRIDGE_FIRMWARE_VERSION_MINOR 1
#endif

#ifndef BRIDGE_DEBUG_IO
#define BRIDGE_DEBUG_IO 0
#endif

#ifndef BRIDGE_DEBUG_FRAMES
#define BRIDGE_DEBUG_FRAMES 1
#endif

#ifndef CONSOLE_RX_BUFFER_SIZE
#define CONSOLE_RX_BUFFER_SIZE 32
#endif

#ifndef CONSOLE_TX_BUFFER_SIZE
#define CONSOLE_TX_BUFFER_SIZE 64
#endif

// CORRECCIÓN: Agregamos las definiciones de watermark faltantes
#ifndef CONSOLE_BUFFER_HIGH_WATER
#define CONSOLE_BUFFER_HIGH_WATER 24
#endif
#ifndef CONSOLE_BUFFER_LOW_WATER
#define CONSOLE_BUFFER_LOW_WATER 8
#endif

static_assert(
  CONSOLE_RX_BUFFER_SIZE > 0,
  "Console RX buffer size must be greater than zero"
);
static_assert(
  CONSOLE_TX_BUFFER_SIZE > 0,
  "Console TX buffer size must be greater than zero"
);

class ConsoleClass : public Print {
 public:
  ConsoleClass();
  void begin();
  virtual size_t write(uint8_t c);
  virtual size_t write(const uint8_t* buffer, size_t size);
  int available();
  int read();
  int peek();
  void flush();
  explicit operator bool() const { return _begun; }
  void _push(BufferView chunk);

 private:
  bool _begun;
  uint8_t _rx_buffer[CONSOLE_RX_BUFFER_SIZE];
  uint8_t _tx_buffer[CONSOLE_TX_BUFFER_SIZE];
    volatile size_t _rx_buffer_head;
    volatile size_t _rx_buffer_tail;
    size_t _tx_buffer_pos;
  
  // CORRECCIÓN: Agregamos la variable miembro faltante
  bool _xoff_sent;
};

class DataStoreClass {
 public:
  DataStoreClass();
  void put(const char* key, const char* value);
  void requestGet(const char* key);
};

class MailboxClass {
 public:
  MailboxClass();
  void send(const char* message);
  void send(const uint8_t* data, size_t length);
  void requestRead();
  void requestAvailable();
};

class FileSystemClass {
 public:
  void write(const char* filePath, const uint8_t* data, size_t length);
  void remove(const char* filePath);
};

class ProcessClass {
 public:
  ProcessClass();
  void kill(int pid);
};

class BridgeClass {
 public:
  explicit BridgeClass(HardwareSerial& serial);
  BridgeClass(Stream& stream);

  static constexpr size_t kMaxFilePathLength = 255;

  // begin acepta baudrate y secreto (permite longitud explícita)
  void begin(
      unsigned long baudrate = 115200,
      const char* secret = nullptr,
      size_t secret_len = 0);
  
  void process();
  void flushStream();

  typedef void (*MailboxHandler)(const uint8_t* buffer, size_t size);
  void onMailboxMessage(MailboxHandler handler);

  typedef void (*MailboxAvailableHandler)(uint8_t available_count);
  void onMailboxAvailableResponse(MailboxAvailableHandler handler);

  typedef void (*CommandHandler)(const rpc::Frame& frame);
  void onCommand(CommandHandler handler);

  typedef void (*DataStoreGetHandler)(const char* key,
                                     const uint8_t* value,
                                     uint8_t length);
  void onDataStoreGetResponse(DataStoreGetHandler handler);

  typedef void (*DigitalReadHandler)(int value);
  void onDigitalReadResponse(DigitalReadHandler handler);

  typedef void (*AnalogReadHandler)(int value);
  void onAnalogReadResponse(AnalogReadHandler handler);

  typedef void (*ProcessRunHandler)(rpc::StatusCode status,
                                   const uint8_t* stdout_data,
                                   uint16_t stdout_len,
                                   const uint8_t* stderr_data,
                                   uint16_t stderr_len);
  void onProcessRunResponse(ProcessRunHandler handler);

  typedef void (*ProcessPollHandler)(rpc::StatusCode status, uint8_t exit_code, const uint8_t* stdout_data, uint16_t stdout_len, const uint8_t* stderr_data, uint16_t stderr_len);
  void onProcessPollResponse(ProcessPollHandler handler);

  typedef void (*ProcessRunAsyncHandler)(int pid);
  void onProcessRunAsyncResponse(ProcessRunAsyncHandler handler);

  typedef void (*FileSystemReadHandler)(const uint8_t* content, uint16_t length);
  void onFileSystemReadResponse(FileSystemReadHandler handler);

  typedef void (*GetFreeMemoryHandler)(uint16_t free_memory);
  void onGetFreeMemoryResponse(GetFreeMemoryHandler handler);

  typedef void (*StatusHandler)(rpc::StatusCode status_code, const uint8_t* payload,
                                uint16_t length);
  void onStatus(StatusHandler handler);

  void pinMode(uint8_t pin, uint8_t mode);
  void digitalWrite(uint8_t pin, uint8_t value);
  void analogWrite(uint8_t pin, int value);
  void requestDigitalRead(uint8_t pin);
  void requestAnalogRead(uint8_t pin);

  void requestProcessRun(const char* command);
  void requestProcessRunAsync(const char* command);
  void requestProcessPoll(int pid);

  void requestFileSystemRead(const char* filePath);
  void requestGetFreeMemory();

  bool sendFrame(rpc::CommandId command_id,
                 BufferView payload = BufferView());
  bool sendFrame(rpc::StatusCode status_code,
                 BufferView payload = BufferView());

  struct FrameDebugSnapshot {
    uint16_t command_id;
    uint16_t payload_length;
    uint16_t crc;
    uint16_t raw_length;
    uint16_t cobs_length;
    uint16_t expected_serial_bytes;
    uint16_t last_write_return;
    uint16_t last_shortfall;
    uint32_t tx_count;
    uint32_t write_shortfall_events;
    uint32_t build_failures;
  };

#if BRIDGE_DEBUG_FRAMES
  FrameDebugSnapshot getTxDebugSnapshot() const;
  void resetTxDebugStats();
#endif

 private:
  Stream& _stream;
  HardwareSerial* _hardware_serial;
  
  const uint8_t* _shared_secret;
  size_t _shared_secret_len;

  rpc::FrameParser _parser;
  rpc::FrameBuilder _builder;
  rpc::Frame _rx_frame;

  CommandHandler _command_handler;
  DataStoreGetHandler _datastore_get_handler;
  MailboxHandler _mailbox_handler;
  MailboxAvailableHandler _mailbox_available_handler;
  DigitalReadHandler _digital_read_handler;
  AnalogReadHandler _analog_read_handler;
  ProcessRunHandler _process_run_handler;
  ProcessPollHandler _process_poll_handler;
  ProcessRunAsyncHandler _process_run_async_handler;
  FileSystemReadHandler _file_system_read_handler;
  GetFreeMemoryHandler _get_free_memory_handler;
  StatusHandler _status_handler;

  static constexpr uint8_t kMaxPendingDatastore = BRIDGE_DATASTORE_PENDING_MAX;
  static constexpr size_t kMaxDatastoreKeyLength = BRIDGE_DATASTORE_KEY_MAX_LEN;
  char _pending_datastore_keys[kMaxPendingDatastore][kMaxDatastoreKeyLength + 1];
  uint8_t _pending_datastore_key_lengths[kMaxPendingDatastore];
  uint8_t _pending_datastore_head;
  uint8_t _pending_datastore_count;

  static constexpr uint8_t kMaxPendingProcessPolls = BRIDGE_PROCESS_PENDING_MAX;
  uint16_t _pending_process_pids[kMaxPendingProcessPolls];
  uint8_t _pending_process_poll_head;
  uint8_t _pending_process_poll_count;

  struct PendingTxFrame {
    uint16_t command_id;
    uint16_t payload_length;
    uint8_t payload[rpc::MAX_PAYLOAD_SIZE];
  };

  static constexpr uint8_t kMaxPendingTxFrames = BRIDGE_TX_QUEUE_MAX;
  PendingTxFrame _pending_tx_frames[kMaxPendingTxFrames];
  uint8_t _pending_tx_head;
  uint8_t _pending_tx_count;

#if BRIDGE_DEBUG_FRAMES
  FrameDebugSnapshot _tx_debug;
#endif

  bool _awaiting_ack;
  uint16_t _last_command_id;
  uint8_t _last_cobs_frame[rpc::COBS_BUFFER_SIZE];
  uint16_t _last_cobs_length;
  uint8_t _scratch_payload[rpc::MAX_PAYLOAD_SIZE];
  uint8_t _raw_frame_buffer[rpc::MAX_RAW_FRAME_SIZE];
  uint8_t _retry_count;
  unsigned long _last_send_millis;
  unsigned long _ack_timeout_ms;
  uint8_t _ack_retry_limit;
  uint32_t _response_timeout_ms;

  static constexpr uint8_t kMaxAckRetries = 3;
  static constexpr unsigned long kAckTimeoutMs = 75;

  bool _requiresAck(uint16_t command_id) const;
  void _recordLastFrame(uint16_t command_id, const uint8_t* cobs_frame,
                        size_t cobs_len);
  void _clearAckState();
  void _handleAck(uint16_t command_id);
  void _handleMalformed(uint16_t command_id);
  void _retransmitLastFrame();
  void _processAckTimeout();
  void _resetLinkState();
  void _flushPendingTxQueue();
  void _clearPendingTxQueue();
  bool _enqueuePendingTx(uint16_t command_id, BufferView payload);
  bool _dequeuePendingTx(PendingTxFrame& frame);
  bool _sendFrame(uint16_t command_id, BufferView payload);
  bool _sendFrameImmediate(uint16_t command_id, BufferView payload);
  size_t _writeFrameBytes(const uint8_t* data, size_t length);

  bool _trackPendingDatastoreKey(const char* key);
  const char* _popPendingDatastoreKey();
  bool _pushPendingProcessPid(uint16_t pid);
  uint16_t _popPendingProcessPid();
  friend class DataStoreClass;

  void dispatch(const rpc::Frame& frame);
  void _emitStatus(rpc::StatusCode status_code, const char* message);
  void _applyTimingConfig(BufferView payload);

  void _computeHandshakeTag(const uint8_t* nonce, size_t nonce_len, uint8_t* out_tag);
};

extern BridgeClass Bridge;
extern ConsoleClass Console;
extern DataStoreClass DataStore;
extern MailboxClass Mailbox;
extern FileSystemClass FileSystem;
extern ProcessClass Process;

#endif
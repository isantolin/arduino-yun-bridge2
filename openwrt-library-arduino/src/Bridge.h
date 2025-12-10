/*
 * This file is part of Arduino Yun Ecosystem v2.
 * Copyright (C) 2025 Ignacio Santolin and contributors
 */
#ifndef BRIDGE_H
#define BRIDGE_H

#include <Arduino.h>
#include <Stream.h>
#include <stdint.h>

#include "arduino/ArduinoCompat.h"
#include "arduino/BufferView.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"

// Feature flags
#define BRIDGE_DEBUG_FRAMES 0
#define BRIDGE_DEBUG_IO 0

#ifndef BRIDGE_ENABLE_WATCHDOG
#define BRIDGE_ENABLE_WATCHDOG 1
#endif

// Firmware version definition
#define BRIDGE_FIRMWARE_VERSION_MAJOR 2
#define BRIDGE_FIRMWARE_VERSION_MINOR 0

class BridgeClass {
 public:
  static constexpr unsigned long kAckTimeoutMs =
      rpc::RPC_HANDSHAKE_ACK_TIMEOUT_MIN_MS;
  static constexpr uint8_t kMaxAckRetries =
      rpc::RPC_HANDSHAKE_RETRY_LIMIT_MAX;
  
  // Limites de colas internas
  static constexpr uint8_t kMaxPendingDatastore = 4;
  static constexpr uint8_t kMaxDatastoreKeyLength = 32;
  static constexpr uint8_t kMaxPendingProcessPolls = 4;
  static constexpr uint8_t kMaxPendingTxFrames = 4;
  static constexpr uint8_t kMaxFilePathLength = 64;

  // --- Callbacks ---
  using CommandHandler = void (*)(const rpc::Frame&);
  using DataStoreGetHandler = void (*)(const char* key, const uint8_t* value,
                                       uint8_t len);
  using MailboxHandler = void (*)(const uint8_t* data, size_t len);
  using MailboxAvailableHandler = void (*)(uint8_t count);
  using DigitalReadHandler = void (*)(int value);
  using AnalogReadHandler = void (*)(int value);
  using ProcessRunHandler = void (*)(rpc::StatusCode status,
                                     const uint8_t* stdout_data,
                                     uint16_t stdout_len,
                                     const uint8_t* stderr_data,
                                     uint16_t stderr_len);
  using ProcessPollHandler = void (*)(rpc::StatusCode status, uint8_t exit_code,
                                      const uint8_t* stdout_data,
                                      uint16_t stdout_len,
                                      const uint8_t* stderr_data,
                                      uint16_t stderr_len);
  using ProcessRunAsyncHandler = void (*)(int pid);
  using FileSystemReadHandler = void (*)(const uint8_t* data, uint16_t len);
  using GetFreeMemoryHandler = void (*)(uint16_t free_mem);
  using StatusHandler = void (*)(rpc::StatusCode code, const uint8_t* payload,
                                 uint16_t len);

  explicit BridgeClass(HardwareSerial& serial);
  explicit BridgeClass(Stream& stream);

  // --- API Pública ---
  void begin(unsigned long baudrate = 250000, const char* secret = nullptr,
             size_t secret_len = 0);
  void process();
  void flushStream(); // Flush subyacente (bloqueante)

  // Envia un comando. Retorna true si se encolo/envio, false si fallo (buffer lleno).
  bool sendFrame(rpc::CommandId command_id, BufferView payload = BufferView());
  bool sendFrame(rpc::StatusCode status_code, BufferView payload = BufferView());

  // --- Handler Registration ---
  void onCommand(CommandHandler handler);
  void onDataStoreGetResponse(DataStoreGetHandler handler);
  void onMailboxMessage(MailboxHandler handler);
  void onMailboxAvailableResponse(MailboxAvailableHandler handler);
  void onDigitalReadResponse(DigitalReadHandler handler);
  void onAnalogReadResponse(AnalogReadHandler handler);
  void onProcessRunResponse(ProcessRunHandler handler);
  void onProcessPollResponse(ProcessPollHandler handler);
  void onProcessRunAsyncResponse(ProcessRunAsyncHandler handler);
  void onFileSystemReadResponse(FileSystemReadHandler handler);
  void onGetFreeMemoryResponse(GetFreeMemoryHandler handler);
  void onStatus(StatusHandler handler);

  // --- Métodos Internos para Clases Satélite ---
  // (No usar desde sketch de usuario)
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

  // Gestión de cola de Datastore (para correlacionar respuestas)
  bool _trackPendingDatastoreKey(const char* key);
  const char* _popPendingDatastoreKey();

  // Gestión de cola de Procesos
  bool _pushPendingProcessPid(uint16_t pid);
  uint16_t _popPendingProcessPid();

  void _emitStatus(rpc::StatusCode status_code, const char* message = nullptr);

  // [OPTIMIZACIÓN] Acceso al buffer compartido ("Scratchpad")
  // Permite a las clases satélite construir payloads sin usar el Stack.
  // IMPORTANTE: El buffer solo es válido hasta la siguiente llamada a sendFrame().
  uint8_t* getScratchBuffer() { return _scratch_payload; }

#if BRIDGE_DEBUG_FRAMES
  struct FrameDebugSnapshot {
    uint16_t tx_count;
    uint16_t build_failures;
    uint16_t write_shortfall_events;
    uint16_t last_shortfall;
    uint16_t last_write_return;
    uint16_t expected_serial_bytes;
    uint16_t command_id;
    uint16_t payload_length;
    uint16_t raw_length;
    uint16_t cobs_length;
    uint16_t crc;
  };
  FrameDebugSnapshot getTxDebugSnapshot() const;
  void resetTxDebugStats();
#endif

 private:
  Stream& _stream;
  HardwareSerial* _hardware_serial;
  const uint8_t* _shared_secret;
  size_t _shared_secret_len;

  // Buffers
  rpc::FrameParser _parser;
  rpc::FrameBuilder _builder;
  rpc::Frame _rx_frame;
  
  // Buffer compartido para construcción temporal de frames y payloads.
  // Evita allocs en stack en Peripherals.cpp.
  uint8_t _scratch_payload[rpc::MAX_PAYLOAD_SIZE];
  
  // Buffer de serialización final (Header + Payload + CRC)
  uint8_t _raw_frame_buffer[rpc::MAX_RAW_FRAME_SIZE];

  // Callbacks
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

  // Estado Datastore
  char _pending_datastore_keys[kMaxPendingDatastore][kMaxDatastoreKeyLength + 1];
  uint8_t _pending_datastore_key_lengths[kMaxPendingDatastore];
  uint8_t _pending_datastore_head;
  uint8_t _pending_datastore_count;

  // Estado Procesos
  uint16_t _pending_process_pids[kMaxPendingProcessPolls];
  uint8_t _pending_process_poll_head;
  uint8_t _pending_process_poll_count;

  // Estado Transmisión (Stop-and-Wait ARQ simple)
  struct PendingTxFrame {
    uint16_t command_id;
    uint16_t payload_length;
    uint8_t payload[rpc::MAX_PAYLOAD_SIZE];
  };
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
  uint8_t _retry_count;
  unsigned long _last_send_millis;

  // Configuración de tiempos (negociable via Handshake)
  uint16_t _ack_timeout_ms;
  uint8_t _ack_retry_limit;
  uint32_t _response_timeout_ms;

  // Métodos Privados
  void dispatch(const rpc::Frame& frame);
  bool _sendFrame(uint16_t command_id, BufferView payload);
  bool _sendFrameImmediate(uint16_t command_id, BufferView payload);
  bool _requiresAck(uint16_t command_id) const;
  void _recordLastFrame(uint16_t command_id, const uint8_t* cobs_frame, size_t cobs_len);
  void _clearAckState();
  void _handleAck(uint16_t command_id);
  void _handleMalformed(uint16_t command_id);
  void _retransmitLastFrame();
  void _processAckTimeout();
  void _resetLinkState();
  void _computeHandshakeTag(const uint8_t* nonce, size_t nonce_len, uint8_t* out_tag);
  void _applyTimingConfig(BufferView payload);
  
  // Cola TX
  bool _enqueuePendingTx(uint16_t command_id, BufferView payload);
  bool _dequeuePendingTx(PendingTxFrame& frame);
  void _flushPendingTxQueue();
  void _clearPendingTxQueue();

  size_t _writeFrameBytes(const uint8_t* data, size_t length);
};

extern BridgeClass Bridge;

class ConsoleClass : public Stream {
 public:
  ConsoleClass();
  void begin();
  void end();

  // Stream implementation
  virtual int available();
  virtual int read();
  virtual int peek();
  virtual void flush(); // Envía buffer local si hay datos

  // Print implementation override
  virtual size_t write(uint8_t c);
  virtual size_t write(const uint8_t* buffer, size_t size);

  // Internal
  void _push(BufferView data);

 private:
  static constexpr size_t kRxBufferSize = 64;
  static constexpr size_t kTxBufferSize = rpc::MAX_PAYLOAD_SIZE; 

  uint8_t _rx_buffer[kRxBufferSize];
  uint16_t _rx_head;
  uint16_t _rx_tail;

  uint8_t _tx_buffer[kTxBufferSize];
  uint16_t _tx_head;

  void _tryFlush();
};

extern ConsoleClass Console;

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
  void write(const char* filePath, const char* contents) {
    write(filePath, (const uint8_t*)contents, strlen(contents));
  }
  void write(const char* filePath, const uint8_t* data, size_t length);
  void read(const char* filePath) { Bridge.requestFileSystemRead(filePath); }
  void remove(const char* filePath);
};

extern FileSystemClass FileSystem;

class ProcessClass {
 public:
  ProcessClass();
  void run(const char* command) { Bridge.requestProcessRun(command); }
  void runAsync(const char* command) { Bridge.requestProcessRunAsync(command); }
  void kill(int pid);
};

extern ProcessClass Process;

#endif  // BRIDGE_H
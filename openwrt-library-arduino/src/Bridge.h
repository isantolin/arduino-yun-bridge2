/**
 * @file Bridge.h
 * @brief Librería principal del Arduino Yun Bridge v2.
 * @details Esta librería facilita la comunicación RPC (Remote Procedure Call)
 * entre el microcontrolador Arduino y el procesador Linux en placas como
 * el Arduino Yún. Esta versión impone un modelo de programación puramente asíncrono.
 *
 * Copyright (C) 2025 Ignacio Santolin and contributors
 * Licenciado bajo la GNU General Public License, v3 o posterior.
 */
#ifndef BRIDGE_V2_H
#define BRIDGE_V2_H

#include <Arduino.h>

#include "Print.h"
#include "arduino/BridgeSecret.h"
#include "protocol/rpc_frame.h"

class HardwareSerial;

// Adjusted resource limits to keep SRAM usage below 2.5 KB on ATmega32u4.
constexpr uint8_t BRIDGE_DATASTORE_PENDING_MAX = 4;
constexpr size_t BRIDGE_DATASTORE_KEY_MAX_LEN = 96;
constexpr uint8_t BRIDGE_PROCESS_PENDING_MAX = 8;
constexpr uint8_t BRIDGE_TX_QUEUE_MAX = 3;

#ifndef BRIDGE_FIRMWARE_VERSION_MAJOR
#define BRIDGE_FIRMWARE_VERSION_MAJOR 2
#endif

#ifndef BRIDGE_FIRMWARE_VERSION_MINOR
#define BRIDGE_FIRMWARE_VERSION_MINOR 0
#endif

#ifndef BRIDGE_DEBUG_IO
#define BRIDGE_DEBUG_IO 0
#endif

#ifndef BRIDGE_DEBUG_FRAMES
#define BRIDGE_DEBUG_FRAMES 1
#endif

// --- Constantes de la Consola ---
// Ajustar los límites de agua para que respiren sobre un buffer real.
// El cabezal circular necesita al menos un byte libre, por lo que un
// tamaño de 64 bytes permite aplicar backpressure antes de saturar.
#define CONSOLE_RX_BUFFER_SIZE 64
#define CONSOLE_BUFFER_HIGH_WATER 48
#define CONSOLE_BUFFER_LOW_WATER 16

/**
 * @class ConsoleClass
 * @brief Permite enviar y recibir datos de texto a/desde la consola de Linux.
 */
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
  void _push(const uint8_t* buffer, size_t size);

 private:
  bool _begun;
  uint8_t _rx_buffer[CONSOLE_RX_BUFFER_SIZE];
  volatile uint16_t _rx_buffer_head;
  volatile uint16_t _rx_buffer_tail;
  bool _xoff_sent;
};

/**
 * @class DataStoreClass
 * @brief Proporciona un almacén de clave-valor en el lado de Linux.
 */
class DataStoreClass {
 public:
  DataStoreClass();
  void put(const char* key, const char* value);
  void requestGet(const char* key);
};

/**
 * @class MailboxClass
 * @brief Permite el intercambio de mensajes entre Arduino y Linux.
 */
class MailboxClass {
 public:
  MailboxClass();
  void send(const char* message);
  void send(const uint8_t* data, size_t length);
  void requestRead();
  void requestAvailable();
};

/**
 * @class FileSystemClass
 * @brief Permite al sketch interactuar con el sistema de ficheros de Linux.
 */
class FileSystemClass {
 public:
  void write(const char* filePath, const uint8_t* data, size_t length);
  void remove(const char* filePath);
};

/**
 * @class ProcessClass
 * @brief Permite al sketch ejecutar comandos y procesos en Linux.
 */
class ProcessClass {
 public:
  ProcessClass();
  void kill(int pid);
};

/**
 * @class BridgeClass
 * @brief Clase principal que gestiona la comunicación RPC.
 */
class BridgeClass {
 public:
  explicit BridgeClass(HardwareSerial& serial);
  BridgeClass(Stream& stream);
  void begin();
  void process();
  void flushStream();

  // --- Manejadores de Respuestas (Callbacks) ---
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

  typedef void (*ProcessRunHandler)(uint8_t status,
                                   const uint8_t* stdout_data,
                                   uint16_t stdout_len,
                                   const uint8_t* stderr_data,
                                   uint16_t stderr_len);
  void onProcessRunResponse(ProcessRunHandler handler);

  typedef void (*ProcessPollHandler)(uint8_t status, uint8_t exit_code, const uint8_t* stdout_data, uint16_t stdout_len, const uint8_t* stderr_data, uint16_t stderr_len);
  void onProcessPollResponse(ProcessPollHandler handler);

  typedef void (*ProcessRunAsyncHandler)(int pid);
  void onProcessRunAsyncResponse(ProcessRunAsyncHandler handler);

  typedef void (*FileSystemReadHandler)(const uint8_t* content, uint16_t length);
  void onFileSystemReadResponse(FileSystemReadHandler handler);

  typedef void (*GetFreeMemoryHandler)(uint16_t free_memory);
  void onGetFreeMemoryResponse(GetFreeMemoryHandler handler);

  typedef void (*StatusHandler)(uint8_t status_code, const uint8_t* payload,
                                uint16_t length);
  void onStatus(StatusHandler handler);

  // --- API de Control de Pines (No Bloqueante) ---
  void pinMode(uint8_t pin, uint8_t mode);
  void digitalWrite(uint8_t pin, uint8_t value);
  void analogWrite(uint8_t pin, int value);
  void requestDigitalRead(uint8_t pin);
  void requestAnalogRead(uint8_t pin);

  // --- API de Procesos (No Bloqueante) ---
  void requestProcessRun(const char* command);
  void requestProcessRunAsync(const char* command);
  void requestProcessPoll(int pid);

  // --- API de Sistema de Ficheros (No Bloqueante) ---
  void requestFileSystemRead(const char* filePath);
  void requestGetFreeMemory();

  // --- Métodos de Bajo Nivel ---
  bool sendFrame(uint16_t command_id, const uint8_t* payload,
                 uint16_t payload_len);

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
  rpc::FrameParser _parser;
  rpc::FrameBuilder _builder;
  rpc::Frame _rx_frame;  // Reuse a single frame buffer to save stack space.

  // Punteros a las funciones de callback
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
  uint8_t _last_raw_frame[rpc::MAX_RAW_FRAME_SIZE];
  uint16_t _last_raw_length;
  uint8_t _last_cobs_frame[rpc::COBS_BUFFER_SIZE];
  uint16_t _last_cobs_length;
  uint8_t _retry_count;
  unsigned long _last_send_millis;

  static constexpr uint8_t kMaxAckRetries = 3;
  static constexpr unsigned long kAckTimeoutMs = 75;

  bool _requiresAck(uint16_t command_id) const;
  void _recordLastFrame(uint16_t command_id, const uint8_t* raw_frame,
                        size_t raw_len, const uint8_t* cobs_frame,
                        size_t cobs_len);
  void _clearAckState();
  void _handleAck(uint16_t command_id);
  void _handleMalformed(uint16_t command_id);
  void _retransmitLastFrame();
  void _processAckTimeout();
  void _resetLinkState();
  void _flushPendingTxQueue();
  void _clearPendingTxQueue();
  bool _enqueuePendingTx(uint16_t command_id, const uint8_t* payload,
                         uint16_t payload_len);
  bool _dequeuePendingTx(PendingTxFrame& frame);
  bool _sendFrameImmediate(uint16_t command_id, const uint8_t* payload,
                           uint16_t payload_len);

  void _trackPendingDatastoreKey(const char* key);
  const char* _popPendingDatastoreKey();
  bool _pushPendingProcessPid(uint16_t pid);
  uint16_t _popPendingProcessPid();
  friend class DataStoreClass;

  void dispatch(const rpc::Frame& frame);
  void _emitStatus(uint8_t status_code, const char* message);
};

// --- Instancias Globales ---
extern BridgeClass Bridge;
extern ConsoleClass Console;
extern DataStoreClass DataStore;
extern MailboxClass Mailbox;
extern FileSystemClass FileSystem;
extern ProcessClass Process;

#endif  // BRIDGE_V2_H
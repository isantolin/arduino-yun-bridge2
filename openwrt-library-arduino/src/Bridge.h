/*
 * Bridge.h - Cabeçalho principal para o Ecossistema Arduino Yun v2.
 *
 * Este cabeçalho define a API pública para a biblioteca Bridge e seus componentes.
 * Inclui declarações para BridgeClass, ConsoleClass, ProcessClass e outros.
 *
 * Copyright (c) 2024 Arduino Yun Ecosystem v2
 */

#ifndef BRIDGE_H_
#define BRIDGE_H_

#include <array>
#include "Arduino.h"
#include "Stream.h"
#include "arduino/StringUtils.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"

// Forward declarations de dependências de mock/teste
// Numa build real, estes seriam cabeçalhos da biblioteca padrão ou específicos da plataforma.

// Constantes para configuração
#ifndef BRIDGE_BAUDRATE
#define BRIDGE_BAUDRATE 250000
#endif

namespace rpc {
// Forward declaration
struct Frame;
// Definir tamanho máximo local do frame se não estiver no protocolo
// Payload + Cabeçalho (4) + CRC (4) + Overhead COBS (~1 + len/254) + 2 (0x00 delimitadores)
constexpr size_t K_FRAME_OVERHEAD = 16;
constexpr size_t K_MAX_FRAME_BUFFER_SIZE = MAX_PAYLOAD_SIZE + K_FRAME_OVERHEAD;
}

/*
 * BridgeClass
 * Gere a ligação série para o lado OpenWrt.
 * Lida com enquadramento, despacho e detalhes do protocolo de baixo nível.
 */
class BridgeClass {
 public:
  static constexpr uint8_t kFirmwareVersionMajor = 2;
  static constexpr uint8_t kFirmwareVersionMinor = 0;

  // Construtores
  explicit BridgeClass(HardwareSerial& serial);
  explicit BridgeClass(Stream& stream);

  // Inicialização
  void begin(unsigned long baudrate = BRIDGE_BAUDRATE,
             const char* secret = nullptr, size_t secret_len = 0);
  
  // Tarefa do loop principal - DEVE ser chamada frequentemente
  void process();

  // Envio de frame de baixo nível
  bool sendFrame(rpc::CommandId command_id, const uint8_t* payload = nullptr,
                 size_t length = 0);
  bool sendFrame(rpc::StatusCode status_code, const uint8_t* payload = nullptr,
                 size_t length = 0);

  // Shims da API Arduino
  void put(const char* key, const char* value);
  unsigned int get(const char* key, uint8_t* buff, unsigned int size);

  // Suporte API Depreciada / Legado (mapeada para novo protocolo quando possível)
  void pinMode(uint8_t pin, uint8_t mode);
  void digitalWrite(uint8_t pin, uint8_t value);
  int digitalRead(uint8_t pin);
  void analogWrite(uint8_t pin, int value);
  int analogRead(uint8_t pin);
  
  // Bridge.transfer não é suportado na v2 pois era transferência raw eficiente.
  // Use componentes específicos em vez disso.

  // Tipos de Callback
  typedef void (*CommandHandler)(const rpc::Frame& frame);
  typedef void (*DigitalReadHandler)(uint8_t pin, int value);
  typedef void (*AnalogReadHandler)(uint8_t pin, int value);
  typedef void (*GetFreeMemoryHandler)(uint16_t free_memory);
  typedef void (*StatusHandler)(rpc::StatusCode code, const uint8_t* msg, uint16_t len);

  // Registo para callbacks
  void onCommand(CommandHandler handler);
  void onDigitalReadResponse(DigitalReadHandler handler);
  void onAnalogReadResponse(AnalogReadHandler handler);
  void onGetFreeMemoryResponse(GetFreeMemoryHandler handler);
  void onStatus(StatusHandler handler);
  
  // Auxiliares Internos ou Avançados
  void flushStream(); // Limpar o stream de transporte subjacente
  
  // Métodos de Pedido (MCU -> Linux)
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
  
  // Auxiliar interno para emitir estados facilmente
  void _emitStatus(rpc::StatusCode status_code, const char* message);
  void _emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message);


 private:
  // Camada de Transporte
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
    
    // Tratamento de erros
    void clearError();
    void clearOverflow();
    rpc::FrameParser::Error getLastError() const;

   private:
    Stream& _stream;
    HardwareSerial* _hw_serial;
    rpc::FrameParser _parser;
    uint8_t _tx_buffer[rpc::K_MAX_FRAME_BUFFER_SIZE];
    uint8_t _rx_buffer[rpc::K_MAX_FRAME_BUFFER_SIZE]; // Apenas se necessário pelo parser, o parser pode possuí-lo
  };

  BridgeTransport _transport;
  const uint8_t* _shared_secret;
  size_t _shared_secret_len;

  // Estado RX/TX
  rpc::Frame _rx_frame; // Frame atual a ser processado
  
  // Lógica de ACK / Retransmissão
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

  // Fila de TX Pendente (para quando se aguarda ACK)
  static constexpr uint8_t kMaxPendingTxFrames = 4;
  struct PendingTxFrame {
      uint16_t command_id;
      uint16_t payload_length;
      std::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload;
  };
  PendingTxFrame _pending_tx_frames[kMaxPendingTxFrames];
  uint8_t _pending_tx_head;
  uint8_t _pending_tx_count;

  // Estado do Handshake
  bool _synchronized;
  uint8_t _scratch_payload[rpc::MAX_PAYLOAD_SIZE]; // Buffer temporário para construir payloads

#if BRIDGE_DEBUG_FRAMES
  FrameDebugSnapshot _tx_debug;
#endif

  // Auxiliares Internos
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
 * Fornece uma interface tipo Serial sobre a Bridge.
 */
class ConsoleClass : public Stream {
 public:
  ConsoleClass();

  void begin();
  void end();

  // Implementação Stream
  virtual int available(void);
  virtual int peek(void);
  virtual int read(void);
  virtual void flush(void);
  virtual size_t write(uint8_t c);
  virtual size_t write(const uint8_t *buffer, size_t size);
  
  // Hook da Bridge
  void _push(const uint8_t* data, size_t len);

  // Permitir verificação bool estilo C++ "if (Console)"
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
 * Lança e controla processos no lado Linux.
 */
class ProcessClass {
 public:
  ProcessClass();
  
  // API Básica
  void run(const char* command);     // Execução bloqueante (espera pela conclusão)
  void runAsync(const char* command); // Fire and forget (ou esperar por output assíncrono)
  void poll(int pid);                 // Pedir output para um PID
  void kill(int pid);                 // Matar um PID

  // Despacho Interno
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
  
  // Auxiliar para tratamento de contrapressão
  bool _sendWithRetry(rpc::CommandId cmd, const uint8_t* payload, size_t len);
};

/*
 * DataStoreClass
 * Interface de armazenamento Chave-Valor.
 */
class DataStoreClass {
  // Implementação simplista por agora, espelhando Bridge.put/get básico
 public:
  void put(const char* key, const char* value);
  // Get é assíncrono neste protocolo, por isso pedimos e fornecemos um callback
  void get(const char* key); 
  
  typedef void (*DataStoreGetHandler)(const char* key, const char* value);
  void onGet(DataStoreGetHandler handler);
  
  void handleResponse(const rpc::Frame& frame);
 
 private:
  DataStoreGetHandler _get_handler;
};

/*
 * FileSystemClass
 * Operações básicas de ficheiros.
 */
class FileSystemClass {
 public:
  // Escrever conteúdo num ficheiro. Modo pode ser "w" (sobrescrever) ou "a" (acrescentar).
  void write(const char* path, const char* content, const char* mode = "w");
  void read(const char* path); // Pedir conteúdo do ficheiro

  typedef void (*FileReadHandler)(const char* path, const uint8_t* content, uint16_t len);
  void onRead(FileReadHandler handler);

  void handleResponse(const rpc::Frame& frame);

 private:
  FileReadHandler _read_handler;
};

/*
 * MailboxClass
 * Interface de passagem de mensagens.
 */
class MailboxClass {
 public:
  void write(const char* message);
  void read(); // Verificar mensagens

  typedef void (*MailboxReadHandler)(const char* message, uint16_t len);
  void onRead(MailboxReadHandler handler);
  
  void handleResponse(const rpc::Frame& frame);

 private:
  MailboxReadHandler _read_handler;
};

// Instâncias Globais
extern BridgeClass Bridge;
extern ConsoleClass Console;
extern ProcessClass Process;
extern DataStoreClass DataStore;
extern FileSystemClass FileSystem;
extern MailboxClass Mailbox;

#endif // BRIDGE_H_

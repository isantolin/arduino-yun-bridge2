#ifndef BRIDGE_H
#define BRIDGE_H

#include "bridge_config.h"
#include "etl_profile.h"

#include <etl/algorithm.h>
#include <etl/vector.h>
#include <etl/string.h>
#include <etl/cstring.h>
#include <etl/circular_buffer.h>

#include <Arduino.h>
#include <TaskSchedulerDeclarations.h>

#include "protocol/rpc_protocol.h"
#include "protocol/rpc_frame.h"

// Forward declarations for TaskScheduler
class Scheduler;
class Task;

/**
 * @class BridgeClass
 * @brief High-level interface for the Arduino-OpenWrt communication bridge.
 * 
 * This class implements the Arduino-side of the Bridge 2.0 protocol,
 * providing deterministic execution and SIL-2 compatible memory management.
 */
class BridgeClass {
public:
  explicit BridgeClass(Stream& stream);
  explicit BridgeClass(HardwareSerial& serial);

  /**
   * @brief Initialize the bridge.
   * @param baud Baud rate for serial communication (default: 115200).
   * @param secret Shared secret for authentication (optional).
   * @param secret_len Length of the shared secret.
   */
  void begin(unsigned long baud = 115200, const char* secret = nullptr, size_t secret_len = 0);

  /**
   * @brief Process pending bridge tasks. Must be called frequently.
   */
  void process();

  // --- External Component Interfaces ---
  
  // Console
  size_t consoleWrite(uint8_t c);
  size_t consoleWrite(const uint8_t* buffer, size_t size);
  int consoleRead();
  int consoleAvailable();
  int consolePeek();
  void consoleFlush();

  // DataStore
  void datastorePut(const char* key, const char* value);
  void datastoreGet(const char* key, char* value, size_t max_len);

  // FileSystem
  // Implementation note: These are complex operations handled via RPC.

  // Process
  // Implementation note: Handled via RPC.

private:
  // Internal Transport
  struct BridgeTransport {
    Stream& stream;
    explicit BridgeTransport(Stream& s) : stream(s) {}
  } _transport;

  // Authentication
  etl::string<32> _shared_secret;
  bool _authenticated;

  // Task Management (Static allocation)
  Scheduler* _scheduler;
  Task* _serialTask;
  Task* _watchdogTask;

  // RPC State
  uint16_t _last_received_seq;
  bool _awaiting_ack;
  uint32_t _last_tx_time;

  // Buffers (ETL Static containers)
  struct PendingTxFrame {
    uint16_t seq;
    rpc::Status status;
    etl::vector<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload;
  };

  etl::circular_buffer<PendingTxFrame, rpc::RPC_MAX_PENDING_TX_FRAMES> _pending_tx_queue;

  // Internal Callbacks
  static void _serialTaskCallback();
  static void _watchdogTaskCallback();

  // Frame Handling
  void _processIncomingFrame(const rpc::Frame& frame);
  void _sendAck(uint16_t seq);
  void _sendResponse(uint16_t seq, rpc::Status status, const uint8_t* payload, size_t len);
  
  // Infrastructure
  void _flushPendingTxQueue();
  void _clearPendingTxQueue();
  bool _enqueuePendingTx(uint16_t seq, const uint8_t* payload, size_t len);

  // Components State
  struct PendingKey {
    etl::string<32> key;
  };
  etl::circular_buffer<PendingKey, BRIDGE_MAX_PENDING_DATASTORE> _pending_keys;
  etl::circular_buffer<uint16_t, BRIDGE_MAX_PENDING_PROCESS_POLLS> _pending_pids;

  // Console Buffers
  etl::circular_buffer<uint8_t, BRIDGE_CONSOLE_RX_BUFFER_SIZE> _rx_buffer;
  etl::circular_buffer<uint8_t, BRIDGE_CONSOLE_TX_BUFFER_SIZE> _tx_buffer;
  bool _begun;
};

// Global instance (singleton pattern for Arduino compatibility)
extern BridgeClass Bridge;

#endif // BRIDGE_H
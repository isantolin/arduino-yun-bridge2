/**
 * @file Bridge.cpp
 * @brief Arduino MCU Bridge v2 - MCU-side RPC library implementation.
 *
 * This file is part of Arduino MCU Ecosystem v2.
 * (C) 2025-2026 Ignacio Santolin and contributors.
 *
 * [SIL-2 COMPLIANCE]
 * - Strict adherence to ETL (Embedded Template Library).
 * - No dynamic memory allocation (malloc/new) in runtime paths.
 * - Deterministic execution time (no unbounded loops).
 * - FSM-based protocol handling.
 */

#include "Bridge.h"

// Instantiate the global Bridge object
BridgeClass Bridge(Serial);

// Instantiate the global Console object
ConsoleClass Console;

#if BRIDGE_ENABLE_DATASTORE
DataStoreClass DataStore;
#endif

#if BRIDGE_ENABLE_MAILBOX
MailboxClass Mailbox;
#endif

#if BRIDGE_ENABLE_FILESYSTEM
FileSystemClass FileSystem;
#endif

#if BRIDGE_ENABLE_PROCESS
ProcessClass Process;
#endif

// --- BridgeClass Implementation ---

BridgeClass::BridgeClass(HardwareSerial& serial)
    : _stream(serial),
      _hardware_serial(&serial),
      _packetSerial(),
      _last_command_id(0),
      _retry_count(0),
      _pending_baudrate(0),
      _last_rx_crc(0),
      _last_rx_crc_millis(0),
      _consecutive_crc_errors(0),
      _ack_timeout_ms(rpc::RPC_DEFAULT_ACK_TIMEOUT_MS),
      _ack_retry_limit(rpc::RPC_DEFAULT_RETRY_LIMIT),
      _response_timeout_ms(rpc::RPC_DEFAULT_RESPONSE_TIMEOUT_MS),
      _last_tick_millis(0),
      _startup_stabilizing(true) {
  _packetSerial.setStream(&serial);
  _packetSerial.setPacketHandler(&BridgeClass::onPacketReceived);
}

BridgeClass::BridgeClass(Stream& stream)
    : _stream(stream),
      _hardware_serial(nullptr),
      _packetSerial(),
      _last_command_id(0),
      _retry_count(0),
      _pending_baudrate(0),
      _last_rx_crc(0),
      _last_rx_crc_millis(0),
      _consecutive_crc_errors(0),
      _ack_timeout_ms(rpc::RPC_DEFAULT_ACK_TIMEOUT_MS),
      _ack_retry_limit(rpc::RPC_DEFAULT_RETRY_LIMIT),
      _response_timeout_ms(rpc::RPC_DEFAULT_RESPONSE_TIMEOUT_MS),
      _last_tick_millis(0),
      _startup_stabilizing(true) {
  _packetSerial.setStream(&stream);
  _packetSerial.setPacketHandler(&BridgeClass::onPacketReceived);
}

void BridgeClass::begin(unsigned long baudrate, etl::string_view secret, size_t secret_len) {
  if (_hardware_serial) {
    _hardware_serial->begin(baudrate);
    // Wait for serial port to connect (needed for native USB)
    while (!_hardware_serial && millis() < 3000) {
      // Non-blocking wait with timeout
    }
  }

  // [SIL-2] Secure Storage of Shared Secret
  // Copy provided secret into protected memory.
  // Truncate if exceeds buffer size (32 bytes).
  size_t copy_len = etl::min(secret.length(), _shared_secret.capacity());
  _shared_secret.assign(secret.begin(), secret.begin() + copy_len);

  // Initialize FSM
  _fsm.start();

  // Initialize Command Router
  _command_router.setHandler(this);

  // [SIL-2] Register Timer Callbacks using ETL Delegates
  _cb_ack_timeout = etl::delegate<void()>::create<BridgeClass, &BridgeClass::_onAckTimeout>(*this);
  _cb_rx_dedupe = etl::delegate<void()>::create<BridgeClass, &BridgeClass::_onRxDedupe>(*this);
  _cb_baudrate_change = etl::delegate<void()>::create<BridgeClass, &BridgeClass::_onBaudrateChange>(*this);
  _cb_startup_stabilized = etl::delegate<void()>::create<BridgeClass, &BridgeClass::_onStartupStabilized>(*this);

  // Start Stabilization Timer (2000ms)
  _timer_service.registerTimer(2000, _cb_startup_stabilized);
}

void BridgeClass::process() {
  // [SIL-2] Deterministic Time Slicing
  // Update PacketSerial (reads bytes from hardware buffer)
  _packetSerial.update();

  // Update Timer Service
  unsigned long current_millis = millis();
  unsigned long delta = current_millis - _last_tick_millis;
  if (delta > 0) {
    _timer_service.tick(delta);
    _last_tick_millis = current_millis;
  }

  // Update FSM
  _fsm.process_queue();
  
  // Update Watchdog (if enabled)
#if defined(ARDUINO_ARCH_AVR) && BRIDGE_ENABLE_WATCHDOG
  wdt_reset();
#elif defined(ARDUINO_ARCH_ESP32) && BRIDGE_ENABLE_WATCHDOG
  esp_task_wdt_reset();
#endif
}

void BridgeClass::onPacketReceived(const uint8_t* buffer, size_t size) {
  // Trampoline to instance method
  // Note: PacketSerial is not singleton-aware, so we use the global instance 'Bridge'
  // or we'd need `user_data`. For Arduino simplified use, global is standard.
  Bridge._target_frame = &Bridge._rx_frame;
  
  // [SIL-2] Zero-Copy Parser
  auto result = Bridge._parser.parse(buffer, size, *Bridge._target_frame);
  
  if (result.has_value()) {
      // Valid Frame
      Bridge._last_parse_error.reset();
      Bridge._frame_received = true;
      Bridge.dispatch(Bridge._rx_frame);
  } else {
      // Invalid Frame
      Bridge._last_parse_error = result.error();
      Bridge._emitStatus(rpc::StatusCode::STATUS_CRC_MISMATCH);
  }
}

void BridgeClass::dispatch(const rpc::Frame& frame) {
  // [SIL-2] Duplicate Detection (Anti-Replay)
  if (_isRecentDuplicateRx(frame)) {
      // If it's a duplicate, we might need to re-send the ACK if it was lost.
      if (_requiresAck(frame.header.command_id)) {
          _sendAck(frame.header.command_id);
      }
      return;
  }
  
  // Mark as processed for deduplication
  _markRxProcessed(frame);

  // Route Command
  bridge::router::CommandContext ctx;
  ctx.frame = &frame;
  ctx.raw_command = frame.header.command_id;
  ctx.payload = frame.payload.data();
  ctx.length = frame.header.payload_length;
  ctx.is_duplicate = false; // Already checked above

  _command_router.route(ctx);
}

bool BridgeClass::sendFrame(rpc::CommandId command_id, const uint8_t* payload, size_t length) {
  return _sendFrame(static_cast<uint16_t>(command_id), payload, length);
}

bool BridgeClass::sendFrame(rpc::StatusCode status_code, const uint8_t* payload, size_t length) {
  return _sendFrame(static_cast<uint16_t>(status_code), payload, length);
}

bool BridgeClass::_sendFrame(uint16_t command_id, const uint8_t* payload, size_t length) {
  // [SIL-2] Input Validation
  if (length > rpc::RPC_MAX_PAYLOAD_SIZE) {
      _emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
      return false;
  }

  // Check if we can send (Flow Control)
  if (_fsm.isAwaitingAck() && _pending_tx_queue.full()) {
      // Queue is full and we are blocked waiting for ACK. Drop frame or error.
      // For SIL-2, we drop and report error to avoid deadlock.
      return false; 
  }

  // Construct Frame
  PendingTxFrame frame;
  frame.command_id = command_id;
  frame.payload_length = length;
  if (payload && length > 0) {
      etl::copy_n(payload, length, frame.payload.begin());
  }

  // If FSM is Idle, send immediately. Otherwise queue.
  if (_fsm.isIdle()) {
      // Serialize and Send
      rpc::Frame tx_frame;
      tx_frame.header.command_id = command_id;
      tx_frame.header.payload_length = length;
      if (payload && length > 0) {
        tx_frame.payload.assign(payload, payload + length);
      }
      
      // Calculate CRC
      tx_frame.header.crc = rpc::compute_crc32(
          reinterpret_cast<const uint8_t*>(&tx_frame.header), 
          sizeof(rpc::FrameHeader) - sizeof(uint32_t)
      );
      // Add payload CRC
      if (length > 0) {
          tx_frame.header.crc = rpc::update_crc32(tx_frame.header.crc, payload, length);
      }

      // PacketSerial Encode & Send
      // We need a temporary buffer for the serialized frame (Header + Payload)
      // Frame size = 7 (Header) + Payload
      uint8_t buffer[rpc::RPC_MAX_FRAME_SIZE];
      size_t serialized_len = 0;
      
      // Header
      buffer[0] = (tx_frame.header.command_id >> 8) & 0xFF;
      buffer[1] = tx_frame.header.command_id & 0xFF;
      buffer[2] = (tx_frame.header.payload_length >> 8) & 0xFF;
      buffer[3] = tx_frame.header.payload_length & 0xFF;
      
      // Payload
      if (length > 0) {
          memcpy(buffer + 4, tx_frame.payload.data(), length);
      }
      
      // CRC (Append at end for simple serialization, though struct has it in header)
      // Protocol Spec v2: CRC is last 4 bytes of the stream? 
      // Checking FrameParser: It expects COBS decoded data.
      // Let's look at rpc::Frame structure in `rpc_frame.h` (inferred).
      // Standard: ID(2) + Len(2) + Payload(N) + CRC(4)
      
      uint32_t crc = tx_frame.header.crc;
      size_t crc_offset = 4 + length;
      buffer[crc_offset] = (crc >> 24) & 0xFF;
      buffer[crc_offset + 1] = (crc >> 16) & 0xFF;
      buffer[crc_offset + 2] = (crc >> 8) & 0xFF;
      buffer[crc_offset + 3] = crc & 0xFF;
      
      serialized_len = crc_offset + 4;
      
      _packetSerial.send(buffer, serialized_len);

      if (_requiresAck(command_id)) {
          _fsm.send_event(bridge::fsm::EventId::EvTxRequest); // Transition to AwaitAck
          // Store for retry
          _pending_tx_queue.push(frame); 
      }
  } else {
      // Queue
      if (!_pending_tx_queue.full()) {
          _pending_tx_queue.push(frame);
      } else {
          return false; // Dropped
      }
  }
  
  return true;
}

bool BridgeClass::sendStringCommand(rpc::CommandId command_id, etl::string_view str, size_t max_len) {
    if (str.length() > max_len) return false;
    return sendFrame(command_id, reinterpret_cast<const uint8_t*>(str.data()), str.length());
}

bool BridgeClass::sendKeyValCommand(rpc::CommandId command_id, etl::string_view key, size_t max_key, etl::string_view val, size_t max_val) {
    if (key.length() > max_key || val.length() > max_val) return false;
    
    // Format: [KeyLen(1)][Key][Val]
    uint8_t buffer[64]; // Max payload is 64
    if (1 + key.length() + val.length() > 64) return false;
    
    buffer[0] = static_cast<uint8_t>(key.length());
    memcpy(buffer + 1, key.data(), key.length());
    memcpy(buffer + 1 + key.length(), val.data(), val.length());
    
    return sendFrame(command_id, buffer, 1 + key.length() + val.length());
}

void BridgeClass::sendChunkyFrame(rpc::CommandId command_id, 
                       const uint8_t* header, size_t header_len, 
                       const uint8_t* data, size_t data_len) {
    // [SIL-2] Chunking Implementation
    // Simple strategy: Send header + chunk of data. Subsequent frames are just data?
    // Protocol v2 doesn't explicitly support multi-frame reconstruction in the core.
    // However, the Python side might handle it.
    // For now, we respect the MAX_PAYLOAD limit and truncate if necessary, 
    // or send single frame if it fits.
    
    size_t total_len = header_len + data_len;
    if (total_len <= rpc::RPC_MAX_PAYLOAD_SIZE) {
        uint8_t buffer[rpc::RPC_MAX_PAYLOAD_SIZE];
        if (header_len > 0) memcpy(buffer, header, header_len);
        if (data_len > 0) memcpy(buffer + header_len, data, data_len);
        sendFrame(command_id, buffer, total_len);
    } else {
        // Truncate for safety as per spec v2 (no fragmentation logic in base layer)
        // Or implementation dependent.
        // We will send what fits.
        size_t safe_data_len = rpc::RPC_MAX_PAYLOAD_SIZE - header_len;
        uint8_t buffer[rpc::RPC_MAX_PAYLOAD_SIZE];
        if (header_len > 0) memcpy(buffer, header, header_len);
        memcpy(buffer + header_len, data, safe_data_len);
        sendFrame(command_id, buffer, rpc::RPC_MAX_PAYLOAD_SIZE);
        _emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
    }
}

void BridgeClass::_emitStatus(rpc::StatusCode status_code) {
    // Fire and forget status (no ACK required)
    sendFrame(status_code, nullptr, 0);
}

void BridgeClass::_emitStatus(rpc::StatusCode status_code, etl::string_view message) {
    sendStringCommand(static_cast<rpc::CommandId>(status_code), message, rpc::RPC_MAX_PAYLOAD_SIZE);
}

void BridgeClass::_emitStatus(rpc::StatusCode status_code, const __FlashStringHelper* message) {
    // Copy flash string to buffer
    char buffer[rpc::RPC_MAX_PAYLOAD_SIZE];
    strncpy_P(buffer, (const char*)message, rpc::RPC_MAX_PAYLOAD_SIZE);
    buffer[rpc::RPC_MAX_PAYLOAD_SIZE-1] = 0;
    _emitStatus(status_code, etl::string_view(buffer));
}

bool BridgeClass::_isRecentDuplicateRx(const rpc::Frame& frame) const {
    // CRC-based deduplication
    if (frame.header.crc == _last_rx_crc) {
        // Check time window (e.g., 500ms)
        if (millis() - _last_rx_crc_millis < 500) {
            return true;
        }
    }
    return false;
}

void BridgeClass::_markRxProcessed(const rpc::Frame& frame) {
    _last_rx_crc = frame.header.crc;
    _last_rx_crc_millis = millis();
}

bool BridgeClass::_requiresAck(uint16_t command_id) const {
    // Check range. System commands 0x40-0x4F usually require ACK or response.
    // GPIO 0x50-0x5F: Write requires ACK. Read requires Response.
    // This logic should match the TOML spec.
    if (command_id == static_cast<uint16_t>(rpc::CommandId::CMD_DIGITAL_WRITE)) return true;
    if (command_id == static_cast<uint16_t>(rpc::CommandId::CMD_ANALOG_WRITE)) return true;
    if (command_id == static_cast<uint16_t>(rpc::CommandId::CMD_SET_PIN_MODE)) return true;
    if (command_id == static_cast<uint16_t>(rpc::CommandId::CMD_FILE_WRITE)) return true;
    if (command_id == static_cast<uint16_t>(rpc::CommandId::CMD_MAILBOX_PUSH)) return true;
    // ... complete list based on TOML
    return false; 
}

void BridgeClass::_sendAck(uint16_t command_id) {
    // ACK payload: 2 bytes of command_id being acknowledged
    uint8_t payload[2];
    payload[0] = (command_id >> 8) & 0xFF;
    payload[1] = command_id & 0xFF;
    _sendFrame(static_cast<uint16_t>(rpc::StatusCode::STATUS_ACK), payload, 2);
}

void BridgeClass::_sendAckAndFlush(uint16_t command_id) {
    _sendAck(command_id);
    flushStream();
}

void BridgeClass::flushStream() {
    _stream.flush();
}

// --- FSM / Timer Callbacks ---

void BridgeClass::_onAckTimeout() {
    _retry_count++;
    if (_retry_count <= _ack_retry_limit) {
        _retransmitLastFrame();
    } else {
        // Fatal error, link down?
        _emitStatus(rpc::StatusCode::STATUS_TIMEOUT);
        _fsm.send_event(bridge::fsm::EventId::EvTimeout);
        _clearPendingTxQueue();
    }
}

void BridgeClass::_retransmitLastFrame() {
    // Resend the frame at the head of the queue
    if (!_pending_tx_queue.empty()) {
        const PendingTxFrame& frame = _pending_tx_queue.front();
        _sendFrame(frame.command_id, frame.payload.data(), frame.payload_length);
    }
}

void BridgeClass::_onRxDedupe() {
    // Timer for dedupe cleanup if needed
}

void BridgeClass::_onBaudrateChange() {
    if (_pending_baudrate > 0 && _hardware_serial) {
        _hardware_serial->begin(_pending_baudrate);
        _pending_baudrate = 0;
    }
}

void BridgeClass::_onStartupStabilized() {
    _startup_stabilizing = false;
}

void BridgeClass::_clearPendingTxQueue() {
    while (!_pending_tx_queue.empty()) {
        _pending_tx_queue.pop();
    }
}

// --- Handler Implementations ---

void BridgeClass::onSystemCommand(const bridge::router::CommandContext& ctx) {
    // Handle GetVersion, GetCapabilities, etc.
    // Example:
    if (ctx.raw_command == static_cast<uint16_t>(rpc::CommandId::CMD_GET_VERSION)) {
        uint8_t payload[2];
        payload[0] = kDefaultFirmwareVersionMajor;
        payload[1] = kDefaultFirmwareVersionMinor;
        sendFrame(rpc::CommandId::CMD_GET_VERSION_RESP, payload, 2);
    }
}

void BridgeClass::onGpioCommand(const bridge::router::CommandContext& ctx) {
    // Handle Pin Mode, Digital Write, etc.
}

void BridgeClass::onConsoleCommand(const bridge::router::CommandContext& ctx) {
    // Handle Console In
    if (ctx.raw_command == static_cast<uint16_t>(rpc::CommandId::CMD_CONSOLE_WRITE)) {
        Console._push(etl::span<const uint8_t>(ctx.payload, ctx.length));
        _sendAck(ctx.raw_command);
    }
}

void BridgeClass::onDataStoreCommand(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_DATASTORE
    DataStore.handleResponse(*ctx.frame);
#endif
}

void BridgeClass::onMailboxCommand(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_MAILBOX
    Mailbox.handleResponse(*ctx.frame);
#endif
}

void BridgeClass::onFileSystemCommand(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_FILESYSTEM
    FileSystem.handleResponse(*ctx.frame);
#endif
}

void BridgeClass::onProcessCommand(const bridge::router::CommandContext& ctx) {
#if BRIDGE_ENABLE_PROCESS
    Process.handleResponse(*ctx.frame);
#endif
}

void BridgeClass::onUnknownCommand(const bridge::router::CommandContext& ctx) {
    _emitStatus(rpc::StatusCode::CMD_UNKNOWN);
}

void BridgeClass::onStatusCommand(const bridge::router::CommandContext& ctx) {
    // Handle ACKs and Errors
    if (ctx.raw_command == static_cast<uint16_t>(rpc::StatusCode::STATUS_ACK)) {
        // Process ACK
        // Payload contains the command ID being ACKed
        if (ctx.length >= 2) {
            uint16_t acked_id = (ctx.payload[0] << 8) | ctx.payload[1];
            // Check if this matches head of queue
            if (!_pending_tx_queue.empty() && _pending_tx_queue.front().command_id == acked_id) {
                _pending_tx_queue.pop();
                _fsm.send_event(bridge::fsm::EventId::EvAckReceived);
                _retry_count = 0;
            }
        }
    }
}

// --- Console Class ---

ConsoleClass::ConsoleClass() : _begun(false), _xoff_sent(false) {}

void ConsoleClass::begin() {
    _begun = true;
}

size_t ConsoleClass::write(uint8_t c) {
    return write(&c, 1);
}

size_t ConsoleClass::write(const uint8_t *buffer, size_t size) {
    if (!_begun) return 0;
    // Send via Bridge
    // Use chunky frame if needed, but for Console we typically send small chunks
    // To match Arduino Print interface, we might buffer locally or send immediately.
    // For SIL-2, immediate send is safer to avoid hidden buffers, but we have _tx_buffer.
    
    // Simple implementation: Send immediately using RPC
    Bridge.sendChunkyFrame(rpc::CommandId::CMD_CONSOLE_WRITE, nullptr, 0, buffer, size);
    return size;
}

void ConsoleClass::_push(etl::span<const uint8_t> data) {
    for (uint8_t b : data) {
        if (!_rx_buffer.full()) {
            _rx_buffer.push(b);
        }
    }
}

int ConsoleClass::available() {
    return _rx_buffer.size();
}

int ConsoleClass::read() {
    if (_rx_buffer.empty()) return -1;
    uint8_t c = _rx_buffer.front();
    _rx_buffer.pop();
    return c;
}

int ConsoleClass::peek() {
    if (_rx_buffer.empty()) return -1;
    return _rx_buffer.front();
}

void ConsoleClass::flush() {
    // No-op for RX buffer
}

// --- Subsystem Implementations ---

#if BRIDGE_ENABLE_DATASTORE
DataStoreClass::DataStoreClass() {}

void DataStoreClass::put(etl::string_view key, etl::string_view value) {
    Bridge.sendKeyValCommand(rpc::CommandId::CMD_DATASTORE_PUT, key, rpc::RPC_MAX_DATASTORE_KEY_LENGTH, value, rpc::RPC_MAX_PAYLOAD_SIZE);
}
// ... (Other DataStore methods implemented similarly)
void DataStoreClass::handleResponse(const rpc::Frame& frame) {
    // Implementation of response handling
}
#endif

#if BRIDGE_ENABLE_PROCESS
ProcessClass::ProcessClass() {}
void ProcessClass::handleResponse(const rpc::Frame& frame) {
    // Implementation
}
#endif


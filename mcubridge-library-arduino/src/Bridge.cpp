#include <etl/algorithm.h>
#include <etl/bitset.h>
#include <etl/crc32.h>

#include "Bridge.h"

#include <Arduino.h>
#include <etl/numeric.h>
#include <etl/span.h>
#include "protocol/rle.h"
#include "protocol/rpc_cobs.h"
#include "security/security.h"

#ifndef BRIDGE_DEFAULT_SERIAL_PORT
#define BRIDGE_DEFAULT_SERIAL_PORT Serial
#endif

// External logger provided by the emulator harness
#ifdef BRIDGE_HOST_TEST
extern void mcu_log(const char* fmt, ...);
#else
#endif

namespace {
void resetOnFatalError() {
#if defined(ARDUINO_ARCH_AVR)
  wdt_enable(WDTO_15MS);
#elif defined(ARDUINO_ARCH_ESP32)
  esp_restart();
#elif defined(ARDUINO_ARCH_ESP8266)
  ESP.restart();
#endif
  while (true) {}
}
}  // namespace

BridgeClass::BridgeClass(Stream& arg_stream)
    : etl::imessage_router(255),
      _stream(arg_stream),
      _hardware_serial(nullptr),
      _shared_secret(),
      _last_frame{0, {}},
      _cobs{0, 0, 0, 0, true, {0}},
      _frame_received(false),
      _rx_frame{},
      _last_parse_error(),
      _rng(millis()),
      _last_command_id(0),
      _retry_count(0),
      _pending_baudrate(0),
      _rx_history(),
      _consecutive_crc_errors(0),
      _ack_timeout_ms(rpc::RPC_DEFAULT_ACK_TIMEOUT_MS),
      _ack_retry_limit(rpc::RPC_DEFAULT_RETRY_LIMIT),
      _response_timeout_ms(rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS),
      _fsm(),
      _timers(),
      _startup_stabilizing(false),
      _subscribed(false),
      _bus() {
  _timers.clear();
}

void BridgeClass::begin(unsigned long arg_baudrate, etl::string_view arg_secret,
                        size_t arg_secret_len) {
  _fsm.begin();

  if (!rpc::security::run_cryptographic_self_tests()) {
    _fsm.cryptoFault();
    return;
  }

  _timers.clear();

  if (!_subscribed) {
    _timer_callbacks[0] = etl::delegate<void()>::create<BridgeClass, &BridgeClass::onAckTimeout>(*this);
    _timer_callbacks[1] = etl::delegate<void()>::create<BridgeClass, &BridgeClass::onRxDedupe>(*this);
    _timer_callbacks[2] = etl::delegate<void()>::create<BridgeClass, &BridgeClass::onBaudrateChange>(*this);
    _timer_callbacks[3] = etl::delegate<void()>::create<BridgeClass, &BridgeClass::onStartupStabilized>(*this);

    _timer_ids[0] = _timers.register_timer(_timer_callbacks[0], 0, false);
    _timer_ids[1] = _timers.register_timer(_timer_callbacks[1], 0, false);
    _timer_ids[2] = _timers.register_timer(_timer_callbacks[2], BRIDGE_BAUDRATE_SETTLE_MS, false);
    _timer_ids[3] = _timers.register_timer(_timer_callbacks[3], BRIDGE_STARTUP_STABILIZATION_MS, false);

    _bus.subscribe(*this);
    _bus.subscribe(Console);
#if BRIDGE_ENABLE_DATASTORE
    _bus.subscribe(DataStore);
#endif
#if BRIDGE_ENABLE_MAILBOX
    _bus.subscribe(Mailbox);
#endif
#if BRIDGE_ENABLE_FILESYSTEM
    _bus.subscribe(FileSystem);
#endif
#if BRIDGE_ENABLE_PROCESS
    _bus.subscribe(Process);
#endif
    
    add_observer(Console);
#if BRIDGE_ENABLE_DATASTORE
    add_observer(DataStore);
#endif
    _subscribed = true;
  }

  Console.begin();
#if BRIDGE_ENABLE_DATASTORE
  DataStore.begin();
#endif

  if (_hardware_serial != nullptr) _hardware_serial->begin(arg_baudrate);
  _startup_stabilizing = true;
  _timers.start(_timer_ids[3], false);
  _timers.enable(true);

  _shared_secret.clear();
  if (!arg_secret.empty()) {
    size_t actual_len = (arg_secret_len > 0) ? arg_secret_len : arg_secret.length();
    _shared_secret.assign(reinterpret_cast<const uint8_t*>(arg_secret.data()),
                          reinterpret_cast<const uint8_t*>(arg_secret.data()) + etl::min(actual_len, _shared_secret.capacity()));
  }
  _last_command_id = 0;
  _retry_count = 0;
  _rx_history.clear();
  _ack_timeout_ms = 500;
  _cobs.bytes_received = 0;
}

void BridgeClass::process() {
#if defined(ARDUINO_ARCH_AVR)
  if (kBridgeEnableWatchdog) wdt_reset();
#elif defined(ARDUINO_ARCH_ESP32)
  if (kBridgeEnableWatchdog) esp_task_wdt_reset();
#elif defined(ARDUINO_ARCH_ESP8266)
  if (kBridgeEnableWatchdog) yield();
#endif

  if (_startup_stabilizing) {
    uint8_t drain_limit = BRIDGE_STARTUP_DRAIN_PER_TICK;
    while (_stream.available() > 0 && drain_limit-- > 0) _stream.read();
  } else {
    BRIDGE_ATOMIC_BLOCK {
      while (_stream.available() > 0) {
        int b = _stream.read();
        if (b < 0) break;
        uint8_t byte = static_cast<uint8_t>(b);
        
        if (byte == rpc::RPC_FRAME_DELIMITER) {
          if (_cobs.bytes_received > 0) {
            etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE> decoded_buf;
            size_t decoded_len = rpc::cobs::decode(
                etl::span<const uint8_t>(_cobs.buffer.data(), _cobs.bytes_received),
                etl::span<uint8_t>(decoded_buf.data(), rpc::MAX_RAW_FRAME_SIZE));
                
            if (decoded_len >= 5 + rpc::CRC_TRAILER_SIZE) {
              etl::crc32 crc_calc;
              const size_t data_len = decoded_len - rpc::CRC_TRAILER_SIZE;
              crc_calc.add(&decoded_buf[0], &decoded_buf[data_len]);
              
              if (crc_calc.value() == rpc::read_u32_be(&decoded_buf[data_len])) {
                _rx_frame.header.version = decoded_buf[0];
                _rx_frame.header.payload_length = rpc::read_u16_be(&decoded_buf[1]);
                _rx_frame.header.command_id = rpc::read_u16_be(&decoded_buf[3]);
                _rx_frame.crc = crc_calc.value();
                
                if (_rx_frame.header.version == rpc::PROTOCOL_VERSION && _rx_frame.header.payload_length <= rpc::MAX_PAYLOAD_SIZE) {
                  if (_rx_frame.header.payload_length > 0) {
                    etl::copy_n(&decoded_buf[5], _rx_frame.header.payload_length, _rx_frame.payload.begin());
                  }
                  _frame_received = true;
                  _last_parse_error.reset();
                } else {
                  _last_parse_error = rpc::FrameError::MALFORMED;
                }
              } else {
                _last_parse_error = rpc::FrameError::CRC_MISMATCH;
              }
            } else {
              _last_parse_error = rpc::FrameError::MALFORMED;
            }
          }
          _cobs.bytes_received = 0;
          if (_frame_received || _last_parse_error.has_value()) break;
        } else {
          if (_cobs.bytes_received < rpc::MAX_RAW_FRAME_SIZE) {
            _cobs.buffer[_cobs.bytes_received++] = byte;
          } else {
            _last_parse_error = rpc::FrameError::OVERFLOW;
            _cobs.bytes_received = 0;
            break;
          }
        }
      }
    }
  }

  if (_frame_received) { _frame_received = false; dispatch(_rx_frame); }
  else if (_last_parse_error.has_value()) {
    rpc::FrameError error = _last_parse_error.value();
    if (error == rpc::FrameError::CRC_MISMATCH) {
      BRIDGE_ATOMIC_BLOCK { _consecutive_crc_errors++; }
      if (_consecutive_crc_errors >= BRIDGE_MAX_CONSECUTIVE_CRC_ERRORS) resetOnFatalError();
      emitStatus(rpc::StatusCode::STATUS_CRC_MISMATCH);
    } else { emitStatus(rpc::StatusCode::STATUS_MALFORMED); }
    _last_parse_error.reset();
  }

  _timers.tick(millis());
}

void BridgeClass::receive(const etl::imessage& msg) {
  if (msg.get_message_id() < rpc::RPC_STATUS_CODE_MIN) return;
  const auto& cmd_msg = static_cast<const bridge::router::CommandMessage&>(msg);
  const uint16_t cmd = cmd_msg.raw_command;

  if (cmd >= rpc::RPC_STATUS_CODE_MIN && cmd <= rpc::RPC_STATUS_CODE_MAX) onStatusCommand(cmd_msg);
  else if (cmd >= rpc::RPC_SYSTEM_COMMAND_MIN && cmd <= rpc::RPC_SYSTEM_COMMAND_MAX) onSystemCommand(cmd_msg);
  else if (cmd >= rpc::RPC_GPIO_COMMAND_MIN && cmd <= rpc::RPC_GPIO_COMMAND_MAX) onGpioCommand(cmd_msg);
}

bool BridgeClass::accepts(etl::message_id_t id) const {
  const uint16_t cmd = static_cast<uint16_t>(id);
  return (cmd >= rpc::RPC_STATUS_CODE_MIN && cmd <= rpc::RPC_STATUS_CODE_MAX) || (cmd >= rpc::RPC_SYSTEM_COMMAND_MIN && cmd <= rpc::RPC_SYSTEM_COMMAND_MAX) || (cmd >= rpc::RPC_GPIO_COMMAND_MIN && cmd <= rpc::RPC_GPIO_COMMAND_MAX);
}

void BridgeClass::onStatusCommand(const bridge::router::CommandMessage& msg) {
  const uint16_t status_val = msg.raw_command;
  switch (status_val - rpc::RPC_STATUS_CODE_MIN) {
    case 3: { auto pl_res = rpc::Payload::parse<rpc::payload::AckPacket>(*msg.frame); _handleAck(pl_res ? pl_res->command_id : rpc::RPC_INVALID_ID_SENTINEL); break; }
    case 8: { auto pl_res = rpc::Payload::parse<rpc::payload::AckPacket>(*msg.frame); _handleAck(pl_res ? pl_res->command_id : rpc::RPC_INVALID_ID_SENTINEL); break; }
    default: break;
  }
  if (_status_handler.is_valid()) _status_handler(static_cast<rpc::StatusCode>(status_val), etl::span<const uint8_t>(msg.frame->payload.data(), msg.frame->header.payload_length));
}

void BridgeClass::onSystemCommand(const bridge::router::CommandMessage& msg) {
  const uint16_t index = (msg.raw_command - rpc::RPC_SYSTEM_COMMAND_MIN) >> 1;
  switch (index) {
    case 0: _withResponse(msg, [&]() { _sendResponse<rpc::payload::VersionResponse>(rpc::CommandId::CMD_GET_VERSION_RESP, kDefaultFirmwareVersionMajor, kDefaultFirmwareVersionMinor); }); break;
    case 1: _withResponse(msg, [&]() { _sendResponse<rpc::payload::FreeMemoryResponse>(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, getFreeMemory()); }); break;
    case 2: _handleLinkSync(msg); break;
    case 3: _withResponse(msg, [&]() { enterSafeState(); if (msg.frame->header.payload_length == rpc::payload::HandshakeConfig::SIZE) { _applyTimingConfig(*msg.frame); } (void)sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP); }); break;
    case 4: _withResponse(msg, [&]() {
        uint8_t arch = 0;
#if defined(ARDUINO_ARCH_AVR)
        arch = rpc::RPC_ARCH_AVR;
#elif defined(ARDUINO_ARCH_ESP32)
        arch = rpc::RPC_ARCH_ESP32;
#elif defined(ARDUINO_ARCH_ESP8266)
        arch = rpc::RPC_ARCH_ESP8266;
#elif defined(ARDUINO_ARCH_SAMD)
        arch = rpc::RPC_ARCH_SAMD;
#elif defined(ARDUINO_ARCH_RP2040)
        arch = rpc::RPC_ARCH_RP2040;
#endif
        etl::bitset<32> features; features.set(0); if (kBridgeEnableWatchdog) features.set(1);
#if BRIDGE_DEBUG_FRAMES
        features.set(2);
#endif
        _sendResponse<rpc::payload::Capabilities>(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, rpc::PROTOCOL_VERSION, arch, uint8_t{20}, uint8_t{6}, static_cast<uint32_t>(features.to_ulong()));
      }); break;
    case 5: _withPayloadResponse<rpc::payload::SetBaudratePacket>(msg, [&](const rpc::payload::SetBaudratePacket& p) { (void)sendFrame(rpc::CommandId::CMD_SET_BAUDRATE_RESP); flushStream(); _pending_baudrate = p.baudrate; _timers.set_period(_timer_ids[2], BRIDGE_BAUDRATE_SETTLE_MS); _timers.start(_timer_ids[2], false); }); break;
    default: break;
  }
}

void BridgeClass::onGpioCommand(const bridge::router::CommandMessage& msg) {
  const uint16_t index = msg.raw_command - rpc::RPC_GPIO_COMMAND_MIN;
  switch (index) {
    case 0: _withPayloadAck<rpc::payload::PinMode>(msg, [](const rpc::payload::PinMode& p) { if (bridge::hal::isValidPin(p.pin)) ::pinMode(p.pin, p.mode); }); break;
    case 1: _withPayloadAck<rpc::payload::DigitalWrite>(msg, [](const rpc::payload::DigitalWrite& p) { if (bridge::hal::isValidPin(p.pin)) ::digitalWrite(p.pin, p.value ? HIGH : LOW); }); break;
    case 2: _withPayloadAck<rpc::payload::AnalogWrite>(msg, [](const rpc::payload::AnalogWrite& p) { if (bridge::hal::isValidPin(p.pin)) ::analogWrite(p.pin, p.value); }); break;
    case 3: _handlePinRead<rpc::payload::DigitalReadResponse>(msg, rpc::CommandId::CMD_DIGITAL_READ_RESP, [](uint8_t p) { return bridge::hal::isValidPin(p); }, [](uint8_t p) -> uint8_t { return static_cast<uint8_t>(::digitalRead(p)); }); break;
    case 4: _handlePinRead<rpc::payload::AnalogReadResponse>(msg, rpc::CommandId::CMD_ANALOG_READ_RESP, [](uint8_t p) { return bridge::hal::isValidPin(p); }, [](uint8_t p) -> uint16_t { return static_cast<uint16_t>(::analogRead(p)); }); break;
    default: break;
  }
}

void BridgeClass::onUnknownCommand(const bridge::router::CommandMessage& msg) {
  if (_command_handler.is_valid()) _command_handler(*msg.frame);
  else (void)sendFrame(rpc::StatusCode::STATUS_CMD_UNKNOWN);
}

void BridgeClass::dispatch(const rpc::Frame& frame) {
  _consecutive_crc_errors = 0;
  uint16_t raw_command = frame.header.command_id;
  rpc::Frame decompressed_frame;
  const rpc::Frame* effective_frame = &frame;
  if (raw_command & rpc::RPC_CMD_FLAG_COMPRESSED) {
    raw_command &= ~rpc::RPC_CMD_FLAG_COMPRESSED;
    decompressed_frame.header = frame.header; decompressed_frame.header.command_id = raw_command;
    size_t out_len = rle::decode(etl::span<const uint8_t>(frame.payload.data(), frame.header.payload_length), etl::span<uint8_t>(decompressed_frame.payload.data(), rpc::MAX_PAYLOAD_SIZE));
    decompressed_frame.header.payload_length = static_cast<uint16_t>(out_len); effective_frame = &decompressed_frame;
  }
  bridge::router::CommandMessage msg(effective_frame, raw_command, _isRecentDuplicateRx(*effective_frame), rpc::requires_ack(raw_command));
  if (!_fsm.isSynchronized() && !_isHandshakeCommand(raw_command)) { sendFrame(rpc::StatusCode::STATUS_ERROR); return; }
  _bus.receive(msg);
}

void BridgeClass::onAckTimeout() { 
  if (_retry_count < _ack_retry_limit) {
    _retry_count++;
    _retransmitLastFrame();
    _timers.set_period(_timer_ids[0], _ack_timeout_ms);
    _timers.start(_timer_ids[0], false);
  } else {
    _fsm.timeout(); 
  }
}
void BridgeClass::onBaudrateChange() { if (_hardware_serial) _hardware_serial->begin(_pending_baudrate); _pending_baudrate = 0; }
void BridgeClass::onRxDedupe() { _rx_history.clear(); }
void BridgeClass::onStartupStabilized() { _startup_stabilizing = false; }

bool BridgeClass::sendFrame(rpc::CommandId cmd, etl::span<const uint8_t> pl) { return _sendFrame(rpc::to_underlying(cmd), pl); }
bool BridgeClass::sendFrame(rpc::StatusCode st, etl::span<const uint8_t> pl) { return _sendFrame(rpc::to_underlying(st), pl); }

bool BridgeClass::_sendFrame(uint16_t cmd, etl::span<const uint8_t> pl) {
  if (!_fsm.isSynchronized() && !_isHandshakeCommand(cmd)) {
    return false;
  }
  _sendRawFrame(cmd, pl);
  if (rpc::requires_ack(cmd)) {
    _last_command_id = cmd;
    _retry_count = 0;
    _timers.set_period(_timer_ids[0], _ack_timeout_ms);
    _timers.start(_timer_ids[0], false);
    _fsm.sendCritical();
  }
  return true;
}

void BridgeClass::_sendRawFrame(uint16_t cmd, etl::span<const uint8_t> pl) {
  etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE> raw;
  uint16_t out_cmd = cmd; etl::span<const uint8_t> effective_pl = pl;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> comp_buf;
  if (pl.size() > 8 && rle::should_compress(pl)) {
    size_t c_len = rle::encode(pl, etl::span<uint8_t>(comp_buf.data(), rpc::MAX_PAYLOAD_SIZE));
    if (c_len < pl.size()) { out_cmd |= rpc::RPC_CMD_FLAG_COMPRESSED; effective_pl = etl::span<const uint8_t>(comp_buf.data(), c_len); }
  }
  size_t len = 0; raw[len++] = rpc::PROTOCOL_VERSION;
  rpc::write_u16_be(&raw[len], effective_pl.size()); len += 2;
  rpc::write_u16_be(&raw[len], out_cmd); len += 2;
  if (!effective_pl.empty()) { etl::copy_n(effective_pl.data(), effective_pl.size(), &raw[len]); len += effective_pl.size(); }
  etl::crc32 crc; crc.add(&raw[0], &raw[len]);
  rpc::write_u32_be(&raw[len], crc.value()); len += 4;
  
  if (rpc::requires_ack(cmd)) {
    _last_frame.command_id = cmd;
    _last_frame.raw.assign(raw.begin(), raw.begin() + len);
  }

  etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE + 2> cobs;
  size_t enc = rpc::cobs::encode(etl::span<const uint8_t>(raw.data(), len), etl::span<uint8_t>(cobs.data(), rpc::MAX_RAW_FRAME_SIZE + 2));
  if (enc > 0) { 
    _stream.write(cobs.data(), enc); _stream.write(rpc::RPC_FRAME_DELIMITER); _stream.flush(); 
  }
  }


void BridgeClass::_retransmitLastFrame() {
  if (_last_frame.raw.empty()) return;
  etl::array<uint8_t, rpc::MAX_RAW_FRAME_SIZE + 2> cobs;
  size_t enc = rpc::cobs::encode(etl::span<const uint8_t>(_last_frame.raw.data(), _last_frame.raw.size()), etl::span<uint8_t>(cobs.data(), rpc::MAX_RAW_FRAME_SIZE + 2));
  if (enc > 0) { 
    _stream.write(cobs.data(), enc); _stream.write(rpc::RPC_FRAME_DELIMITER); _stream.flush(); 
  }
  }


void BridgeClass::_handleLinkSync(const bridge::router::CommandMessage& msg) {
  const size_t nl = rpc::RPC_HANDSHAKE_NONCE_LENGTH;
  const bool has_sec = !_shared_secret.empty();
  if (msg.frame->header.payload_length != nl + (has_sec ? rpc::RPC_HANDSHAKE_TAG_LENGTH : 0)) {
    emitStatus(rpc::StatusCode::STATUS_MALFORMED); return;
  }
  _withResponse(msg, [&]() {
    enterSafeState(); _fsm.handshakeStart();
    if (has_sec) {
      etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> exp_tag;
      _computeHandshakeTag(etl::span<const uint8_t>(msg.frame->payload.data(), nl), exp_tag.data());
      if (!etl::equal(exp_tag.begin(), exp_tag.end(), msg.frame->payload.data() + nl)) {
        _fsm.handshakeFailed(); return;
      }
    }
    etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buf;
    etl::copy_n(msg.frame->payload.data(), nl, buf.begin());
    if (has_sec) _computeHandshakeTag(etl::span<const uint8_t>(buf.data(), nl), buf.data() + nl);
    (void)sendFrame(rpc::CommandId::CMD_LINK_SYNC_RESP, etl::span<const uint8_t>(buf.data(), nl + (has_sec ? rpc::RPC_HANDSHAKE_TAG_LENGTH : 0)));
    flushStream();
    _fsm.handshakeComplete(); notify_observers(MsgBridgeSynchronized());
  });
}

void BridgeClass::_handleAck(uint16_t cmd) { 
  if (isAwaitingAck() && cmd == _last_command_id) { 
    _timers.stop(_timer_ids[0]); 
    _fsm.ackReceived();
    _last_frame.raw.clear();
  } 
}
void BridgeClass::_handleMalformed(uint16_t) { if (isAwaitingAck()) { _timers.stop(_timer_ids[0]); _fsm.ackReceived(); } }
void BridgeClass::enterSafeState() { _clearAckState(); _fsm.resetFsm(); }
void BridgeClass::_clearAckState() { _timers.stop(_timer_ids[0]); _last_command_id = 0; }
void BridgeClass::_computeHandshakeTag(etl::span<const uint8_t> n, uint8_t* out) {
  uint8_t auth_key[32];
  rpc::security::derive_handshake_key(_shared_secret.data(), _shared_secret.size(), auth_key);
  rpc::security::hmac_sha256(auth_key, 32, n.data(), n.size(), out, rpc::RPC_HANDSHAKE_TAG_LENGTH);
  rpc::security::secure_zero(auth_key, 32);
}

void BridgeClass::_applyTimingConfig(const rpc::Frame& frame) {
  auto config = rpc::Payload::parse<rpc::payload::HandshakeConfig>(frame);
  if (config) {
    _ack_timeout_ms = etl::max<uint16_t>(rpc::RPC_HANDSHAKE_ACK_TIMEOUT_MIN_MS, config->ack_timeout_ms);
    _ack_retry_limit = config->ack_retry_limit;
    _response_timeout_ms = etl::max<uint32_t>(rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS, config->response_timeout_ms);
  }
}

void BridgeClass::_markRxProcessed(const rpc::Frame& f) { if (_rx_history.full()) _rx_history.pop(); _rx_history.push({f.crc, static_cast<uint32_t>(millis())}); }
bool BridgeClass::_isRecentDuplicateRx(const rpc::Frame& f) const { for (const auto& h : _rx_history) if (h.crc == f.crc) return true; return false; }
bool BridgeClass::_isHandshakeCommand(uint16_t cmd) const {
  return cmd == rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC) ||
         cmd == rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET) ||
         cmd == rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC_RESP) ||
         cmd == rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET_RESP);
}
void BridgeClass::emitStatus(rpc::StatusCode st, etl::string_view msg) { etl::vector<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl; if (!msg.empty()) pl.assign(reinterpret_cast<const uint8_t*>(msg.data()), reinterpret_cast<const uint8_t*>(msg.data()) + etl::min(msg.length(), pl.capacity())); _doEmitStatus(st, pl); }
void BridgeClass::emitStatus(rpc::StatusCode st, const __FlashStringHelper* msg) { etl::vector<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl; if (msg) { const char* p = reinterpret_cast<const char*>(msg); while (uint8_t c = pgm_read_byte(p++)) { if (pl.full()) break; pl.push_back(c); } } _doEmitStatus(st, pl); }
void BridgeClass::_doEmitStatus(rpc::StatusCode status_code, etl::span<const uint8_t> payload) { 
  (void)_sendFrame(rpc::to_underlying(status_code), payload); 
}
void BridgeClass::_sendAck(uint16_t cmd) { _sendResponse<rpc::payload::AckPacket>(rpc::StatusCode::STATUS_ACK, cmd); }
void BridgeClass::_sendAckAndFlush(uint16_t cmd) { _sendAck(cmd); flushStream(); }
bool BridgeClass::sendStringCommand(rpc::CommandId cmd, etl::string_view str, size_t max) { if (str.length() > max || str.length() >= rpc::MAX_PAYLOAD_SIZE) { emitStatus(rpc::StatusCode::STATUS_OVERFLOW); return false; } if (str.empty()) return true; etl::vector<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl; rpc::PacketBuilder(pl).add_pascal_string(str); return sendFrame(cmd, pl); }
bool BridgeClass::sendKeyValCommand(rpc::CommandId cmd, etl::string_view k, size_t mk, etl::string_view v, size_t mv) { if (k.length() > mk || v.length() > mv || k.length() + v.length() + 2 > rpc::MAX_PAYLOAD_SIZE) { emitStatus(rpc::StatusCode::STATUS_OVERFLOW); return false; } if (k.empty()) return true; etl::vector<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl; rpc::PacketBuilder(pl).add_pascal_string(k).add_pascal_string(v); return sendFrame(cmd, pl); }
bool BridgeClass::sendChunkyFrame(rpc::CommandId cmd, etl::span<const uint8_t> h, etl::span<const uint8_t> d) {
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> buffer;
  if (h.size() + d.size() > rpc::MAX_PAYLOAD_SIZE) return false;
  if (h.size() > 0) etl::copy(h.begin(), h.end(), buffer.begin());
  if (d.size() > 0) etl::copy(d.begin(), d.end(), buffer.begin() + h.size());
  return _sendFrame(rpc::to_underlying(cmd), etl::span<const uint8_t>(buffer.data(), h.size() + d.size()));
}

BridgeClass Bridge(BRIDGE_DEFAULT_SERIAL_PORT);

namespace etl { void __attribute__((weak)) handle_error(const etl::exception& e) { (void)e; Bridge.enterSafeState(); } }

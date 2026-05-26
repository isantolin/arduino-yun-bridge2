/*
 * This file is part of Arduino MCU Ecosystem v2.
 * Copyright (C) 2025-2026 Ignacio Santolin and contributors
 */

#include "Bridge.h"

#include <Arduino.h>
#include <pb_decode.h>
#include <pb_encode.h>

#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"

BridgeClass::BridgeClass(Stream& stream)
    : _stream(stream),
      _hardware_serial(nullptr),
      _command_handler(),
      _status_handler(),
      _last_command_id(0),
      _tx_sequence_id(0),
      _retry_count(0),
      _retry_limit(rpc::RPC_DEFAULT_RETRY_LIMIT),
      _ack_timeout_ms(rpc::RPC_DEFAULT_ACK_TIMEOUT_MS),
      _response_timeout_ms(rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS),
      _pending_baudrate(0),
      _consecutive_crc_errors(0),
      _last_parse_error(rpc::FrameError::NONE),
      _packet_serial(etl::span<uint8_t>(_ps_rx_storage), etl::span<uint8_t>(_ps_work_buffer)),
      _tx_nonce_counter(0),
      _rx_nonce_counter(0),
      _fsm(),
      _watchdog_task(),
      _serial_task(),
      _timer_task(),
      _is_post_passed(false),
      _tx_enabled(true) {
  _serial_task.bind(*this);
  _timer_task.bind(*this);
  _tasks.push_back(&_watchdog_task);
  _tasks.push_back(&_serial_task);
  _tasks.push_back(&_timer_task);
}

void BridgeClass::begin(uint32_t baudrate, const char* secret) {
  if (baudrate > 0) {
    _hardware_serial = dynamic_cast<HardwareSerial*>(&_stream);
    if (_hardware_serial) _hardware_serial->begin(baudrate);
  }

  if (secret != nullptr) {
    const uint8_t* secret_ptr = reinterpret_cast<const uint8_t*>(secret);
    _shared_secret.assign(secret_ptr, secret_ptr + strlen(secret));
  }

  _packet_serial.setPacketHandler(
      etl::delegate<void(etl::span<const uint8_t>)>::create<BridgeClass, &BridgeClass::_onPacketReceived>(*this));

  _fsm.start();
  _initializeRuntime();
}

void BridgeClass::process() {
  for (auto* task : _tasks) {
    if (task->task_request_work() > 0) {
      task->task_process_work();
    }
  }
}

bool BridgeClass::isSynchronized() const { return _fsm.isSynchronized(); }

void BridgeClass::enterSafeState() {
  _tx_enabled = false;
  _fsm.receive(bridge::fsm::EvReset());
}

void BridgeClass::signalXoff() { (void)sendFrame(rpc::CommandId::CMD_XOFF); }
void BridgeClass::signalXon() { (void)sendFrame(rpc::CommandId::CMD_XON); }
void BridgeClass::_initializeRuntime() {
  _is_post_passed = true;
  _tx_enabled = true;
  _fsm.receive(bridge::fsm::EvReset());
}

void BridgeClass::_onPacketReceived(etl::span<const uint8_t> packet) {
  auto res = rpc::FrameParser().parse(packet);
  if (!res) {
    _last_parse_error = res.error();
    _consecutive_crc_errors++;
    if (_consecutive_crc_errors >= 3) {
      enterSafeState();
    }
    return;
  }


  _consecutive_crc_errors = 0;
  _handleReceivedFrame(packet);
}

void BridgeClass::_handleReceivedFrame(etl::span<const uint8_t> p) {
  auto res = rpc::FrameParser().parse(p);
  if (!res) return;

  rpc::Frame frame = res.value();
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> dec_pl;

  if (isSynchronized() && !_shared_secret.empty() && frame.envelope.tag.size > 0) {
    if (!rpc::security::aead_decrypt_frame(frame.envelope.sequence_id, frame.payload(),
            etl::span<const uint8_t>(frame.envelope.tag.bytes, 16), _session_key,
            etl::span<const uint8_t>(frame.envelope.nonce.bytes, 12), dec_pl) ||
        !rpc::security::validate_frame_nonce(etl::span<const uint8_t>(frame.envelope.nonce.bytes, 12), &_rx_nonce_counter)) {
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }
    frame.envelope.payload.size = static_cast<pb_size_t>(frame.envelope.payload.size);
    etl::copy_n(dec_pl.begin(), frame.envelope.payload.size, frame.envelope.payload.bytes);
  }

  rpc_pb_RpcPayload payload = rpc_pb_RpcPayload_init_default;
  pb_istream_t stream = pb_istream_from_buffer(frame.envelope.payload.bytes,
                                               frame.envelope.payload.size);
  if (pb_decode(&stream, rpc_pb_RpcPayload_fields, &payload)) {
    bridge::router::CommandContext ctx(&frame, frame.envelope.sequence_id, false,
                                      is_reliable_cmd(static_cast<uint16_t>(payload.which_msg)));
    _dispatch(payload, ctx);
  } else {
    emitStatus(rpc::StatusCode::STATUS_MALFORMED);
  }
}

void BridgeClass::_dispatch(const rpc_pb_RpcPayload& payload,
                            const bridge::router::CommandContext& ctx) {
  switch (payload.which_msg) {
    case rpc_pb_RpcPayload_ok_tag:
      _handleStatusOk(ctx);
      break;
    case rpc_pb_RpcPayload_malformed_tag:
      _handleStatusMalformed(ctx);
      break;
    case rpc_pb_RpcPayload_ack_tag:
      _handleStatusAck(ctx, payload.msg.ack);
      break;
    case rpc_pb_RpcPayload_get_version_tag:
      _handleGetVersion(ctx);
      break;
    case rpc_pb_RpcPayload_link_sync_tag:
      _handleLinkSync(ctx, payload.msg.link_sync);
      break;
    case rpc_pb_RpcPayload_link_reset_tag:
      _handleLinkReset(ctx, payload.msg.link_reset);
      break;
    case rpc_pb_RpcPayload_digital_write_tag:
      _handleDigitalWriteCommand(ctx, payload.msg.digital_write);
      break;
    case rpc_pb_RpcPayload_set_pin_mode_tag:
      _handleSetPinModeCommand(ctx, payload.msg.set_pin_mode);
      break;
    case rpc_pb_RpcPayload_digital_read_tag:
      _handleDigitalReadCommand(ctx, payload.msg.digital_read);
      break;
    case rpc_pb_RpcPayload_analog_read_tag:
      _handleAnalogReadCommand(ctx, payload.msg.analog_read);
      break;
    case rpc_pb_RpcPayload_console_write_tag:
      _handleConsoleWriteCommand(ctx, payload.msg.console_write);
      break;
    default:
      onUnknownCommand(ctx);
      break;
  }
}

void BridgeClass::emitStatus(rpc::StatusCode s, etl::string_view m) {
  (void)sendFrame(s, 0, {});
}

void BridgeClass::emitStatus(rpc::StatusCode s, etl::span<const uint8_t> p) {
  (void)sendFrame(s, 0, p);
}

void BridgeClass::emitStatus(rpc::StatusCode s, const __FlashStringHelper* m) {
  (void)sendFrame(s, 0, {});
}

void BridgeClass::_sendRawFrame(uint16_t sequence_id,
                                etl::span<const uint8_t> payload,
                                bool do_encrypt) {
  if (!_tx_enabled) return;

  etl::array<uint8_t, rpc::AEAD_NONCE_SIZE> nonce = {};
  etl::array<uint8_t, rpc::AEAD_TAG_SIZE> tag = {};
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> enc_pl;
  etl::span<const uint8_t> final_payload = payload;

  if (do_encrypt && isSynchronized() && !_shared_secret.empty()) {
    if (!rpc::security::aead_encrypt_frame(sequence_id, payload, _session_key,
                                           &_tx_nonce_counter, enc_pl, nonce,
                                           tag))
      return;
    final_payload = etl::span<const uint8_t>(enc_pl.data(), payload.size());
  }

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> buffer;
  size_t len =
      rpc::FrameBuilder::build(buffer, sequence_id, final_payload, nonce, tag);
  if (len > 0)
    _packet_serial.send(_stream, etl::span<const uint8_t>(buffer.data(), len));
}

bool BridgeClass::sendFrame(rpc::StatusCode s, uint16_t seq,
                            etl::span<const uint8_t> p) {
  return _sendFrame(static_cast<uint16_t>(s), seq, p);
}

bool BridgeClass::sendFrame(rpc::CommandId c, uint16_t seq,
                            etl::span<const uint8_t> p) {
  return _sendFrame(static_cast<uint16_t>(c), seq, p);
}

bool BridgeClass::_sendFrame(uint16_t cmd, uint16_t seq,
                             etl::span<const uint8_t> pl) {
  if (is_reliable_cmd(cmd) && _fsm.isAwaitingAck()) {
    return false;
  }

  const bool is_excluded = (cmd >= rpc::RPC_STATUS_CODE_MIN &&
                            cmd <= rpc::RPC_STATUS_CODE_MAX) ||
                           (cmd >= rpc::RPC_SYSTEM_COMMAND_MIN &&
                            cmd <= rpc::RPC_SYSTEM_COMMAND_MAX);

  _sendRawFrame(seq, pl, !is_excluded);

  if (is_reliable_cmd(cmd)) {
    _fsm.receive(bridge::fsm::EvSendCritical());
    _last_command_id = cmd;
    _last_sequence_id = seq;
  }
  return true;
}

void BridgeClass::_handleLinkSync(const bridge::router::CommandContext& ctx,
                                  const rpc_pb_LinkSync& m) {
  if (m.nonce.size != 16 || m.tag.size != 16) {
    emitStatus(rpc::StatusCode::STATUS_MALFORMED);
    return;
  }

  etl::array<uint8_t, 16> expected_tag;
  if (rpc::security::handshake_authenticate(
          etl::span<const uint8_t>(_shared_secret),
          etl::span<const uint8_t>(m.nonce.bytes, 16),
          etl::span<const uint8_t>(m.tag.bytes, 16),
          etl::span<uint8_t>(expected_tag))) {
    rpc::security::derive_session_key(etl::span<const uint8_t>(_shared_secret),
                                      etl::span<const uint8_t>(m.nonce.bytes, 16),
                                      etl::span<uint8_t>(_session_key));
    _fsm.receive(bridge::fsm::EvHandshakeStart());
    _fsm.receive(bridge::fsm::EvHandshakeComplete());
    emitStatus(rpc::StatusCode::STATUS_OK);
  } else {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

void BridgeClass::_handleLinkReset(const bridge::router::CommandContext& ctx,
                                   const rpc_pb_HandshakeConfig& m) {
  _fsm.receive(bridge::fsm::EvReset());
  _shared_secret.clear();
  emitStatus(rpc::StatusCode::STATUS_OK);
}

void BridgeClass::_handleStatusOk(const bridge::router::CommandContext& ctx) {
  // Logic for Status OK
}

void BridgeClass::_handleStatusMalformed(const bridge::router::CommandContext& ctx) {
  // Logic for Status Malformed
}

void BridgeClass::_handleStatusAck(const bridge::router::CommandContext& ctx,
                                   const rpc_pb_AckPacket& ack) {
  _handleAck(static_cast<uint16_t>(ack.command_id));
}

void BridgeClass::_handleAck(uint16_t cmd) {
  if (_fsm.isAwaitingAck() && cmd == _last_command_id) {
    _fsm.receive(bridge::fsm::EvAckReceived());
  }
}

void BridgeClass::_handleGetVersion(const bridge::router::CommandContext& ctx) {
  rpc_pb_VersionResponse resp = rpc_pb_VersionResponse_init_default;
  resp.major = 2;
  resp.minor = 8;
  resp.patch = 5;
  (void)send(rpc::CommandId::CMD_GET_VERSION_RESP, ctx.sequence_id, resp);
}

void BridgeClass::_handleDigitalWriteCommand(const bridge::router::CommandContext& ctx,
                                            const rpc_pb_DigitalWrite& m) {
  pinMode(m.pin, OUTPUT);
  digitalWrite(m.pin, m.value);
  (void)sendFrame(rpc::StatusCode::STATUS_OK, ctx.sequence_id);
}

void BridgeClass::_handleSetPinModeCommand(const bridge::router::CommandContext& ctx,
                                          const rpc_pb_PinMode& m) {
  pinMode(m.pin, m.mode);
  (void)sendFrame(rpc::StatusCode::STATUS_OK, ctx.sequence_id);
}

void BridgeClass::_handleDigitalReadCommand(const bridge::router::CommandContext& ctx,
                                           const rpc_pb_PinRead& m) {
  rpc_pb_DigitalReadResponse resp = rpc_pb_DigitalReadResponse_init_default;
  resp.value = digitalRead(m.pin);
  (void)send(rpc::CommandId::CMD_DIGITAL_READ, ctx.sequence_id, resp);
}

void BridgeClass::_handleAnalogReadCommand(const bridge::router::CommandContext& ctx,
                                          const rpc_pb_PinRead& m) {
  rpc_pb_AnalogReadResponse resp = rpc_pb_AnalogReadResponse_init_default;
  resp.value = analogRead(m.pin);
  (void)send(rpc::CommandId::CMD_ANALOG_READ, ctx.sequence_id, resp);
}

void BridgeClass::_handleConsoleWriteCommand(const bridge::router::CommandContext& ctx,
                                            const rpc_pb_ConsoleWrite& m) {
  _stream.write(m.data.bytes, m.data.size);
  (void)sendFrame(rpc::StatusCode::STATUS_OK, ctx.sequence_id);
}


void BridgeClass::WatchdogTask::task_process_work() {
  // Watchdog logic
}

void BridgeClass::SerialTask::task_process_work() {
  if (bridge) bridge->_packet_serial.update(bridge->_stream);
}

void BridgeClass::TimerTask::task_process_work() {
  // Timer logic
}

void BridgeClass::_onAckTimeout() {
  _fsm.receive(bridge::fsm::EvTimeout());
}

void BridgeClass::_onRxDedupe() {
  // Dedupe logic
}

void BridgeClass::_onBaudrateChange() {
  // Baudrate change logic
}

void BridgeClass::_retransmitLastFrame() {
  // Retransmit logic
}

void BridgeClass::onUnknownCommand(const bridge::router::CommandContext& ctx) {
  (void)sendFrame(rpc::StatusCode::STATUS_MALFORMED, ctx.sequence_id);
}

BridgeClass Bridge(Serial);

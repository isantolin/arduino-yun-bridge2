/**
 * @file Bridge.h
 * @brief Arduino MCU Bridge v2 - MCU-side RPC library.
 */

#ifndef BRIDGE_H
#define BRIDGE_H

#include "etl_profile.h"
#include <stdint.h>

#if defined(ARDUINO_ARCH_AVR)
#include <avr/wdt.h>
#endif

#include "config/bridge_config.h"
#include "hal/hal.h"
#include "protocol/BridgeEvents.h"
#include "fsm/bridge_fsm.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "protocol/rle.h"
#include "security/security.h"

#include <PacketSerial.h>
#include <Codecs/COBS.h>

#include <etl/array.h>
#include <etl/callback_timer.h>
#include <etl/delegate.h>
#include <etl/expected.h>
#include <etl/fsm.h>
#include <etl/observer.h>
#include <etl/pool.h>
#include <etl/queue.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <etl/variant.h>
#include <etl/vector.h>

/**
 * @brief Concrete structure for TX payload storage.
 */
struct TxPayloadBuffer {
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> data;
};

/**
 * @brief Concrete structure for pending TX frames.
 */
struct PendingTxFrame {
  uint16_t command_id;
  uint16_t payload_length;
  TxPayloadBuffer* buffer;
};

namespace bridge::router {
  struct CommandContext {
    const rpc::Frame* frame;
    uint16_t raw_command;
    uint16_t sequence_id;
    bool is_duplicate;
    bool requires_ack;

    CommandContext(const rpc::Frame* f, uint16_t cmd, uint16_t seq, bool dup, bool ack)
        : frame(f), raw_command(cmd), sequence_id(seq), is_duplicate(dup), requires_ack(ack) {}
  };
}

class BridgeClass : public etl::observable<BridgeObserver, 4> {
 public:
  explicit BridgeClass(Stream& stream);

  void begin(uint32_t baudrate = 0, const char* secret = nullptr);
  void process();
  bool isSynchronized() const;
  void enterSafeState();
  void forceSafeState();

  // [MIL-SPEC] Cryptographic Integrity
  bool runPowerOnSelfTests();

  void emitStatus(rpc::StatusCode s, etl::string_view m = {});
  void emitStatus(rpc::StatusCode s, etl::span<const uint8_t> p);
  void emitStatus(rpc::StatusCode s, const __FlashStringHelper* m);

  void signalXoff();
  void signalXon();

  [[nodiscard]] bool sendFrame(rpc::StatusCode s, uint16_t seq = 0, etl::span<const uint8_t> p = {});
  [[nodiscard]] bool sendFrame(rpc::CommandId c, uint16_t seq = 0, etl::span<const uint8_t> p = {});

  template <typename T>
  [[nodiscard]] bool send(rpc::CommandId c, uint16_t seq, const T& packet) {
    msgpack::Encoder enc(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE);
    if (packet.encode(enc)) return sendFrame(c, seq, enc.result());
    return false;
  }

  // --- [PURE TEMPLATE DISPATCH INFRASTRUCTURE] ---
  template <uint16_t ID, void (BridgeClass::*Handler)(const bridge::router::CommandContext&)>
  struct BehaviorCmd {
    static constexpr uint16_t id = ID;
    static void run(BridgeClass* b, const bridge::router::CommandContext& ctx) { (b->*Handler)(ctx); }
  };

  template <uint16_t ID, typename TPacket, void (BridgeClass::*Handler)(const TPacket&)>
  struct BehaviorBridgePayload {
    static constexpr uint16_t id = ID;
    static void run(BridgeClass* b, const bridge::router::CommandContext& ctx) {
      b->_withPayloadAck<TPacket>(ctx, [b](const TPacket& msg) { (b->*Handler)(msg); });
    }
  };

  template <uint16_t ID, typename TPacket, typename TService, TService& Service, void (TService::*Method)(const TPacket&)>
  struct BehaviorServicePayload {
    static constexpr uint16_t id = ID;
    static void run(BridgeClass* b, const bridge::router::CommandContext& ctx) {
      b->_withPayloadAck<TPacket>(ctx, [](const TPacket& msg) { (Service.*Method)(msg); });
    }
  };

  template <uint16_t ID, typename TPacket, typename TAdapter, void (TAdapter::*Method)(const TPacket&)>
  struct BehaviorGpioPayload {
    static constexpr uint16_t id = ID;
    static void run(BridgeClass* b, const bridge::router::CommandContext& ctx) {
      b->_withPayloadAck<TPacket>(ctx, [b](const TPacket& msg) { (b->_gpio_adapter.*Method)(msg); });
    }
  };

  template <uint16_t ID, typename TRespPacket, rpc::CommandId RespID, bool (*Validator)(uint8_t), int (*Reader)(uint8_t)>
  struct BehaviorPinRead {
    static constexpr uint16_t id = ID;
    static void run(BridgeClass* b, const bridge::router::CommandContext& ctx) {
      b->_handlePinRead<TRespPacket>(ctx, RespID, Validator, Reader);
    }
  };

  template <uint8_t GID, bool Enabled, typename... Behaviors>
  struct Group {
    static constexpr uint8_t gid = GID;
    static constexpr bool enabled = Enabled;
    static bool dispatch(BridgeClass* b, const bridge::router::CommandContext& ctx) {
      if constexpr (Enabled) return ((ctx.raw_command == Behaviors::id ? (Behaviors::run(b, ctx), true) : false) || ...);
      return false;
    }
  };

  template <typename... Groups>
  void _dispatchRoot(const bridge::router::CommandContext& ctx) {
    const uint8_t target_gid = static_cast<uint8_t>(ctx.raw_command >> rpc::RPC_COMMAND_GROUP_SHIFT);
    if (!((target_gid == Groups::gid ? (Groups::dispatch(this, ctx), true) : false) || ...)) {
      onUnknownCommand(ctx);
    }
  }

  using CommandHandler = etl::delegate<void(const rpc::Frame&)>;
  using StatusHandler = etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;
  void onCommand(CommandHandler h) { _command_handler = h; }
  void onStatus(StatusHandler h) { _status_handler = h; }
  void flushStream() { _stream.flush(); }

  // [VISIBILITY FOR TESTS AND TEMPLATES]
  void _dispatchCommand(const rpc::Frame& frame);
  void _onStartupStabilized();
  void _onAckTimeout();
  void _retransmitLastFrame();
  void _computeHandshakeTag(const etl::span<const uint8_t> nonce, etl::span<uint8_t> tag);
  bool _isSecurityCheckPassed(uint16_t command_id) const;
  void _onPacketReceived(etl::span<const uint8_t> packet);
  
  void _handleStatusAck(const bridge::router::CommandContext& ctx);
  void _handleStatusMalformed(const bridge::router::CommandContext& ctx);
  void _handleGetVersion(const bridge::router::CommandContext& ctx);
  void _handleGetFreeMemory(const bridge::router::CommandContext& ctx);
  void _handleLinkSync(const bridge::router::CommandContext& ctx);
  void _handleLinkReset(const bridge::router::CommandContext& ctx);
  void _handleGetCapabilities(const bridge::router::CommandContext& ctx);
  void _handleSpiBegin(const bridge::router::CommandContext& ctx);
  void _handleSpiEnd(const bridge::router::CommandContext& ctx);
  void _handleSpiTransfer(const bridge::router::CommandContext& ctx);
  void _handleXoff(const bridge::router::CommandContext& ctx);
  void _handleXon(const bridge::router::CommandContext& ctx);
  void _handleSetBaudrate(const rpc::payload::SetBaudratePacket& msg);
  void _handleEnterBootloader(const rpc::payload::EnterBootloader& msg);

  void onUnknownCommand(const bridge::router::CommandContext& ctx);
  void _markRxProcessed(const rpc::Frame& frame);
  void _sendRawFrame(uint16_t cmd, uint16_t seq, etl::span<const uint8_t> payload);

  template <typename T> void _sendPbResponse(rpc::StatusCode s, uint16_t seq, const T& msg) {
    msgpack::Encoder enc(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE);
    if (msg.encode(enc)) _sendRawFrame(rpc::to_underlying(s), seq, enc.result());
  }
  template <typename T> void _sendPbResponse(rpc::CommandId c, uint16_t seq, const T& msg) {
    msgpack::Encoder enc(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE);
    if (msg.encode(enc)) _sendRawFrame(rpc::to_underlying(c), seq, enc.result());
  }

  void _sendError(rpc::StatusCode s, uint16_t cmd, uint16_t seq);
  void _sendAckAndFlush(uint16_t cmd, uint16_t seq);
  bool _sendFrame(uint16_t cmd, uint16_t seq, etl::span<const uint8_t> payload);
  bool _isHandshakeCommand(uint16_t cmd) const;
  void _handleReceivedFrame(etl::span<const uint8_t> payload);
  etl::expected<void, rpc::FrameError> _decompressFrame(const rpc::Frame& org, rpc::Frame& eff);
  void _applyTimingConfig(const rpc::payload::HandshakeConfig& msg);

  template <typename F> void _withAck(const bridge::router::CommandContext& ctx, F handler) { if (!ctx.is_duplicate) handler(); if (ctx.requires_ack) _sendAckAndFlush(ctx.raw_command, ctx.sequence_id); }
  template <typename F> void _withResponse(const bridge::router::CommandContext& ctx, F handler) { if (!ctx.is_duplicate) handler(); }
  template <typename T, typename F> void _withPayloadAck(const bridge::router::CommandContext& ctx, F handler) { if (!ctx.is_duplicate) { auto res = rpc::Payload::parse<T>(*ctx.frame); if (res) handler(res.value()); } if (ctx.requires_ack) _sendAckAndFlush(ctx.raw_command, ctx.sequence_id); }
  template <typename T, typename F> void _withPayload(const bridge::router::CommandContext& ctx, F handler) { auto res = rpc::Payload::parse<T>(*ctx.frame); if (res) handler(res.value()); }
  template <typename T, typename TID, typename TValid, typename TRead> void _handlePinRead(const bridge::router::CommandContext& ctx, TID resp_id, TValid valid, TRead read) {
    _withResponse(ctx, [this, &ctx, resp_id, valid, read]() {
      auto res = rpc::Payload::parse<rpc::payload::PinRead>(*ctx.frame);
      if (res && valid(res->pin)) { T resp = {static_cast<decltype(T::value)>(read(res->pin))}; _sendPbResponse(resp_id, ctx.sequence_id, resp); }
      else emitStatus(rpc::StatusCode::STATUS_ERROR);
    });
  }

  Stream& _stream; HardwareSerial* _hardware_serial; CommandHandler _command_handler; StatusHandler _status_handler;
  uint16_t _last_command_id; uint16_t _tx_sequence_id; uint8_t _retry_count; uint8_t _retry_limit; uint16_t _ack_timeout_ms; uint32_t _response_timeout_ms; uint32_t _pending_baudrate; uint8_t _consecutive_crc_errors; rpc::FrameError _last_parse_error;
  uint32_t _last_timer_ms;
  
  // [SIL-2] Reordered for -Wreorder compliance
  etl::array<uint8_t, 4> _timer_ids;
  etl::callback_timer<4> _timers;
  bridge::fsm::BridgeFsm _fsm;
  rpc::FrameBuilder _frame_builder;
  rpc::FrameParser _frame_parser;
  
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> _rx_storage;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE + 2> _transient_buffer;
  
  PacketSerial2::PacketSerial<PacketSerial2::COBS, PacketSerial2::NoCRC, PacketSerial2::NoLock, PacketSerial2::NoWatchdog> _packet_serial;
  
  etl::vector<uint8_t, 32> _shared_secret;
  
  struct GpioAdapter {
    void setPinMode(const rpc::payload::PinMode& m) { if (bridge::hal::isValidPin(m.pin)) ::pinMode(m.pin, m.mode); else _bridge.emitStatus(rpc::StatusCode::STATUS_ERROR); }
    void digitalWrite(const rpc::payload::DigitalWrite& m) { if (bridge::hal::isValidPin(m.pin)) ::digitalWrite(m.pin, m.value); else _bridge.emitStatus(rpc::StatusCode::STATUS_ERROR); }
    void analogWrite(const rpc::payload::AnalogWrite& m) { if (bridge::hal::isValidPin(m.pin)) ::analogWrite(m.pin, m.value); else _bridge.emitStatus(rpc::StatusCode::STATUS_ERROR); }
    explicit GpioAdapter(BridgeClass& b) : _bridge(b) {}
    BridgeClass& _bridge;
  } _gpio_adapter;

  etl::pool<TxPayloadBuffer, bridge::config::TX_QUEUE_CAPACITY> _tx_payload_pool;
  etl::queue<PendingTxFrame, bridge::config::TX_QUEUE_CAPACITY> _pending_tx_queue;
  struct RxHistory {
    etl::array<uint16_t, bridge::config::RX_HISTORY_SIZE> buffer;
    uint8_t head = 0;
    void push(uint16_t seq) { buffer[head] = seq; head = (head + 1) % bridge::config::RX_HISTORY_SIZE; }
    bool exists(uint16_t seq) const { for (auto s : buffer) if (s == seq) return true; return false; }
    void clear() { buffer.fill(0xFFFF); }
  } _rx_history;
  bool _tx_enabled;

  void _onRxDedupe();
  void _onBaudrateChange();
  void _clearPendingTxQueue();
  void _clearAckState();
  void _flushPendingTxQueue();
  void _handleAck(uint16_t command_id);
  void _handleMalformed(uint16_t command_id);
};

extern BridgeClass Bridge;

#endif

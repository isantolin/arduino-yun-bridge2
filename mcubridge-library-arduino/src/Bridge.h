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

#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/callback_timer.h>
#include <etl/delegate.h>
#include <etl/expected.h>
#include <etl/fsm.h>
#include <etl/pool.h>
#include <etl/queue.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <etl/variant.h>
#include <etl/vector.h>

// Forward declaration for friend class
namespace bridge { namespace test { class TestAccessor; } }

namespace rpc {
  class Serializable {
   public:
    virtual bool encode(msgpack::Encoder& enc) const = 0;
  };
}

namespace bridge {
namespace router {
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
}

#include "ErrorPolicy.h"

class BridgeClass {
 public:
  using ErrorPolicy = bridge::SafeStatePolicy;
  explicit BridgeClass(Stream& stream);

  void notify_observers(const MsgBridgeSynchronized& msg);
  void notify_observers(const MsgBridgeLost& msg);

  void begin(uint32_t baudrate = 0, const char* secret = nullptr);
  void process();
  bool isSynchronized() const;
  void enterSafeState();
  static void forceSafeState();

  static bool runPowerOnSelfTests();

  template <rpc::StatusCode S>
  void emitStatus() { emitStatus(S, etl::span<const uint8_t>()); }

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

  using CommandHandler = etl::delegate<void(const rpc::Frame&)>;
  using StatusHandler = etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;
  [[maybe_unused]] void onCommand(CommandHandler h) { _command_handler = h; }
  [[maybe_unused]] void onStatus(StatusHandler h) { _status_handler = h; }
  void flushStream() { _stream.flush(); }

  [[maybe_unused]] void _computeHandshakeTag(const etl::span<const uint8_t> nonce, etl::span<uint8_t> tag);

  void _dispatchCommand(const rpc::Frame& frame);
  void _onStartupStabilized();
  void _onAckTimeout();
  void _onRxDedupe();
  void _onBaudrateChange();
  void _retransmitLastFrame();
  bool _isSecurityCheckPassed(uint16_t command_id) const;
  void _onPacketReceived(etl::span<const uint8_t> packet);

  struct GpioAdapter {
    void setPinMode(const rpc::payload::PinMode& m) { if (bridge::hal::isValidPin(m.pin)) ::pinMode(m.pin, m.mode); else _bridge.emitStatus<rpc::StatusCode::STATUS_ERROR>(); }
    void digitalWrite(const rpc::payload::DigitalWrite& m) { if (bridge::hal::isValidPin(m.pin)) ::digitalWrite(m.pin, m.value); else _bridge.emitStatus<rpc::StatusCode::STATUS_ERROR>(); }
    void analogWrite(const rpc::payload::AnalogWrite& m) { if (bridge::hal::isValidPin(m.pin)) ::analogWrite(m.pin, m.value); else _bridge.emitStatus<rpc::StatusCode::STATUS_ERROR>(); }
    explicit GpioAdapter(BridgeClass& b) : _bridge(b) {}
    BridgeClass& _bridge;
  };

 private:
  struct TxPayloadBuffer { etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> data; };
  struct PendingTxFrame { uint16_t command_id; uint16_t sequence_id; TxPayloadBuffer* buffer; size_t length; };

  // STRICT ORDER FOR CONSTRUCTOR
  Stream& _stream;
  HardwareSerial* _hardware_serial;
  CommandHandler _command_handler;
  StatusHandler _status_handler;
  uint16_t _last_command_id;
  uint16_t _tx_sequence_id;
  uint8_t _retry_count;
  uint8_t _retry_limit;
  uint16_t _ack_timeout_ms;
  uint32_t _response_timeout_ms;
  uint32_t _pending_baudrate;
  uint8_t _consecutive_crc_errors;
  rpc::FrameError _last_parse_error;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> _ps_rx_storage;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> _ps_work_buffer;
  PacketSerial2::PacketSerial<PacketSerial2::COBS, PacketSerial2::NoCRC, PacketSerial2::NoLock, PacketSerial2::NoWatchdog> _packet_serial;

  etl::vector<uint8_t, 32> _shared_secret;
  bridge::fsm::BridgeFsm _fsm;
  etl::callback_timer<bridge::scheduler::NUMBER_OF_TIMERS> _timers;
  etl::array<etl::timer::id::type, bridge::scheduler::NUMBER_OF_TIMERS> _timer_ids;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> _transient_buffer;
  etl::array<uint8_t, 256> _rx_storage;
  rpc::FrameParser _frame_parser;
  bool _is_post_passed;
  bool _tx_enabled;

  GpioAdapter _gpio_adapter;
  etl::pool<TxPayloadBuffer, bridge::config::TX_QUEUE_CAPACITY> _tx_payload_pool;
  etl::queue<PendingTxFrame, bridge::config::TX_QUEUE_CAPACITY> _pending_tx_queue;

  struct RxHistory {
    etl::array<uint16_t, bridge::config::RX_HISTORY_SIZE> buffer;
    uint8_t head = 0;
    void push(uint16_t seq) { buffer[head] = seq; head = (head + 1) % bridge::config::RX_HISTORY_SIZE; }
    bool exists(uint16_t seq) const { return etl::find(buffer.begin(), buffer.end(), seq) != buffer.end(); }
    void clear() { buffer.fill(0xFFFF); }
  } _rx_history;

  [[nodiscard]] bool _sendFrame(uint16_t command_id, uint16_t sequence_id, etl::span<const uint8_t> payload);
  void _sendRawFrame(uint16_t command_id, uint16_t sequence_id, etl::span<const uint8_t> payload);
  [[nodiscard]] etl::expected<void, rpc::FrameError> _decompressFrame(const rpc::Frame& in, rpc::Frame& out);
  [[maybe_unused]] void _applyTimingConfig(const rpc::payload::HandshakeConfig& msg);

  template <typename T>  void _sendPbResponse(rpc::CommandId c, uint16_t seq, const T& packet) {
    msgpack::Encoder enc(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE);
    if (packet.encode(enc)) (void)sendFrame(c, seq, enc.result());
  }

  void _handleSetBaudrateCommand(const bridge::router::CommandContext& ctx);
  void _handleEnterBootloaderCommand(const bridge::router::CommandContext& ctx);
  void _handleSetPinModeCommand(const bridge::router::CommandContext& ctx);
  void _handleDigitalWriteCommand(const bridge::router::CommandContext& ctx);
  void _handleAnalogWriteCommand(const bridge::router::CommandContext& ctx);
  void _handleDigitalReadCommand(const bridge::router::CommandContext& ctx);
  void _handleAnalogReadCommand(const bridge::router::CommandContext& ctx);
  void _handleConsoleWriteCommand(const bridge::router::CommandContext& ctx);
#if BRIDGE_ENABLE_DATASTORE
  void _handleDataStoreGetResponseCommand(const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_MAILBOX
  void _handleMailboxPushCommand(const bridge::router::CommandContext& ctx);
  void _handleMailboxReadResponseCommand(const bridge::router::CommandContext& ctx);
  void _handleMailboxAvailableResponseCommand(const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  void _handleFileWriteCommand(const bridge::router::CommandContext& ctx);
  void _handleFileReadCommand(const bridge::router::CommandContext& ctx);
  void _handleFileRemoveCommand(const bridge::router::CommandContext& ctx);
  void _handleFileReadResponseCommand(const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_PROCESS
  void _handleProcessKillCommand(const bridge::router::CommandContext& ctx);
  void _handleProcessRunAsyncResponseCommand(const bridge::router::CommandContext& ctx);
  void _handleProcessPollResponseCommand(const bridge::router::CommandContext& ctx);
#endif
#if BRIDGE_ENABLE_SPI
  void _handleSpiSetConfigCommand(const bridge::router::CommandContext& ctx);
#endif

  void _handleStatusMalformed(const bridge::router::CommandContext& ctx);
  void _handleStatusAck(const bridge::router::CommandContext& ctx);
  void _handleGetVersion(const bridge::router::CommandContext& ctx);
  void _handleGetFreeMemory(const bridge::router::CommandContext& ctx);
  void _handleLinkSync(const bridge::router::CommandContext& ctx);
  void _handleLinkReset(const bridge::router::CommandContext& ctx);
  void _handleGetCapabilities(const bridge::router::CommandContext& ctx);
  void _handleXoff(const bridge::router::CommandContext& ctx);
  void _handleXon(const bridge::router::CommandContext& ctx);
  void _handleSetBaudrate(const rpc::payload::SetBaudratePacket& msg);
  void _handleSetTiming(const rpc::payload::HandshakeConfig& msg);
  void _handleEnterBootloader(const rpc::payload::EnterBootloader& msg);
  void _handleSpiBegin(const bridge::router::CommandContext& ctx);
  void _handleSpiEnd(const bridge::router::CommandContext& ctx);
  void _handleSpiTransfer(const bridge::router::CommandContext& ctx);
  void _handleReceivedFrame(etl::span<const uint8_t> p);
  void onUnknownCommand(const bridge::router::CommandContext& ctx);

  template <typename T, typename F> void _withPayload(const bridge::router::CommandContext& ctx, F handler) { auto res = rpc::Payload::parse<T>(*ctx.frame); if (res) handler(res.value()); }
  template <typename T, typename F> void _withPayloadAck(const bridge::router::CommandContext& ctx, F handler) {
    if (ctx.is_duplicate) { (void)sendFrame(rpc::StatusCode::STATUS_ACK, ctx.sequence_id); return; }
    auto res = rpc::Payload::parse<T>(*ctx.frame);
    if (res) { handler(res.value()); if (ctx.requires_ack) (void)sendFrame(rpc::StatusCode::STATUS_ACK, ctx.sequence_id); }
    else emitStatus<rpc::StatusCode::STATUS_ERROR>();
  }
  template <typename F> void _withResponse(const bridge::router::CommandContext& ctx, F handler) {
    if (ctx.is_duplicate) { _retransmitLastFrame(); return; }
    handler();
  }
  template <typename T, typename TID, typename TValid, typename TRead> void _handlePinRead(const bridge::router::CommandContext& ctx, TID resp_id, TValid valid, TRead read) {
    _withResponse(ctx, [this, &ctx, resp_id, valid, read]() {
      auto res = rpc::Payload::parse<rpc::payload::PinRead>(*ctx.frame);
      if (res && valid(res->pin)) { T resp = {static_cast<decltype(T::value)>(read(res->pin))}; _sendPbResponse(resp_id, ctx.sequence_id, resp); }
      else emitStatus<rpc::StatusCode::STATUS_ERROR>();
    });
  }
  void _clearPendingTxQueue();
  void _clearAckState();
  void _flushPendingTxQueue();
  void _handleAck(uint16_t command_id);
  void _handleMalformed(uint16_t command_id);

  friend class bridge::test::TestAccessor;
};

extern BridgeClass Bridge;

#endif

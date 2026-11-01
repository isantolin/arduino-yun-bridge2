/**
 * @file Bridge.h
 * @brief Arduino MCU Bridge v2 - MCU-side Pigweed HDLC and RPC library.
 */

#ifndef BRIDGE_H
#define BRIDGE_H

#include <Arduino.h>
#include <stdint.h>

#if defined(ARDUINO_ARCH_AVR)
#include <avr/wdt.h>
#endif

#ifdef min
#undef min
#endif
#ifdef max
#undef max
#endif
#ifdef round
#undef round
#endif

#include <PigweedAVR.h>

#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

// clang-format off
#if defined(ARDUINO_BOARD)
#pragma push_macro("ARDUINO_BOARD")
#undef ARDUINO_BOARD
#include <Embedded_Template_Library.h>
#pragma pop_macro("ARDUINO_BOARD")
#else
#include <Embedded_Template_Library.h>
#endif

#include <wolfssl.h>
#include <wolfssl/wolfcrypt/settings.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/bitset.h>
#include <etl/callback_timer.h>
#include <etl/debounce.h>
#include <etl/delegate.h>
#include <etl/fsm.h>
#include <etl/observer.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <etl/vector.h>
// clang-format on

#include "config/bridge_config.h"
#include "fsm/bridge_fsm.h"
#include "hal/hal.h"
#include "protocol/mcu_wire.pb.h"
#include "pb_encode.h"
#include "pb_decode.h"
#include "protocol/rpc_protocol.h"
#include "security/security.h"

namespace bridge {

enum class SystemEvent : uint8_t {
  SAFE_STATE_ENTERED = 0,
  SYNCHRONIZED = 1,
  UNSYNCHRONIZED = 2
};

class BridgeObserver {
 public:
  virtual ~BridgeObserver() = default;
  virtual void notification(SystemEvent event) {
    if (event == SystemEvent::SAFE_STATE_ENTERED ||
        event == SystemEvent::UNSYNCHRONIZED) {
      onLost();
    }
  }
  virtual void onLost() {}
};

namespace pb {
inline void copy_string(char* dst, size_t dst_cap, etl::string_view src) {
  if (dst_cap == 0) return;
  const size_t len = etl::min(src.size(), dst_cap - 1);
  etl::copy_n(src.data(), len, dst);
  dst[len] = '\0';
}

template <typename PbBytesField>
inline void copy_bytes(PbBytesField& dst, etl::span<const uint8_t> src) {
  const size_t len = etl::min(src.size(), sizeof(dst.bytes));
  etl::copy_n(src.data(), len, dst.bytes);
  dst.size = static_cast<pb_size_t>(len);
}

template <typename PbBytesField>
inline void copy_bytes(PbBytesField& dst, const uint8_t* src, size_t size) {
  const size_t len = etl::min(size, sizeof(dst.bytes));
  etl::copy_n(src, len, dst.bytes);
  dst.size = static_cast<pb_size_t>(len);
}
}  // namespace pb

}  // namespace bridge

class BridgeClass : public etl::observable<bridge::BridgeObserver,
                                           bridge::config::MAX_OBSERVERS> {
 public:
  explicit BridgeClass(Stream& stream);

  void setStream(Stream& stream);
  void begin(uint32_t baudrate = 0, const char* secret = nullptr);
  void process();
  bool isSynchronized() const;
  bool awaitSynchronization(
      uint32_t timeout_ms = bridge::config::SYNC_TIMEOUT_MS,
      uint32_t poll_delay_ms = 2);
  bool isPostPassed() const { return _state_flags.test(FLAG_POST_PASSED); }
  uint32_t getWcetMaxMicros() const { return _wcet_max_micros; }
  void resetWcetStats() { _wcet_max_micros = 0; }

  void enterSafeState();

  using SafetyPinState = bridge::hal::SafetyPinState;
  struct SafetyPinEntry {
    uint8_t pin;
    SafetyPinState state;
  };
  void registerSafetyPin(uint8_t pin, SafetyPinState state);
  void clearSafetyPins();
  void setWatchdogTimeout(uint32_t timeout_ms) {
    _watchdog_timeout_ms = timeout_ms;
  }
  uint32_t getWatchdogTimeout() const { return _watchdog_timeout_ms; }

  using StatusHandler =
      etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;
  void onStatus(StatusHandler h) { _status_handler = h; }

  void emitStatus(rpc::StatusCode s, etl::span<const uint8_t> p = {}) {
    if (_status_handler.is_valid()) {
      _status_handler(s, p);
    }
  }

  void emitStatus(rpc::StatusCode s, etl::string_view p) {
    emitStatus(s, etl::span<const uint8_t>(
                      reinterpret_cast<const uint8_t*>(p.data()), p.size()));
  }

  [[nodiscard]] bool sendPinEvent(uint8_t pin, uint16_t value);

  template <uint16_t ValidCount = 1>
  [[nodiscard]] bool sendDebouncedPinEvent(
      uint8_t pin, bool raw_state, etl::debounce<ValidCount>& debouncer) {
    if (debouncer.add(raw_state)) {
      return sendPinEvent(pin, debouncer.is_set() ? 1U : 0U);
    }
    return true;
  }

  [[nodiscard]] bool sendConsoleStream(etl::span<const uint8_t> data);

  template <typename ProtoMsg>
  [[nodiscard]] bool sendRpcRequest(uint32_t service_id, uint32_t method_id,
                                    const pb_msgdesc_t* fields,
                                    const ProtoMsg& msg, uint32_t call_id = 0) {
    if (!_stream || !_state_flags.test(FLAG_TX_ENABLED)) return false;
    uint8_t payload_buf[rpc::MAX_PAYLOAD_SIZE];
    pb_ostream_t pb_stream =
        pb_ostream_from_buffer(payload_buf, sizeof(payload_buf));
    if (fields != nullptr && !pb_encode(&pb_stream, fields, &msg)) {
      return false;
    }

    pw::rpc_avr::RpcPacket packet;
    packet.type = pw::rpc_avr::PacketType::kRequest;
    packet.channel_id = 1;
    packet.service_id = service_id;
    packet.method_id = method_id;
    packet.call_id = call_id;
    packet.payload = pw::ConstByteSpan(
        reinterpret_cast<const std::byte*>(payload_buf),
        pb_stream.bytes_written);

    uint8_t frame_buf[bridge::config::RX_BUFFER_SIZE];
    auto encode_res =
        packet.Encode(pw::as_writable_bytes(pw::span(frame_buf)));
    if (!encode_res.ok()) return false;

    auto write_res = pw::hdlc::WriteUIFrame(
        1,
        pw::ConstByteSpan(reinterpret_cast<const std::byte*>(frame_buf),
                          encode_res.size()),
        _arduino_stream);
    return write_res.ok();
  }

  [[nodiscard]] bool sendRpcRequestNoPayload(uint32_t service_id,
                                            uint32_t method_id,
                                            uint32_t call_id = 0) {
    if (!_stream || !_state_flags.test(FLAG_TX_ENABLED)) return false;
    pw::rpc_avr::RpcPacket packet;
    packet.type = pw::rpc_avr::PacketType::kRequest;
    packet.channel_id = 1;
    packet.service_id = service_id;
    packet.method_id = method_id;
    packet.call_id = call_id;

    uint8_t frame_buf[bridge::config::RX_BUFFER_SIZE];
    auto encode_res =
        packet.Encode(pw::as_writable_bytes(pw::span(frame_buf)));
    if (!encode_res.ok()) return false;

    auto write_res = pw::hdlc::WriteUIFrame(
        1,
        pw::ConstByteSpan(reinterpret_cast<const std::byte*>(frame_buf),
                          encode_res.size()),
        _arduino_stream);
    return write_res.ok();
  }

  void _onBaudrateChange();
  void _onHandshakeTimeout();

  static void _handleSetPinMode(const rpc_pb_PinMode& m);
  template <typename MsgType, bool IsAnalog>
  static void _handlePinWrite(const MsgType& m) {
#if defined(ARDUINO_ARCH_ESP32)
    if (!digitalPinIsValid(m.pin)) return;
#endif
    if constexpr (IsAnalog) {
      analogWrite(m.pin,
                  static_cast<int>(etl::clamp<uint32_t>(m.value, 0UL, 255UL)));
    } else {
      pinMode(m.pin, OUTPUT);
      digitalWrite(m.pin, (m.value == 0) ? LOW : HIGH);
    }
  }

  rpc_pb_PinSubscribeResponse _handlePinSubscribe(
      const rpc_pb_PinSubscribeRequest& m);
  rpc_pb_LinkSync _handleLinkSync(const rpc_pb_LinkSync& m);
  void _handleLinkReset();
  void _handleSetBaudrate(const rpc_pb_SetBaudratePacket& msg);
  void _handleEnterBootloader(const rpc_pb_EnterBootloader& msg);

  static void _fillVersion(rpc_pb_VersionResponse& resp);
  static void _fillFreeMemory(rpc_pb_FreeMemoryResponse& resp);
  static void _fillCapabilities(rpc_pb_Capabilities& resp);

 protected:
  void _initializeRuntime();
  void _initObservers();
  void _handleReceivedHdlcFrame(const pw::hdlc::Frame& frame);
  void _dispatchRpcResponse(const pw::rpc_avr::RpcPacket& packet);

  Stream* _stream = nullptr;
  HardwareSerial* _hardware_serial = nullptr;
  avr_port::ArduinoStream _arduino_stream;
  pw::hdlc::DecoderBuffer<bridge::config::RX_BUFFER_SIZE> _hdlc_decoder;
  pw::rpc_avr::RpcServer<8> _rpc_server;

  StatusHandler _status_handler;
  uint32_t _pending_baudrate = 0;
  uint32_t _response_timeout_ms = rpc::RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS;

  etl::vector<uint8_t, 64> _shared_secret;
  bridge::fsm::BridgeFsm _fsm;

  void _watchdogTask();
  void _serialTask();
  void _timerTask();
  void _subscriptionTask();

  enum StateFlags : uint8_t {
    FLAG_POST_PASSED = 0,
    FLAG_TX_ENABLED = 1,
    FLAG_SERIAL_XOFF = 2,
    FLAG_COUNT = 3
  };

  uint32_t _timer_last_tick_ms = 0;
  etl::bitset<FLAG_COUNT> _state_flags;
  etl::callback_timer<bridge::scheduler::NUMBER_OF_TIMERS> _timers;
  uint32_t _wcet_max_micros = 0;

  struct PinSubscription {
    uint8_t pin{0};
    uint8_t mode{0};
    uint16_t interval_ms{0};
    uint16_t hysteresis{0};
    uint16_t last_value{0};
    uint32_t last_report_ms{0};
    bool active{false};
  };
  etl::vector<SafetyPinEntry, bridge::config::MAX_SAFETY_PINS> _safety_pins;
  etl::vector<PinSubscription, bridge::config::MAX_PIN_SUBSCRIPTIONS>
      _pin_subscriptions;
  uint32_t _watchdog_timeout_ms{0};
  uint32_t _last_rx_ms{0};
};

#ifndef BRIDGE_NO_GLOBAL_EXTERN
extern BridgeClass Bridge;
#endif

#endif  // BRIDGE_H

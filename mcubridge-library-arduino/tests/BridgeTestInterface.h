#ifndef BRIDGE_TEST_INTERFACE_H
#define BRIDGE_TEST_INTERFACE_H

#ifndef BRIDGE_ENABLE_TEST_INTERFACE
#define BRIDGE_ENABLE_TEST_INTERFACE
#endif

#include <pw_rpc/internal/packet.h>
#include <pw_span/span.h>

#include "Bridge.h"
#include "security/security.h"

namespace bridge::test {

class TestAccessor : public BridgeClass {
 public:
  explicit TestAccessor(Stream& s) : BridgeClass(s) {}

  static TestAccessor& create(BridgeClass& b) {
    return static_cast<TestAccessor&>(b);
  }

  void setSynchronized() {
    _fsm.receive(bridge::fsm::EvHandshakeStart());
    _fsm.receive(bridge::fsm::EvHandshakeComplete());
    _state_flags.set(FLAG_TX_ENABLED, true);
  }

  bool isFault() const {
    return _fsm.get_state_id() ==
           static_cast<etl::fsm_state_id_t>(bridge::fsm::StateId::FAULT);
  }
  etl::fsm_state_id_t get_state_id() const { return _fsm.get_state_id(); }
  bool isUnsynchronized() const {
    return _fsm.get_state_id() == static_cast<etl::fsm_state_id_t>(
                                      bridge::fsm::StateId::UNSYNCHRONIZED);
  }
  bool getStartupStabilizing() const {
    return _fsm.get_state_id() ==
           static_cast<etl::fsm_state_id_t>(bridge::fsm::StateId::STARTUP);
  }

  template <typename TEvent>
  void trigger(const TEvent& ev) {
    _fsm.receive(ev);
  }

  bool isSharedSecretEmpty() const { return _shared_secret.empty(); }
  bool isTxEnabled() const { return _state_flags.test(FLAG_TX_ENABLED); }
  void setTxEnabled(bool enabled) {
    _state_flags.set(FLAG_TX_ENABLED, enabled);
  }
  void setSharedSecret(pw::span<const uint8_t> secret) {
    _shared_secret.assign(secret.begin(), secret.end());
  }

  void computeHandshakeTag(const uint8_t* nonce_ptr, size_t len,
                           uint8_t* tag_out) {
    if (_shared_secret.empty()) return;
    etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> out_tag = {};
    if (rpc::security::handshake_generate_tag(
            pw::span<const uint8_t>(_shared_secret),
            pw::span<const uint8_t>(nonce_ptr, len),
            pw::span<uint8_t>(out_tag))) {
      etl::copy_n(out_tag.begin(), out_tag.size(), tag_out);
    }
  }

  void onHandshakeTimeout() { _onHandshakeTimeout(); }

  void invokeHdlcFrameReceived(const pw::hdlc::Frame& frame) {
    _handleReceivedHdlcFrame(frame);
  }

  void feedHdlcByte(uint8_t b) {
    auto res =
        _hdlc_decoder.Process(static_cast<pw::ByteSpan::element_type>(b));
    if (res.ok()) {
      _handleReceivedHdlcFrame(res.value());
    }
  }

  void feedHdlcBytes(pw::span<const uint8_t> data) {
    for (uint8_t b : data) {
      feedHdlcByte(b);
    }
  }

  void invokeRpcResponse(const pw::rpc::internal::Packet& pkt) {
    pw::rpc::internal::Packet copy = pkt;
    if (copy.channel_id() == 0) {
      copy.set_channel_id(1);
    }
    uint8_t buf[bridge::config::RX_BUFFER_SIZE];
    auto res = copy.Encode(pw::as_writable_bytes(pw::span(buf)));
    if (res.ok()) {
      (void)_rpc_client.ProcessPacket(res.value());
    }
  }

  void setPendingBaudrate(uint32_t b) { _pending_baudrate = b; }
  void onBaudrateChange() { _onBaudrateChange(); }
  void invokeWatchdog() { _watchdogTask(); }
  void invokeSerialTask() { _serialTask(); }
  void invokeTimerTask() { _timerTask(); }
  bool isSerialXoffSent() const { return _state_flags.test(FLAG_SERIAL_XOFF); }
  void invokeSendFlowControl(bool xoff) { _sendFlowControl(xoff); }

  void startTimersForCoverage() {
    _timers.start(bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT);
    _timers.start(bridge::scheduler::TIMER_BAUDRATE_CHANGE);
  }
  void startBootloaderTimerForCoverage() {
    _timers.start(bridge::scheduler::TIMER_BOOTLOADER_DELAY);
  }
  void setSerialTaskXoffSent(bool value) {
    _state_flags.set(FLAG_SERIAL_XOFF, value);
  }
  void setTimerLastTick(uint32_t tick) { _timer_last_tick_ms = tick; }
  void setHardwareSerial(HardwareSerial* serial) { _hardware_serial = serial; }

  void clearSynchronized() { _fsm.receive(bridge::fsm::EvReset()); }
  void clearSharedSecret() { _shared_secret.clear(); }

  void setNullStream() { _stream = nullptr; }
  void setLastRxMs(uint32_t ms) { _last_rx_ms = ms; }
  void invokeSubscriptionTask() { _subscriptionTask(); }
  void invokeInitializeRuntime() { _initializeRuntime(); }

  void setSubscriptionLastValue(size_t idx, uint16_t val) {
    if (idx < _pin_subscriptions.size()) {
      _pin_subscriptions[idx].last_value = val;
    }
  }
  void setSubscriptionLastReportMs(size_t idx, uint16_t ms) {
    if (idx < _pin_subscriptions.size()) {
      _pin_subscriptions[idx].last_report_ms = ms;
    }
  }
  // Last value reported for a subscribed pin, or -1 when the pin is not
  // subscribed.
  int32_t subscriptionLastValueForPin(uint8_t pin) const {
    for (const auto& sub : _pin_subscriptions) {
      if (sub.pin == pin) return static_cast<int32_t>(sub.last_value);
    }
    return -1;
  }
  void setSubscriptionActive(size_t idx, bool active) {
    if (idx < _pin_subscriptions.size()) {
      _pin_subscriptions[idx].active = active;
    }
  }

  void setIdle() {
    if (!_fsm.is_started()) _fsm.start();
    _fsm.receive(bridge::fsm::EvReset());
  }

  bool isSessionKeysReady() const { return _session_keys_ready; }
  void setSessionKeysReady(bool ready) { _session_keys_ready = ready; }
  void setTxAeadCounter(uint64_t c) { _tx_aead_counter = c; }
  void setRxAeadCounter(uint64_t c) { _rx_aead_counter = c; }
  pw::span<const uint8_t> rxAeadKey() const {
    return pw::span<const uint8_t>(_session_aead_keys.data(),
                                   rpc::RPC_AEAD_KEY_SIZE);
  }
  bool invokeWriteEncryptedHdlcFrameWithBuffer(uint64_t address,
                                               pw::span<const uint8_t> data,
                                               pw::span<uint8_t> envelope) {
    return _writeEncryptedHdlcFrameWithBuffer(address, data, envelope);
  }
  bool invokeDecryptHdlcFrame(uint64_t address,
                              pw::span<const uint8_t> envelope,
                              pw::span<const uint8_t>& plaintext) {
    return _decryptHdlcFrame(address, envelope, plaintext);
  }
  pw::Status invokeChannelSend(pw::ConstByteSpan packet) {
    return _rpc_channel_output.Send(packet);
  }
};

}  // namespace bridge::test

#endif

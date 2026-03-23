/**
 * @file bridge_fsm.h
 * @brief ETL-based Finite State Machine for Arduino MCU Bridge v2
 *
 * [SIL-2 COMPLIANCE - IEC 61508]
 * This module implements a deterministic state machine using ETL's FSM
 * framework. All state transitions are explicit and bounded.
 *
 * States:
 *   - Stabilizing (0): Hardware startup. Draining serial lines.
 *   - Unsynchronized (1): Waiting for link synchronization.
 *   - Syncing (2): Negotiating parameters with Linux.
 *   - Ready (Parent): Super-state for operational domain.
 *     - Idle (3): Operational and ready for commands.
 *     - AwaitingAck (4): Waiting for frame acknowledgement.
 *   - Fault (5): Safety-triggered halt.
 */
#ifndef BRIDGE_FSM_H
#define BRIDGE_FSM_H

#include <etl/fsm.h>
#include <etl/message.h>
#include <etl/array.h>
#include <etl/bitset.h>

namespace bridge {
namespace fsm {

class BridgeFsm;

enum StateId : etl::fsm_state_id_t {
  STATE_STABILIZING = 0,
  STATE_UNSYNCHRONIZED = 1,
  STATE_SYNCING = 2,
  STATE_READY = 3,
  STATE_IDLE = 4,
  STATE_AWAITING_ACK = 5,
  STATE_FAULT = 6,
  NUMBER_OF_STATES = 7
};

enum EventId : etl::message_id_t {
  EVENT_STABILIZED = 0,
  EVENT_HANDSHAKE_START = 1,
  EVENT_HANDSHAKE_COMPLETE = 2,
  EVENT_HANDSHAKE_FAILED = 3,
  EVENT_SEND_CRITICAL = 4,
  EVENT_ACK_RECEIVED = 5,
  EVENT_TIMEOUT = 6,
  EVENT_RESET = 7,
  EVENT_CRYPTO_FAULT = 8
};

struct EvStabilized : public etl::message<EVENT_STABILIZED> {};
struct EvHandshakeStart : public etl::message<EVENT_HANDSHAKE_START> {};
struct EvHandshakeComplete : public etl::message<EVENT_HANDSHAKE_COMPLETE> {};
struct EvHandshakeFailed : public etl::message<EVENT_HANDSHAKE_FAILED> {};
struct EvSendCritical : public etl::message<EVENT_SEND_CRITICAL> {};
struct EvAckReceived : public etl::message<EVENT_ACK_RECEIVED> {};
struct EvTimeout : public etl::message<EVENT_TIMEOUT> {};
struct EvReset : public etl::message<EVENT_RESET> {};
struct EvCryptoFault : public etl::message<EVENT_CRYPTO_FAULT> {};

class StateStabilizing
    : public etl::fsm_state<BridgeFsm, StateStabilizing, STATE_STABILIZING,
                            EvStabilized, EvReset, EvCryptoFault> {
 public:
  etl::fsm_state_id_t on_enter_state() { return STATE_STABILIZING; }
  etl::fsm_state_id_t on_event(const EvStabilized&) { return STATE_UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvReset&) { return No_State_Change; }
  etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class StateUnsynchronized
    : public etl::fsm_state<BridgeFsm, StateUnsynchronized,
                            STATE_UNSYNCHRONIZED, EvHandshakeStart,
                            EvHandshakeFailed, EvReset, EvCryptoFault> {
 public:
  etl::fsm_state_id_t on_enter_state() { return STATE_UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvHandshakeStart&) { return STATE_SYNCING; }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event(const EvReset&) { return No_State_Change; }
  etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class StateSyncing
    : public etl::fsm_state<BridgeFsm, StateSyncing, STATE_SYNCING,
                            EvHandshakeComplete, EvHandshakeFailed, EvReset,
                            EvCryptoFault> {
 public:
  etl::fsm_state_id_t on_enter_state() { return STATE_SYNCING; }
  etl::fsm_state_id_t on_event(const EvHandshakeComplete&) { return STATE_IDLE; }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class StateReady : public etl::fsm_state<BridgeFsm, StateReady, STATE_READY,
                                         EvReset, EvCryptoFault> {
 public:
  etl::fsm_state_id_t on_enter_state() { return STATE_READY; }
  etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class StateIdle
    : public etl::fsm_state<BridgeFsm, StateIdle, STATE_IDLE, EvSendCritical,
                            EvReset, EvCryptoFault> {
 public:
  etl::fsm_state_id_t on_enter_state() { return STATE_IDLE; }
  etl::fsm_state_id_t on_event(const EvSendCritical&) { return STATE_AWAITING_ACK; }
  etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class StateAwaitingAck
    : public etl::fsm_state<BridgeFsm, StateAwaitingAck, STATE_AWAITING_ACK,
                            EvAckReceived, EvTimeout, EvReset, EvCryptoFault> {
 public:
  etl::fsm_state_id_t on_enter_state() { return STATE_AWAITING_ACK; }
  etl::fsm_state_id_t on_event(const EvAckReceived&) { return STATE_IDLE; }
  etl::fsm_state_id_t on_event(const EvTimeout&) { return STATE_UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class StateFault : public etl::fsm_state<BridgeFsm, StateFault, STATE_FAULT,
                                         EvReset, EvCryptoFault> {
 public:
  etl::fsm_state_id_t on_enter_state() { return STATE_FAULT; }
  etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvCryptoFault&) { return No_State_Change; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class BridgeFsm : public etl::fsm {
 public:
  BridgeFsm() : etl::fsm(NUMBER_OF_STATES), state_list_{} {}

  void begin() {
    static StateStabilizing state_stabilizing;
    static StateUnsynchronized state_unsynchronized;
    static StateSyncing state_syncing;
    static StateReady state_ready;
    static StateIdle state_idle;
    static StateAwaitingAck state_awaiting_ack;
    static StateFault state_fault;

    state_list_[STATE_STABILIZING] = &state_stabilizing;
    state_list_[STATE_UNSYNCHRONIZED] = &state_unsynchronized;
    state_list_[STATE_SYNCING] = &state_syncing;
    state_list_[STATE_READY] = &state_ready;
    state_list_[STATE_IDLE] = &state_idle;
    state_list_[STATE_AWAITING_ACK] = &state_awaiting_ack;
    state_list_[STATE_FAULT] = &state_fault;

    set_states(state_list_, NUMBER_OF_STATES);
    start();
  }

  bool isStabilizing() const { return get_state_id() == STATE_STABILIZING; }
  bool isUnsynchronized() const { return get_state_id() == STATE_UNSYNCHRONIZED; }
  bool isSyncing() const { return get_state_id() == STATE_SYNCING; }
  bool isIdle() const { return get_state_id() == STATE_IDLE; }
  bool isAwaitingAck() const { return get_state_id() == STATE_AWAITING_ACK; }
  bool isFault() const { return get_state_id() == STATE_FAULT; }
  bool isSynchronized() const { return isIdle() || isAwaitingAck(); }

  void stabilized() { receive(EvStabilized()); }
  void handshakeStart() { receive(EvHandshakeStart()); }
  void handshakeComplete() { receive(EvHandshakeComplete()); }
  void handshakeFailed() { receive(EvHandshakeFailed()); }
  void sendCritical() { receive(EvSendCritical()); }
  void ackReceived() { receive(EvAckReceived()); }
  void timeout() { receive(EvTimeout()); }
  void cryptoFault() { receive(EvCryptoFault()); }
  void resetFsm() { receive(EvReset()); }

 private:
  etl::ifsm_state* state_list_[NUMBER_OF_STATES];
};

}  // namespace fsm

namespace scheduler {
enum TimerId : uint8_t {
  TIMER_ACK_TIMEOUT = 0,
  TIMER_RX_DEDUPE = 1,
  TIMER_BAUDRATE_CHANGE = 2,
  TIMER_STARTUP_STABILIZATION = 3,
  NUMBER_OF_TIMERS = 4
};

// [RAM-OPT] Lightweight timer replacing etl::callback_timer<N>.
template <size_t N>
struct SimpleTimer {
  etl::array<uint32_t, N> deadline;
  etl::array<uint32_t, N> period;
  etl::bitset<N> active;

  void clear() {
    deadline.fill(0);
    period.fill(0);
    active.reset();
  }

  void set_period(uint8_t id, uint32_t ms) {
    if (id < N) period[id] = ms;
  }

  void start(uint8_t id, uint32_t now) {
    if (id < N) {
      deadline[id] = now + period[id];
      active.set(id);
    }
  }

  void start_with_period(uint8_t id, uint32_t ms, uint32_t now) {
    if (id < N) {
      period[id] = ms;
      deadline[id] = now + ms;
      active.set(id);
    }
  }

  void stop(uint8_t id) {
    if (id < N) active.reset(id);
  }

  bool is_active(uint8_t id) const {
    return (id < N) && active.test(id);
  }

  static constexpr uint32_t TIMER_OVERFLOW_THRESHOLD = 0x80000000UL;

  etl::bitset<N> check_expired(uint32_t now) {
    etl::bitset<N> expired;
    for (uint8_t i = 0; i < N; ++i) {
      if (active.test(i) && (now - deadline[i]) < TIMER_OVERFLOW_THRESHOLD) {
        expired.set(i);
        active.reset(i);
      }
    }
    return expired;
  }
};

}  // namespace scheduler
}  // namespace bridge

#endif  // BRIDGE_FSM_H

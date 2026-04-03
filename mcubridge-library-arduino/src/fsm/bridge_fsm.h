/**
 * @file bridge_fsm.h
 * @brief ETL-based Finite State Machine for Arduino MCU Bridge v2
 *
 * [SIL-2 COMPLIANCE - IEC 61508]
 * This module implements a deterministic state machine using ETL's FSM
 * framework. All state transitions are explicit and bounded.
 *
 * Timer management:
 *   BridgeFsm wraps event dispatch with automatic exit/entry actions.
 *   Exit actions guarantee timers are stopped when leaving a state,
 *   regardless of the trigger (ack, timeout, reset, crypto fault).
 *   Entry actions kill all timers on FAULT entry as a safety net.
 *   Timer starts remain explicit in Bridge.cpp for context-dependent logic.
 *
 * States:
 *   - Stabilizing (0): Hardware startup. Draining serial lines.
 *   - Unsynchronized (1): Waiting for link synchronization.
 *   - Syncing (2): Negotiating parameters with Linux.
 *   - Ready (Parent): Super-state for operational domain.
 *     - Idle (4): Operational and ready for commands.
 *     - AwaitingAck (5): Waiting for frame acknowledgement.
 *   - Fault (6): Safety-triggered halt.
 */
#ifndef BRIDGE_FSM_H
#define BRIDGE_FSM_H

#include <etl/fsm.h>
#include <etl/message.h>
#include <etl/array.h>
#include <etl/bitset.h>

// ============================================================================
// Timer Scheduler
// [SIL-2] Defined before FSM so BridgeFsm can manage timers on transitions.
// ============================================================================
namespace bridge::scheduler {
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

  [[maybe_unused]] void start_with_period(uint8_t id, uint32_t ms, uint32_t now) {
    if (id < N) {
      period[id] = ms;
      deadline[id] = now + ms;
      active.set(id);
    }
  }

  void stop(uint8_t id) {
    if (id < N) active.reset(id);
  }

  [[maybe_unused]] bool is_active(uint8_t id) const {
    return (id < N) && active.test(id);
  }

  static constexpr uint32_t TIMER_OVERFLOW_THRESHOLD = rpc::RPC_TIMER_OVERFLOW_THRESHOLD;

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

}  // namespace bridge::scheduler

// ============================================================================
// Finite State Machine
// ============================================================================
namespace bridge::fsm {

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
  [[maybe_unused]] etl::fsm_state_id_t on_enter_state() { return STATE_STABILIZING; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvStabilized&) { return STATE_UNSYNCHRONIZED; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) { return No_State_Change; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class StateUnsynchronized
    : public etl::fsm_state<BridgeFsm, StateUnsynchronized,
                            STATE_UNSYNCHRONIZED, EvHandshakeStart,
                            EvHandshakeFailed, EvReset, EvCryptoFault> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_enter_state() { return STATE_UNSYNCHRONIZED; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeStart&) { return STATE_SYNCING; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return STATE_FAULT; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) { return No_State_Change; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class StateSyncing
    : public etl::fsm_state<BridgeFsm, StateSyncing, STATE_SYNCING,
                            EvHandshakeComplete, EvHandshakeFailed, EvReset,
                            EvCryptoFault> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_enter_state() { return STATE_SYNCING; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeComplete&) { return STATE_IDLE; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return STATE_FAULT; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class StateReady : public etl::fsm_state<BridgeFsm, StateReady, STATE_READY, // GCOVR_EXCL_START — no transitions target STATE_READY
                                         EvReset, EvCryptoFault> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_enter_state() { return STATE_READY; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
}; // GCOVR_EXCL_STOP

class StateIdle
    : public etl::fsm_state<BridgeFsm, StateIdle, STATE_IDLE, EvSendCritical,
                            EvReset, EvCryptoFault> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_enter_state() { return STATE_IDLE; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvSendCritical&) { return STATE_AWAITING_ACK; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class StateAwaitingAck
    : public etl::fsm_state<BridgeFsm, StateAwaitingAck, STATE_AWAITING_ACK,
                            EvAckReceived, EvTimeout, EvReset, EvCryptoFault> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_enter_state() { return STATE_AWAITING_ACK; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvAckReceived&) { return STATE_IDLE; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvTimeout&) { return STATE_UNSYNCHRONIZED; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class StateFault : public etl::fsm_state<BridgeFsm, StateFault, STATE_FAULT,
                                         EvReset, EvCryptoFault> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_enter_state() { return STATE_FAULT; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvCryptoFault&) { return No_State_Change; }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

// ============================================================================
// BridgeFsm — deterministic FSM with automatic timer management
//
// [SIL-2] Event dispatch is wrapped via dispatchEvent() which:
//   1. Records the pre-transition state
//   2. Delegates to etl::fsm::receive()
//   3. On state change: fires onExitState() then onEnterState()
//   4. Fires exit/entry actions for timer safety
//
// Exit actions (defense-in-depth):
//   STATE_STABILIZING  → stop TIMER_STARTUP_STABILIZATION
//   STATE_AWAITING_ACK → stop TIMER_ACK_TIMEOUT
//
// Entry actions:
//   STATE_FAULT → stop all active timers (safety net)
// ============================================================================
class BridgeFsm : public etl::fsm {
 public:
  BridgeFsm()
      : etl::fsm(NUMBER_OF_STATES),
        state_list_{},
        timers_(nullptr) {}

  void setTimers(bridge::scheduler::SimpleTimer<bridge::scheduler::NUMBER_OF_TIMERS>* timers) {
    timers_ = timers;
  }

  void begin() {
    state_list_[STATE_STABILIZING] = &state_stabilizing;
    state_list_[STATE_UNSYNCHRONIZED] = &state_unsynchronized;
    state_list_[STATE_SYNCING] = &state_syncing;
    state_list_[STATE_READY] = &state_ready;
    state_list_[STATE_IDLE] = &state_idle;
    state_list_[STATE_AWAITING_ACK] = &state_awaiting_ack;
    state_list_[STATE_FAULT] = &state_fault;

    set_states(state_list_.data(), NUMBER_OF_STATES);
    start();
  }

  bool isStabilizing() const { return get_state_id() == STATE_STABILIZING; }
  bool isUnsynchronized() const { return get_state_id() == STATE_UNSYNCHRONIZED; }
  bool isSyncing() const { return get_state_id() == STATE_SYNCING; }
  bool isIdle() const { return get_state_id() == STATE_IDLE; }
  bool isAwaitingAck() const { return get_state_id() == STATE_AWAITING_ACK; }
  bool isFault() const { return get_state_id() == STATE_FAULT; }
  bool isSynchronized() const { return isIdle() || isAwaitingAck(); }

  void stabilized() { dispatchEvent(EvStabilized()); }
  void handshakeStart() { dispatchEvent(EvHandshakeStart()); }
  void handshakeComplete() { dispatchEvent(EvHandshakeComplete()); }
  void handshakeFailed() { dispatchEvent(EvHandshakeFailed()); }
  void sendCritical() { dispatchEvent(EvSendCritical()); }
  void ackReceived() { dispatchEvent(EvAckReceived()); }
  void timeout() { dispatchEvent(EvTimeout()); }
  void cryptoFault() { dispatchEvent(EvCryptoFault()); }
  void resetFsm() { dispatchEvent(EvReset()); }

 private:
  template <typename TEvent>
  void dispatchEvent(const TEvent& evt) {
    const auto before = static_cast<StateId>(get_state_id());
    receive(evt);
    const auto after = static_cast<StateId>(get_state_id());
    if (before != after) {
      onExitState(before);
      onEnterState(after);
    }
  }

  // [SIL-2] Exit actions — guarantee timers are stopped on any exit path
  void onExitState(StateId state) {
    if (timers_ == nullptr) return;
    switch (state) {
      case STATE_AWAITING_ACK:
        timers_->stop(bridge::scheduler::TIMER_ACK_TIMEOUT);
        break;
      case STATE_STABILIZING:
        timers_->stop(bridge::scheduler::TIMER_STARTUP_STABILIZATION);
        break;
      default:
        break;
    }
  }

  // [SIL-2] Entry actions — enforce safety invariants
  void onEnterState(StateId state) {
    if (timers_ == nullptr) return;
    if (state == STATE_FAULT) {
      for (uint8_t i = 0; i < bridge::scheduler::NUMBER_OF_TIMERS; ++i) {
        timers_->stop(i);
      }
    }
  }

  etl::array<etl::ifsm_state*, NUMBER_OF_STATES> state_list_;
  bridge::scheduler::SimpleTimer<bridge::scheduler::NUMBER_OF_TIMERS>* timers_;
  StateStabilizing state_stabilizing;
  StateUnsynchronized state_unsynchronized;
  StateSyncing state_syncing;
  StateReady state_ready;
  StateIdle state_idle;
  StateAwaitingAck state_awaiting_ack;
  StateFault state_fault;
};

}  // namespace bridge::fsm

#endif  // BRIDGE_FSM_H

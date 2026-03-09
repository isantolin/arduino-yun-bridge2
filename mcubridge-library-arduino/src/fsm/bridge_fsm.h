/**
 * @file bridge_fsm.h
 * @brief ETL-based Finite State Machine for Arduino MCU Bridge v2
 *
 * [SIL-2 COMPLIANCE - IEC 61508]
 * This module implements a deterministic state machine using ETL's FSM
 * framework. All state transitions are explicit and bounded.
 *
 * States:
 *   - Unsynchronized (0): Initial state. Link reset required.
 *   - Syncing (1): Synchronizing link parameters.
 *   - Ready (Parent): Super-state for operational modes.
 *     - Idle (2): Synchronized and ready for commands.
 *     - AwaitingAck (3): Waiting for acknowledgment.
 *   - Fault (4): Safety state for unrecoverable errors.
 */
#ifndef BRIDGE_FSM_H
#define BRIDGE_FSM_H

#include "etl/fsm.h"
#include "etl/message.h"

namespace bridge {
namespace fsm {

class BridgeFsm;

enum StateId : etl::fsm_state_id_t {
  STATE_UNSYNCHRONIZED = 0,
  STATE_SYNCING = 1,
  STATE_READY = 2,  // Parent state
  STATE_IDLE = 3,
  STATE_AWAITING_ACK = 4,
  STATE_FAULT = 5,
  NUMBER_OF_STATES = 6
};

enum EventId : etl::message_id_t {
  EVENT_HANDSHAKE_START = 0,
  EVENT_HANDSHAKE_COMPLETE = 1,
  EVENT_HANDSHAKE_FAILED = 2,
  EVENT_SEND_CRITICAL = 3,
  EVENT_ACK_RECEIVED = 4,
  EVENT_TIMEOUT = 5,
  EVENT_RESET = 6,
  EVENT_CRYPTO_FAULT = 7
};

struct EvHandshakeStart : public etl::message<EVENT_HANDSHAKE_START> {};
struct EvHandshakeComplete : public etl::message<EVENT_HANDSHAKE_COMPLETE> {};
struct EvHandshakeFailed : public etl::message<EVENT_HANDSHAKE_FAILED> {};
struct EvSendCritical : public etl::message<EVENT_SEND_CRITICAL> {};
struct EvAckReceived : public etl::message<EVENT_ACK_RECEIVED> {};
struct EvTimeout : public etl::message<EVENT_TIMEOUT> {};
struct EvReset : public etl::message<EVENT_RESET> {};
struct EvCryptoFault : public etl::message<EVENT_CRYPTO_FAULT> {};

class StateUnsynchronized
    : public etl::fsm_state<BridgeFsm, StateUnsynchronized,
                            STATE_UNSYNCHRONIZED, EvHandshakeStart,
                            EvHandshakeFailed, EvReset, EvCryptoFault> {
 public:
  etl::fsm_state_id_t on_enter_state() { return STATE_UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvHandshakeStart&) {
    return STATE_SYNCING;
  }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event(const EvReset&) { return No_State_Change; }
  etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return No_State_Change;
  }
};

class StateSyncing
    : public etl::fsm_state<BridgeFsm, StateSyncing, STATE_SYNCING,
                            EvHandshakeComplete, EvHandshakeFailed, EvReset,
                            EvCryptoFault> {
 public:
  etl::fsm_state_id_t on_enter_state() { return STATE_SYNCING; }
  etl::fsm_state_id_t on_event(const EvHandshakeComplete&) {
    return STATE_IDLE;
  }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return No_State_Change;
  }
};

class StateReady : public etl::fsm_state<BridgeFsm, StateReady, STATE_READY,
                                         EvReset, EvCryptoFault> {
 public:
  etl::fsm_state_id_t on_enter_state() { return STATE_READY; }
  etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return No_State_Change;
  }
};

class StateIdle
    : public etl::fsm_state<BridgeFsm, StateIdle, STATE_IDLE, EvSendCritical,
                            EvReset, EvCryptoFault> {
 public:
  etl::fsm_state_id_t on_enter_state() { return STATE_IDLE; }
  etl::fsm_state_id_t on_event(const EvSendCritical&) {
    return STATE_AWAITING_ACK;
  }
  etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return No_State_Change;
  }
};

class StateAwaitingAck
    : public etl::fsm_state<BridgeFsm, StateAwaitingAck, STATE_AWAITING_ACK,
                            EvAckReceived, EvTimeout, EvReset, EvCryptoFault> {
 public:
  etl::fsm_state_id_t on_enter_state() { return STATE_AWAITING_ACK; }
  etl::fsm_state_id_t on_event(const EvAckReceived&) { return STATE_IDLE; }
  etl::fsm_state_id_t on_event(const EvTimeout&) {
    return STATE_UNSYNCHRONIZED;
  }
  etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvCryptoFault&) { return STATE_FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return No_State_Change;
  }
};

class StateFault : public etl::fsm_state<BridgeFsm, StateFault, STATE_FAULT,
                                         EvReset, EvCryptoFault> {
 public:
  etl::fsm_state_id_t on_enter_state() { return STATE_FAULT; }
  etl::fsm_state_id_t on_event(const EvReset&) { return STATE_UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvCryptoFault&) { return No_State_Change; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return No_State_Change;
  }
};

class BridgeFsm : public etl::fsm {
 public:
  BridgeFsm() : etl::fsm(NUMBER_OF_STATES), state_list_{} {}

  void begin() {
    static StateUnsynchronized state_unsynchronized;
    static StateSyncing state_syncing;
    static StateReady state_ready;
    static StateIdle state_idle;
    static StateAwaitingAck state_awaiting_ack;
    static StateFault state_fault;

    state_list_[STATE_UNSYNCHRONIZED] = &state_unsynchronized;
    state_list_[STATE_SYNCING] = &state_syncing;
    state_list_[STATE_READY] = &state_ready;
    state_list_[STATE_IDLE] = &state_idle;
    state_list_[STATE_AWAITING_ACK] = &state_awaiting_ack;
    state_list_[STATE_FAULT] = &state_fault;

    set_states(state_list_, NUMBER_OF_STATES);
    start();
  }

  bool isUnsynchronized() const {
    return get_state_id() == STATE_UNSYNCHRONIZED;
  }
  bool isSyncing() const { return get_state_id() == STATE_SYNCING; }
  bool isIdle() const { return get_state_id() == STATE_IDLE; }
  bool isAwaitingAck() const { return get_state_id() == STATE_AWAITING_ACK; }
  bool isFault() const { return get_state_id() == STATE_FAULT; }
  bool isSynchronized() const { return isIdle() || isAwaitingAck(); }

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

// [RAM-OPT] Lightweight timer replacing etl::callback_timer<4>.
// etl::callback_timer + 4 etl::delegate<void()> cost ~136-152 bytes RAM.
// SimpleTimer: 4 deadlines + 1 period + 1 bitmask = ~21 bytes.
struct SimpleTimer {
  uint32_t deadline[NUMBER_OF_TIMERS];
  uint32_t period[NUMBER_OF_TIMERS];
  uint8_t active;  // bitmask: bit i = timer i is running

  void clear() {
    etl::fill(etl::begin(deadline), etl::end(deadline), 0);
    etl::fill(etl::begin(period), etl::end(period), 0);
    active = 0;
  }

  void set_period(uint8_t id, uint32_t ms) {
    period[id] = ms;
  }

  void start(uint8_t id, uint32_t now) {
    deadline[id] = now + period[id];
    active |= (1U << id);
  }

  void start_with_period(uint8_t id, uint32_t ms, uint32_t now) {
    period[id] = ms;
    deadline[id] = now + ms;
    active |= (1U << id);
  }

  void stop(uint8_t id) {
    active &= ~(1U << id);
  }

  bool is_active(uint8_t id) const {
    return (active & (1U << id)) != 0;
  }

  // Returns bitmask of expired timers and clears them
  uint8_t check_expired(uint32_t now) {
    uint8_t expired = 0;
    for (uint8_t i = 0; i < NUMBER_OF_TIMERS; ++i) {
      if ((active & (1U << i)) && (now - deadline[i]) < 0x80000000UL) {
        expired |= (1U << i);
        active &= ~(1U << i);
      }
    }
    return expired;
  }
};

}  // namespace scheduler
}  // namespace bridge

#endif  // BRIDGE_FSM_H
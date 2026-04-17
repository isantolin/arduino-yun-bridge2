#include "bridge_fsm.h"
#include <etl/fsm.h>

namespace bridge::fsm {

// --- State Definitions ---

class StartupState : public etl::fsm_state<BridgeFsm, StartupState, State::STARTUP, EvStabilized, EvReset, EvHandshakeFailed, EvTimeout> {
 public:
  etl::fsm_state_id_t on_event(const EvStabilized&) { return State::UNSYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvReset&) { return State::STARTUP; }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return State::STARTUP; }
  etl::fsm_state_id_t on_event(const EvTimeout&) { return State::FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class UnsynchronizedState : public etl::fsm_state<BridgeFsm, UnsynchronizedState, State::UNSYNCHRONIZED, EvHandshakeStart, EvReset, EvHandshakeFailed, EvTimeout> {
 public:
  etl::fsm_state_id_t on_event(const EvHandshakeStart&) { return State::HANDSHAKE; }
  etl::fsm_state_id_t on_event(const EvReset&) { return State::STARTUP; }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return State::STARTUP; }
  etl::fsm_state_id_t on_event(const EvTimeout&) { return State::FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class HandshakeState : public etl::fsm_state<BridgeFsm, HandshakeState, State::HANDSHAKE, EvHandshakeComplete, EvHandshakeFailed, EvReset, EvTimeout> {
 public:
  etl::fsm_state_id_t on_event(const EvHandshakeComplete&) { return State::SYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return State::STARTUP; }
  etl::fsm_state_id_t on_event(const EvReset&) { return State::STARTUP; }
  etl::fsm_state_id_t on_event(const EvTimeout&) { return State::FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class SynchronizedState : public etl::fsm_state<BridgeFsm, SynchronizedState, State::SYNCHRONIZED, EvSendCritical, EvReset, EvHandshakeFailed, EvTimeout> {
 public:
  etl::fsm_state_id_t on_event(const EvSendCritical&) { return State::AWAITING_ACK; }
  etl::fsm_state_id_t on_event(const EvReset&) { return State::STARTUP; }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return State::STARTUP; }
  etl::fsm_state_id_t on_event(const EvTimeout&) { return State::FAULT; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class AwaitingAckState : public etl::fsm_state<BridgeFsm, AwaitingAckState, State::AWAITING_ACK, EvAckReceived, EvTimeout, EvReset, EvHandshakeFailed> {
 public:
  etl::fsm_state_id_t on_event(const EvAckReceived&) { return State::SYNCHRONIZED; }
  etl::fsm_state_id_t on_event(const EvTimeout&) { return State::FAULT; }
  etl::fsm_state_id_t on_event(const EvReset&) { return State::STARTUP; }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return State::STARTUP; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

class FaultState : public etl::fsm_state<BridgeFsm, FaultState, State::FAULT, EvReset> {
 public:
  etl::fsm_state_id_t on_event(const EvReset&) { return State::STARTUP; }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return No_State_Change; }
};

// --- FSM Implementation ---

static StartupState startup_state;
static UnsynchronizedState unsynchronized_state;
static HandshakeState handshake_state;
static SynchronizedState synchronized_state;
static AwaitingAckState awaiting_ack_state;
static FaultState fault_state;

static etl::ifsm_state* state_table[] = {
    &startup_state,
    &unsynchronized_state,
    &handshake_state,
    &synchronized_state,
    &awaiting_ack_state,
    &fault_state
};

BridgeFsm::BridgeFsm() : etl::fsm(0) {
  set_states(state_table, etl::size(state_table));
}

bool BridgeFsm::isSynchronized() const {
  const auto sid = get_state_id();
  return sid == State::SYNCHRONIZED || sid == State::AWAITING_ACK;
}

bool BridgeFsm::isAwaitingAck() const {
  return get_state_id() == State::AWAITING_ACK;
}

bool BridgeFsm::isFault() const {
  return get_state_id() == State::FAULT;
}

} // namespace bridge::fsm

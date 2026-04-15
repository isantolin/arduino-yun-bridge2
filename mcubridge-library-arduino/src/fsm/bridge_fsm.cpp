#include "fsm/bridge_fsm.h"

namespace bridge::fsm {

struct StateStartup : public etl::fsm_state<BridgeFsm, StateStartup, static_cast<etl::fsm_state_id_t>(StateId::STARTUP)> {
  etl::fsm_state_id_t on_event(etl::imessage& msg) { (void)msg; return get_state_id(); }
};

struct StateUnsynchronized : public etl::fsm_state<BridgeFsm, StateUnsynchronized, static_cast<etl::fsm_state_id_t>(StateId::UNSYNCHRONIZED)> {
  etl::fsm_state_id_t on_event(etl::imessage& msg) { (void)msg; return get_state_id(); }
};

struct StateHandshake : public etl::fsm_state<BridgeFsm, StateHandshake, static_cast<etl::fsm_state_id_t>(StateId::HANDSHAKE)> {
  etl::fsm_state_id_t on_event(etl::imessage& msg) { (void)msg; return get_state_id(); }
};

struct StateSynchronized : public etl::fsm_state<BridgeFsm, StateSynchronized, static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED)> {
  etl::fsm_state_id_t on_event(etl::imessage& msg) { (void)msg; return get_state_id(); }
};

struct StateAwaitingAck : public etl::fsm_state<BridgeFsm, StateAwaitingAck, static_cast<etl::fsm_state_id_t>(StateId::AWAITING_ACK)> {
  etl::fsm_state_id_t on_event(etl::imessage& msg) { (void)msg; return get_state_id(); }
};

struct StateFault : public etl::fsm_state<BridgeFsm, StateFault, static_cast<etl::fsm_state_id_t>(StateId::FAULT)> {
  etl::fsm_state_id_t on_event(etl::imessage& msg) { (void)msg; return get_state_id(); }
};

static StateStartup s_startup;
static StateUnsynchronized s_unsynchronized;
static StateHandshake s_handshake;
static StateSynchronized s_synchronized;
static StateAwaitingAck s_awaiting_ack;
static StateFault s_fault;

BridgeFsm::BridgeFsm() : etl::fsm(6) {
  _states[0] = &s_startup;
  _states[1] = &s_unsynchronized;
  _states[2] = &s_handshake;
  _states[3] = &s_synchronized;
  _states[4] = &s_awaiting_ack;
  _states[5] = &s_fault;
  set_states(_states.data(), 6);
}

void BridgeFsm::stabilized() {
  if (get_state_id() == static_cast<etl::fsm_state_id_t>(StateId::STARTUP)) {
    start(false);
    replace_state(s_unsynchronized);
  }
}

void BridgeFsm::handshakeStart() {
  if (get_state_id() == static_cast<etl::fsm_state_id_t>(StateId::UNSYNCHRONIZED)) {
    replace_state(s_handshake);
  }
}

void BridgeFsm::handshakeComplete() {
  if (get_state_id() == static_cast<etl::fsm_state_id_t>(StateId::HANDSHAKE)) {
    replace_state(s_synchronized);
  }
}

void BridgeFsm::handshakeFailed() {
  resetFsm();
}

void BridgeFsm::sendCritical() {
  if (get_state_id() == static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED)) {
    replace_state(s_awaiting_ack);
  }
}

void BridgeFsm::ackReceived() {
  if (get_state_id() == static_cast<etl::fsm_state_id_t>(StateId::AWAITING_ACK)) {
    replace_state(s_synchronized);
  }
}

void BridgeFsm::timeout() {
  replace_state(s_fault);
}

} // namespace bridge::fsm

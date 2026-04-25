#include "bridge_fsm.h"

#include <etl/array.h>
#include <etl/fsm.h>

namespace bridge::fsm {

// --- FSM Implementation ---

static StartupState startup_state;
static UnsynchronizedState unsynchronized_state;
static HandshakeState handshake_state;
static SynchronizedState synchronized_state;
static AwaitingAckState awaiting_ack_state;
static FaultState fault_state;

static etl::array<etl::ifsm_state*, 6> state_table = {
    &startup_state,      &unsynchronized_state, &handshake_state,
    &synchronized_state, &awaiting_ack_state,   &fault_state};

BridgeFsm::BridgeFsm() : etl::fsm(0) {
  set_states(state_table.data(), state_table.size());
}

bool BridgeFsm::isSynchronized() const {
  const auto sid = get_state_id();
  return sid == static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED) ||
         sid == static_cast<etl::fsm_state_id_t>(StateId::AWAITING_ACK);
}

bool BridgeFsm::isAwaitingAck() const {
  return get_state_id() ==
         static_cast<etl::fsm_state_id_t>(StateId::AWAITING_ACK);
}

}  // namespace bridge::fsm

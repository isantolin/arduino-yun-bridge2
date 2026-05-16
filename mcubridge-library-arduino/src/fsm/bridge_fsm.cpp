#include "bridge_fsm.h"

#include <etl/array.h>
#include <etl/fsm.h>

namespace bridge::fsm {

// --- FSM Implementation ---

BridgeFsm::BridgeFsm()
    : etl::fsm(static_cast<etl::fsm_state_id_t>(StateId::STARTUP)),
      _state_table({&_startup_state, &_unsynchronized_state, &_handshake_state,
                    &_synchronized_state, &_awaiting_ack_state, &_fault_state}) {
  set_states(_state_table.data(), _state_table.size());
}

bool BridgeFsm::isSynchronized() const {
  const auto sid = get_state_id();
  return sid == static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED) ||
         sid == static_cast<etl::fsm_state_id_t>(StateId::AWAITING_ACK);
}

bool BridgeFsm::isUnsynchronized() const {
  return get_state_id() == static_cast<etl::fsm_state_id_t>(StateId::UNSYNCHRONIZED);
}

bool BridgeFsm::isAwaitingAck() const {
  return get_state_id() ==
         static_cast<etl::fsm_state_id_t>(StateId::AWAITING_ACK);
}

}  // namespace bridge::fsm

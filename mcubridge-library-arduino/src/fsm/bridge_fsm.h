#ifndef BRIDGE_FSM_H
#define BRIDGE_FSM_H

#include <etl/fsm.h>
#include <etl/message.h>
#include "protocol/BridgeEvents.h"

namespace bridge::fsm {

enum class StateId {
  STARTUP,
  UNSYNCHRONIZED,
  HANDSHAKE,
  SYNCHRONIZED,
  AWAITING_ACK,
  FAULT
};

// [SIL-2] Forward declarations for States
struct StateStartup;
struct StateUnsynchronized;
struct StateHandshake;
struct StateSynchronized;
struct StateAwaitingAck;
struct StateFault;

struct BridgeFsm : public etl::fsm {
  BridgeFsm();

  void begin() { etl::fsm::reset(); }
  void resetFsm() { etl::fsm::reset(); }
  void stabilized();
  void handshakeStart();
  void handshakeComplete();
  void handshakeFailed();
  void sendCritical();
  void ackReceived();
  void timeout();

  bool isSynchronized() const { 
      return get_state_id() == static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED) || 
             get_state_id() == static_cast<etl::fsm_state_id_t>(StateId::AWAITING_ACK); 
  }
  bool isAwaitingAck() const { 
      return get_state_id() == static_cast<etl::fsm_state_id_t>(StateId::AWAITING_ACK); 
  }
  [[maybe_unused]] bool isFault() const { 
      return get_state_id() == static_cast<etl::fsm_state_id_t>(StateId::FAULT); 
  }
  [[maybe_unused]] StateId get_bridge_state() const { return static_cast<StateId>(get_state_id()); }

 private:
  // Persistent state instances
  etl::array<etl::ifsm_state*, 6> _states;
};

} // namespace bridge::fsm

#endif

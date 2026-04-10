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

struct BridgeFsm : public etl::fsm {
  BridgeFsm() : etl::fsm(0), _state(StateId::STARTUP) {}

  void begin() { _state = StateId::STARTUP; }
  void resetFsm() { _state = StateId::STARTUP; }
  void stabilized() { if (_state == StateId::STARTUP) _state = StateId::UNSYNCHRONIZED; }
  void handshakeStart() { if (_state == StateId::UNSYNCHRONIZED) _state = StateId::HANDSHAKE; }
  void handshakeComplete() { if (_state == StateId::HANDSHAKE) _state = StateId::SYNCHRONIZED; }
  void handshakeFailed() { _state = StateId::STARTUP; }
  void sendCritical() { if (_state == StateId::SYNCHRONIZED) _state = StateId::AWAITING_ACK; }
  void ackReceived() { if (_state == StateId::AWAITING_ACK) _state = StateId::SYNCHRONIZED; }
  void timeout() { _state = StateId::FAULT; }

  bool isSynchronized() const { return _state == StateId::SYNCHRONIZED || _state == StateId::AWAITING_ACK; }
  bool isAwaitingAck() const { return _state == StateId::AWAITING_ACK; }
  bool isFault() const { return _state == StateId::FAULT; }
  StateId get_bridge_state() const { return _state; }

 private:
  StateId _state;
};

} // namespace bridge::fsm

#endif

#ifndef BRIDGE_FSM_H
#define BRIDGE_FSM_H

#include <etl/fsm.h>
#include <etl/message.h>
#include "protocol/BridgeEvents.h"

namespace bridge::fsm {

enum class StateId {
  STARTUP = 0,
  UNSYNCHRONIZED = 1,
  HANDSHAKE = 2,
  SYNCHRONIZED = 3,
  AWAITING_ACK = 4,
  FAULT = 5
};

// --- State IDs ---
struct State {
  enum Id {
    STARTUP = 0,
    UNSYNCHRONIZED = 1,
    HANDSHAKE = 2,
    SYNCHRONIZED = 3,
    AWAITING_ACK = 4,
    FAULT = 5
  };
};

// --- Events ---
struct EvStabilized : public etl::message<0> {};
struct EvHandshakeStart : public etl::message<1> {};
struct EvHandshakeComplete : public etl::message<2> {};
struct EvHandshakeFailed : public etl::message<3> {};
struct EvSendCritical : public etl::message<4> {};
struct EvAckReceived : public etl::message<5> {};
struct EvTimeout : public etl::message<6> {};
struct EvReset : public etl::message<7> {};

// --- Forward Declarations ---
class StartupState;
class UnsynchronizedState;
class HandshakeState;
class SynchronizedState;
class AwaitingAckState;
class FaultState;

class BridgeFsm : public etl::fsm {
 public:
  BridgeFsm();

  bool isSynchronized() const;
  bool isAwaitingAck() const;
  bool isFault() const;
  StateId get_bridge_state() const { return static_cast<StateId>(get_state_id()); }

  // [SIL-2] Wrapper methods for backward compatibility with BridgeClass calls,
  // but internally dispatching ETL events.
  void begin() { 
    if (!is_started()) start();
    receive(EvReset()); 
  }
  void resetFsm() { 
    if (!is_started()) start();
    receive(EvReset()); 
  }
  void stabilized() { receive(EvStabilized()); }
  void handshakeStart() { receive(EvHandshakeStart()); }
  void handshakeComplete() { receive(EvHandshakeComplete()); }
  void handshakeFailed() { receive(EvHandshakeFailed()); }
  void sendCritical() { receive(EvSendCritical()); }
  void ackReceived() { receive(EvAckReceived()); }
  void timeout() { receive(EvTimeout()); }
};

} // namespace bridge::fsm

#endif

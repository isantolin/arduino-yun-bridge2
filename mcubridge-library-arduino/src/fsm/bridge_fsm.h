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

// --- State Classes ---

class StartupState : public etl::fsm_state<BridgeFsm, StartupState, State::STARTUP, EvStabilized, EvReset, EvHandshakeFailed, EvTimeout> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvStabilized&) { return State::UNSYNCHRONIZED; }
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

class BridgeFsm : public etl::fsm {
 public:
  BridgeFsm();

  bool isSynchronized() const;
  bool isAwaitingAck() const;

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

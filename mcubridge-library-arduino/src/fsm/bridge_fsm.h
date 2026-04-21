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
class BridgeFsm;
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
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) { return State::STARTUP; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return State::STARTUP; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvTimeout&) { return State::FAULT; }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return get_state_id(); }
};

class UnsynchronizedState : public etl::fsm_state<BridgeFsm, UnsynchronizedState, State::UNSYNCHRONIZED, EvHandshakeStart, EvReset, EvHandshakeFailed, EvTimeout> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeStart&) { return State::HANDSHAKE; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) { return State::STARTUP; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return State::STARTUP; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvTimeout&) { return State::FAULT; }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return get_state_id(); }
};

class HandshakeState : public etl::fsm_state<BridgeFsm, HandshakeState, State::HANDSHAKE, EvHandshakeComplete, EvHandshakeFailed, EvReset, EvTimeout> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeComplete&) { return State::SYNCHRONIZED; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return State::STARTUP; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) { return State::STARTUP; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvTimeout&) { return State::FAULT; }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return get_state_id(); }
};

class SynchronizedState : public etl::fsm_state<BridgeFsm, SynchronizedState, State::SYNCHRONIZED, EvSendCritical, EvReset, EvHandshakeFailed, EvTimeout> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvSendCritical&) { return State::AWAITING_ACK; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) { return State::STARTUP; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return State::STARTUP; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvTimeout&) { return State::FAULT; }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return get_state_id(); }
};

class AwaitingAckState : public etl::fsm_state<BridgeFsm, AwaitingAckState, State::AWAITING_ACK, EvAckReceived, EvTimeout, EvReset, EvHandshakeFailed> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvAckReceived&) { return State::SYNCHRONIZED; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvTimeout&) { return State::FAULT; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) { return State::STARTUP; }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeFailed&) { return State::STARTUP; }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return get_state_id(); }
};

class FaultState : public etl::fsm_state<BridgeFsm, FaultState, State::FAULT, EvReset> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) { return State::STARTUP; }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) { return get_state_id(); }
};

class BridgeFsm : public etl::fsm {
 public:
  BridgeFsm();

  bool isSynchronized() const;
  bool isAwaitingAck() const;
};

} // namespace bridge::fsm

#endif

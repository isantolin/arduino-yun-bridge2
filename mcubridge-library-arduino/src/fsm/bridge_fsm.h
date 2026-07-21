#ifndef BRIDGE_FSM_H
#define BRIDGE_FSM_H

#include <etl/fsm.h>
#include <etl/message.h>

#include "hal/hal.h"

namespace bridge::fsm {

enum class StateId : uint8_t {
  STARTUP = 0,
  UNSYNCHRONIZED = 1,
  HANDSHAKE = 2,
  SYNCHRONIZED = 3,
  AWAITING_ACK = 4,
  FAULT = 5
};

// --- Events ---
struct EvHandshakeStart : public etl::message<0> {};
struct EvHandshakeComplete : public etl::message<1> {};
struct EvHandshakeFailed : public etl::message<2> {};
struct EvSendCritical : public etl::message<3> {};
struct EvAckReceived : public etl::message<4> {};
struct EvTimeout : public etl::message<5> {};
struct EvReset : public etl::message<6> {};

// --- Forward Declarations ---
class BridgeFsm;
class StartupState;
class UnsynchronizedState;
class HandshakeState;
class SynchronizedState;
class AwaitingAckState;
class FaultState;

// --- State Classes ---

class StartupState
    : public etl::fsm_state<BridgeFsm, StartupState,
                            static_cast<etl::fsm_state_id_t>(StateId::STARTUP),
                            EvReset, EvHandshakeFailed, EvTimeout> {
 public:
  StartupState() = default;
  etl::fsm_state_id_t on_event(const EvReset&) {
    return static_cast<etl::fsm_state_id_t>(StateId::UNSYNCHRONIZED);
  }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  etl::fsm_state_id_t on_event(const EvTimeout&) {
    return static_cast<etl::fsm_state_id_t>(StateId::FAULT);
  }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

class UnsynchronizedState
    : public etl::fsm_state<
          BridgeFsm, UnsynchronizedState,
          static_cast<etl::fsm_state_id_t>(StateId::UNSYNCHRONIZED),
          EvHandshakeStart, EvReset, EvHandshakeFailed, EvTimeout> {
 public:
  UnsynchronizedState() = default;
  etl::fsm_state_id_t on_event(const EvHandshakeStart&) {
    return static_cast<etl::fsm_state_id_t>(StateId::HANDSHAKE);
  }
  etl::fsm_state_id_t on_event(const EvReset&) {
    return static_cast<etl::fsm_state_id_t>(StateId::UNSYNCHRONIZED);
  }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  etl::fsm_state_id_t on_event(const EvTimeout&) {
    return static_cast<etl::fsm_state_id_t>(StateId::FAULT);
  }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

class HandshakeState
    : public etl::fsm_state<
          BridgeFsm, HandshakeState,
          static_cast<etl::fsm_state_id_t>(StateId::HANDSHAKE),
          EvHandshakeComplete, EvHandshakeFailed, EvReset, EvTimeout> {
 public:
  HandshakeState() = default;
  etl::fsm_state_id_t on_event(const EvHandshakeComplete&) {
    return static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED);
  }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  etl::fsm_state_id_t on_event(const EvReset&) {
    return static_cast<etl::fsm_state_id_t>(StateId::UNSYNCHRONIZED);
  }
  etl::fsm_state_id_t on_event(const EvTimeout&) {
    return static_cast<etl::fsm_state_id_t>(StateId::FAULT);
  }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

class SynchronizedState
    : public etl::fsm_state<
          BridgeFsm, SynchronizedState,
          static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED),
          EvSendCritical, EvReset, EvHandshakeFailed, EvTimeout> {
 public:
  SynchronizedState() = default;
  etl::fsm_state_id_t on_event(const EvSendCritical&) {
    return static_cast<etl::fsm_state_id_t>(StateId::AWAITING_ACK);
  }
  etl::fsm_state_id_t on_event(const EvReset&) {
    return static_cast<etl::fsm_state_id_t>(StateId::UNSYNCHRONIZED);
  }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  etl::fsm_state_id_t on_event(const EvTimeout&) {
    return static_cast<etl::fsm_state_id_t>(StateId::FAULT);
  }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

class AwaitingAckState
    : public etl::fsm_state<
          BridgeFsm, AwaitingAckState,
          static_cast<etl::fsm_state_id_t>(StateId::AWAITING_ACK),
          EvAckReceived, EvTimeout, EvReset, EvHandshakeFailed> {
 public:
  AwaitingAckState() = default;
  etl::fsm_state_id_t on_event(const EvAckReceived&) {
    return static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED);
  }
  etl::fsm_state_id_t on_event(const EvTimeout&) {
    return static_cast<etl::fsm_state_id_t>(StateId::FAULT);
  }
  etl::fsm_state_id_t on_event(const EvReset&) {
    return static_cast<etl::fsm_state_id_t>(StateId::UNSYNCHRONIZED);
  }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

class FaultState
    : public etl::fsm_state<BridgeFsm, FaultState,
                            static_cast<etl::fsm_state_id_t>(StateId::FAULT),
                            EvReset> {
 public:
  FaultState() = default;
  // [SIL-2] Force hardware safe state on every entry to FAULT — regardless of
  // which event caused the transition (ACK timeout, handshake failure, etc.).
  etl::fsm_state_id_t on_enter_state() override {
    bridge::hal::forceSafeState();
    return No_State_Change;
  }

  etl::fsm_state_id_t on_event(const EvReset&) {
    return static_cast<etl::fsm_state_id_t>(StateId::UNSYNCHRONIZED);
  }
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

class BridgeFsm : public etl::fsm {
 public:
  BridgeFsm();

  bool isSynchronized() const;
  bool isAwaitingAck() const;

 private:
  StartupState _startup_state;
  UnsynchronizedState _unsynchronized_state;
  HandshakeState _handshake_state;
  SynchronizedState _synchronized_state;
  AwaitingAckState _awaiting_ack_state;
  FaultState _fault_state;

  etl::array<etl::ifsm_state*, 6> _state_table;
};

}  // namespace bridge::fsm

#endif

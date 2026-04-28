#ifndef BRIDGE_FSM_H
#define BRIDGE_FSM_H

#include <etl/fsm.h>
#include <etl/message.h>

#include "protocol/BridgeEvents.h"

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

class StartupState
    : public etl::fsm_state<BridgeFsm, StartupState,
                            static_cast<etl::fsm_state_id_t>(StateId::STARTUP),
                            EvStabilized, EvReset, EvHandshakeFailed,
                            EvTimeout> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvStabilized&) {
    return static_cast<etl::fsm_state_id_t>(StateId::UNSYNCHRONIZED);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeFailed&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvTimeout&) {
    return static_cast<etl::fsm_state_id_t>(StateId::FAULT);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

class UnsynchronizedState
    : public etl::fsm_state<
          BridgeFsm, UnsynchronizedState,
          static_cast<etl::fsm_state_id_t>(StateId::UNSYNCHRONIZED),
          EvHandshakeStart, EvReset, EvHandshakeFailed, EvTimeout> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeStart&) {
    return static_cast<etl::fsm_state_id_t>(StateId::HANDSHAKE);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeFailed&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvTimeout&) {
    return static_cast<etl::fsm_state_id_t>(StateId::FAULT);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

class HandshakeState
    : public etl::fsm_state<
          BridgeFsm, HandshakeState,
          static_cast<etl::fsm_state_id_t>(StateId::HANDSHAKE),
          EvHandshakeComplete, EvHandshakeFailed, EvReset, EvTimeout> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeComplete&) {
    return static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeFailed&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvTimeout&) {
    return static_cast<etl::fsm_state_id_t>(StateId::FAULT);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

class SynchronizedState
    : public etl::fsm_state<
          BridgeFsm, SynchronizedState,
          static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED),
          EvSendCritical, EvReset, EvHandshakeFailed, EvTimeout> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvSendCritical&) {
    return static_cast<etl::fsm_state_id_t>(StateId::AWAITING_ACK);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeFailed&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvTimeout&) {
    return static_cast<etl::fsm_state_id_t>(StateId::FAULT);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

class AwaitingAckState
    : public etl::fsm_state<
          BridgeFsm, AwaitingAckState,
          static_cast<etl::fsm_state_id_t>(StateId::AWAITING_ACK),
          EvAckReceived, EvTimeout, EvReset, EvHandshakeFailed> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvAckReceived&) {
    return static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvTimeout&) {
    return static_cast<etl::fsm_state_id_t>(StateId::FAULT);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvHandshakeFailed&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

class FaultState
    : public etl::fsm_state<BridgeFsm, FaultState,
                            static_cast<etl::fsm_state_id_t>(StateId::FAULT),
                            EvReset> {
 public:
  [[maybe_unused]] etl::fsm_state_id_t on_event(const EvReset&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  [[maybe_unused]] etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

class BridgeFsm : public etl::fsm {
 public:
  BridgeFsm();

  bool isSynchronized() const;
  bool isAwaitingAck() const;
};

}  // namespace bridge::fsm

#endif

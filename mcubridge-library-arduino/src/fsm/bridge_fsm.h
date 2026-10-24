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
  FAULT = 5,
  ENTERING_BOOTLOADER = 6,
  CHANGING_BAUDRATE = 7
};

// --- Events ---
struct EvHandshakeStart : public etl::message<0> {};
struct EvHandshakeComplete : public etl::message<1> {};
struct EvHandshakeFailed : public etl::message<2> {};
struct EvSendCritical : public etl::message<3> {};
struct EvAckReceived : public etl::message<4> {};
struct EvTimeout : public etl::message<5> {};
struct EvReset : public etl::message<6> {};
struct EvEnterBootloader : public etl::message<7> {};
struct EvBaudrateChange : public etl::message<8> {};
struct EvBaudrateApplied : public etl::message<9> {};

// --- Forward Declarations ---
class BridgeFsm;
class StartupState;
class UnsynchronizedState;
class HandshakeState;
class SynchronizedState;
class AwaitingAckState;
class FaultState;
class EnteringBootloaderState;
class ChangingBaudrateState;

// --- Base CRTP State Template ---

template <typename Derived, StateId SId, typename... Events>
class BaseBridgeState
    : public etl::fsm_state<BridgeFsm, Derived,
                            static_cast<etl::fsm_state_id_t>(SId), Events...> {
 public:
  etl::fsm_state_id_t on_event(const EvReset&) {
    return static_cast<etl::fsm_state_id_t>(StateId::UNSYNCHRONIZED);
  }
  etl::fsm_state_id_t on_event(const EvHandshakeFailed&) {
    return static_cast<etl::fsm_state_id_t>(StateId::STARTUP);
  }
  etl::fsm_state_id_t on_event(const EvTimeout&) {
    return static_cast<etl::fsm_state_id_t>(StateId::FAULT);
  }
  static etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return static_cast<etl::fsm_state_id_t>(SId);
  }
};

// --- State Classes ---

class StartupState
    : public BaseBridgeState<StartupState, StateId::STARTUP, EvReset,
                             EvHandshakeFailed, EvTimeout> {};

class UnsynchronizedState
    : public BaseBridgeState<UnsynchronizedState, StateId::UNSYNCHRONIZED,
                             EvHandshakeStart, EvReset, EvHandshakeFailed,
                             EvTimeout> {
 public:
  using BaseBridgeState::on_event;
  etl::fsm_state_id_t on_event(const EvHandshakeStart&) {
    return static_cast<etl::fsm_state_id_t>(StateId::HANDSHAKE);
  }
};

class HandshakeState
    : public BaseBridgeState<HandshakeState, StateId::HANDSHAKE,
                             EvHandshakeComplete, EvSendCritical,
                             EvHandshakeFailed, EvReset, EvTimeout> {
 public:
  using BaseBridgeState::on_event;
  etl::fsm_state_id_t on_event(const EvHandshakeComplete&) {
    return static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED);
  }
  etl::fsm_state_id_t on_event(const EvSendCritical&) {
    return static_cast<etl::fsm_state_id_t>(StateId::FAULT);
  }
};

class SynchronizedState
    : public BaseBridgeState<SynchronizedState, StateId::SYNCHRONIZED,
                             EvSendCritical, EvReset, EvHandshakeFailed,
                             EvTimeout, EvEnterBootloader, EvBaudrateChange> {
 public:
  using BaseBridgeState::on_event;
  etl::fsm_state_id_t on_event(const EvSendCritical&) {
    return static_cast<etl::fsm_state_id_t>(StateId::AWAITING_ACK);
  }
  etl::fsm_state_id_t on_event(const EvEnterBootloader&) {
    return static_cast<etl::fsm_state_id_t>(StateId::ENTERING_BOOTLOADER);
  }
  etl::fsm_state_id_t on_event(const EvBaudrateChange&) {
    return static_cast<etl::fsm_state_id_t>(StateId::CHANGING_BAUDRATE);
  }
};

class AwaitingAckState
    : public BaseBridgeState<AwaitingAckState, StateId::AWAITING_ACK,
                             EvAckReceived, EvTimeout, EvReset,
                             EvHandshakeFailed> {
 public:
  using BaseBridgeState::on_event;
  etl::fsm_state_id_t on_event(const EvAckReceived&) {
    return static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED);
  }
};

class FaultState : public BaseBridgeState<FaultState, StateId::FAULT, EvReset> {
 public:
  using BaseBridgeState::on_event;
  etl::fsm_state_id_t on_enter_state() override {
    bridge::hal::forceSafeState();
    return etl::ifsm_state::No_State_Change;
  }
};

class EnteringBootloaderState
    : public BaseBridgeState<EnteringBootloaderState,
                             StateId::ENTERING_BOOTLOADER, EvReset, EvTimeout> {
 public:
  using BaseBridgeState::on_event;
  etl::fsm_state_id_t on_enter_state() override {
    bridge::hal::forceSafeState();
    return etl::ifsm_state::No_State_Change;
  }
};

class ChangingBaudrateState
    : public BaseBridgeState<ChangingBaudrateState, StateId::CHANGING_BAUDRATE,
                             EvBaudrateApplied, EvReset, EvTimeout> {
 public:
  using BaseBridgeState::on_event;
  etl::fsm_state_id_t on_event(const EvBaudrateApplied&) {
    return static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED);
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
  EnteringBootloaderState _entering_bootloader_state;
  ChangingBaudrateState _changing_baudrate_state;

  etl::array<etl::ifsm_state*, 8> _state_table;
};

}  // namespace bridge::fsm

#endif

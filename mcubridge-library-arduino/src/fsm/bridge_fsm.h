#ifndef BRIDGE_FSM_H
#define BRIDGE_FSM_H

#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#include <etl/fsm.h>
#include <etl/message.h>

#include "hal/hal.h"

namespace bridge::fsm {

enum class StateId : uint8_t {
  STARTUP = 0,
  UNSYNCHRONIZED = 1,
  HANDSHAKE = 2,
  SYNCHRONIZED = 3,
  FAULT = 4,
  ENTERING_BOOTLOADER = 5,
  CHANGING_BAUDRATE = 6
};

// --- Events ---
struct EvHandshakeStart : public etl::message<0> {};
struct EvHandshakeComplete : public etl::message<1> {};
struct EvHandshakeFailed : public etl::message<2> {};
struct EvTimeout : public etl::message<3> {};
struct EvReset : public etl::message<4> {};
struct EvEnterBootloader : public etl::message<5> {};
struct EvBaudrateChange : public etl::message<6> {};
struct EvBaudrateApplied : public etl::message<7> {};

// --- Forward Declarations ---
class BridgeFsm;
class StartupState;
class UnsynchronizedState;
class HandshakeState;
class SynchronizedState;
class FaultState;
class EnteringBootloaderState;
class ChangingBaudrateState;

// --- Base CRTP State Template ---

template <typename Derived, StateId SId, bool ForceSafeOnEnter,
          typename... Events>
class BaseBridgeState
    : public etl::fsm_state<BridgeFsm, Derived,
                            static_cast<etl::fsm_state_id_t>(SId), Events...> {
 public:
  etl::fsm_state_id_t on_enter_state() override {
    if constexpr (ForceSafeOnEnter) {
      bridge::hal::forceSafeState();
    }
    return etl::ifsm_state::No_State_Change;
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
  static etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return static_cast<etl::fsm_state_id_t>(SId);
  }
};

// --- State Classes ---

class StartupState
    : public BaseBridgeState<StartupState, StateId::STARTUP, false, EvReset,
                             EvHandshakeFailed, EvTimeout> {};

class UnsynchronizedState
    : public BaseBridgeState<UnsynchronizedState, StateId::UNSYNCHRONIZED,
                             false, EvHandshakeStart, EvReset,
                             EvHandshakeFailed, EvTimeout> {
 public:
  using BaseBridgeState::on_event;
  etl::fsm_state_id_t on_event(const EvHandshakeStart&) {
    return static_cast<etl::fsm_state_id_t>(StateId::HANDSHAKE);
  }
};

class HandshakeState
    : public BaseBridgeState<HandshakeState, StateId::HANDSHAKE, false,
                             EvHandshakeComplete, EvHandshakeFailed, EvReset,
                             EvTimeout> {
 public:
  using BaseBridgeState::on_event;
  etl::fsm_state_id_t on_event(const EvHandshakeComplete&) {
    return static_cast<etl::fsm_state_id_t>(StateId::SYNCHRONIZED);
  }
};

class SynchronizedState
    : public BaseBridgeState<SynchronizedState, StateId::SYNCHRONIZED, false,
                             EvReset, EvHandshakeFailed, EvTimeout,
                             EvEnterBootloader, EvBaudrateChange> {
 public:
  using BaseBridgeState::on_event;
  etl::fsm_state_id_t on_event(const EvEnterBootloader&) {
    return static_cast<etl::fsm_state_id_t>(StateId::ENTERING_BOOTLOADER);
  }
  etl::fsm_state_id_t on_event(const EvBaudrateChange&) {
    return static_cast<etl::fsm_state_id_t>(StateId::CHANGING_BAUDRATE);
  }
};

class FaultState
    : public BaseBridgeState<FaultState, StateId::FAULT, true, EvReset> {};

class EnteringBootloaderState
    : public BaseBridgeState<EnteringBootloaderState,
                             StateId::ENTERING_BOOTLOADER, true, EvReset,
                             EvTimeout> {};

class ChangingBaudrateState
    : public BaseBridgeState<ChangingBaudrateState, StateId::CHANGING_BAUDRATE,
                             false, EvBaudrateApplied, EvHandshakeFailed,
                             EvReset, EvTimeout> {
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

 private:
  StartupState _startup_state;
  UnsynchronizedState _unsynchronized_state;
  HandshakeState _handshake_state;
  SynchronizedState _synchronized_state;
  FaultState _fault_state;
  EnteringBootloaderState _entering_bootloader_state;
  ChangingBaudrateState _changing_baudrate_state;

  etl::array<etl::ifsm_state*, 7> _state_table;
};

}  // namespace bridge::fsm

#endif

/**
 * @file bridge_fsm.h
 * @brief ETL-based Finite State Machine for Arduino MCU Bridge v2
 * 
 * [SIL-2 COMPLIANCE - IEC 61508]
 * This module implements a deterministic state machine using ETL's FSM
 * framework. All state transitions are explicit, logged, and bounded.
 *
 * States:
 *   - Unsynchronized (0): Initial state. Only handshake commands allowed.
 *   - Idle (1): Synchronized and ready for commands.
 *   - AwaitingAck (2): Sent a critical command, waiting for acknowledgment.
 *   - Fault (3): Safety state for unrecoverable errors.
 *
 * Events:
 *   - EvHandshakeComplete: Handshake succeeded → Idle
 *   - EvSendCritical: Critical command sent → AwaitingAck
 *   - EvAckReceived: ACK received → Idle
 *   - EvTimeout: ACK timeout exhausted → Unsynchronized
 *   - EvReset: Manual reset / error recovery → Unsynchronized
 *   - EvCryptoFault: Cryptographic POST failure → Fault
 */
#ifndef BRIDGE_FSM_H
#define BRIDGE_FSM_H

#include "etl/fsm.h"
#include "etl/message.h"
#include "etl/callback_timer.h"

namespace bridge {
namespace fsm {

// Forward declaration
class BridgeFsm;

// ============================================================================
// State IDs - Must be sequential starting from 0
// ============================================================================
enum StateId : etl::fsm_state_id_t {
  STATE_UNSYNCHRONIZED = 0,
  STATE_IDLE = 1,
  STATE_AWAITING_ACK = 2,
  STATE_FAULT = 3,
  NUMBER_OF_STATES = 4
};

// ============================================================================
// Event IDs - Unique message identifiers
// ============================================================================
enum EventId : etl::message_id_t {
  EVENT_HANDSHAKE_COMPLETE = 0,
  EVENT_SEND_CRITICAL = 1,
  EVENT_ACK_RECEIVED = 2,
  EVENT_TIMEOUT = 3,
  EVENT_RESET = 4,
  EVENT_CRYPTO_FAULT = 5
};

// ============================================================================
// Event Messages
// ============================================================================
struct EvHandshakeComplete : public etl::message<EVENT_HANDSHAKE_COMPLETE> {};
struct EvSendCritical : public etl::message<EVENT_SEND_CRITICAL> {};
struct EvAckReceived : public etl::message<EVENT_ACK_RECEIVED> {};
struct EvTimeout : public etl::message<EVENT_TIMEOUT> {};
struct EvReset : public etl::message<EVENT_RESET> {};
struct EvCryptoFault : public etl::message<EVENT_CRYPTO_FAULT> {};

// ============================================================================
// State: Unsynchronized (Initial State)
// ============================================================================
class StateUnsynchronized : public etl::fsm_state<BridgeFsm, StateUnsynchronized, STATE_UNSYNCHRONIZED,
                                                   EvHandshakeComplete, EvReset, EvCryptoFault>
{
public:
  etl::fsm_state_id_t on_enter_state() {
    // Entry action: system is in startup/reset mode
    return STATE_UNSYNCHRONIZED;
  }

  etl::fsm_state_id_t on_event(const EvHandshakeComplete&) {
    return STATE_IDLE;  // Handshake success → Idle
  }

  etl::fsm_state_id_t on_event(const EvReset&) {
    return No_State_Change;  // Already unsynchronized
  }

  etl::fsm_state_id_t on_event(const EvCryptoFault&) {
    return STATE_FAULT;  // Crypto failure → Fault
  }

  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return No_State_Change;  // Ignore unknown events
  }
};

// ============================================================================
// State: Idle (Synchronized, Ready)
// ============================================================================
class StateIdle : public etl::fsm_state<BridgeFsm, StateIdle, STATE_IDLE,
                                         EvSendCritical, EvHandshakeComplete, EvReset, EvCryptoFault>
{
public:
  etl::fsm_state_id_t on_enter_state() {
    return STATE_IDLE;
  }

  etl::fsm_state_id_t on_event(const EvSendCritical&) {
    return STATE_AWAITING_ACK;  // Critical send → AwaitingAck
  }

  etl::fsm_state_id_t on_event(const EvHandshakeComplete&) {
    return No_State_Change;  // Already synchronized
  }

  etl::fsm_state_id_t on_event(const EvReset&) {
    return STATE_UNSYNCHRONIZED;  // Reset → Unsynchronized
  }

  etl::fsm_state_id_t on_event(const EvCryptoFault&) {
    return STATE_FAULT;  // Crypto failure → Fault
  }

  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return No_State_Change;
  }
};

// ============================================================================
// State: AwaitingAck (Waiting for acknowledgment)
// ============================================================================
class StateAwaitingAck : public etl::fsm_state<BridgeFsm, StateAwaitingAck, STATE_AWAITING_ACK,
                                                EvAckReceived, EvTimeout, EvSendCritical, EvReset, EvCryptoFault>
{
public:
  etl::fsm_state_id_t on_enter_state() {
    return STATE_AWAITING_ACK;
  }

  etl::fsm_state_id_t on_event(const EvAckReceived&) {
    return STATE_IDLE;  // ACK received → Idle
  }

  etl::fsm_state_id_t on_event(const EvTimeout&) {
    return STATE_UNSYNCHRONIZED;  // Timeout → Unsynchronized (safe state)
  }

  etl::fsm_state_id_t on_event(const EvSendCritical&) {
    return No_State_Change;  // Command will be queued
  }

  etl::fsm_state_id_t on_event(const EvReset&) {
    return STATE_UNSYNCHRONIZED;  // Reset → Unsynchronized
  }

  etl::fsm_state_id_t on_event(const EvCryptoFault&) {
    return STATE_FAULT;  // Crypto failure → Fault
  }

  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return No_State_Change;
  }
};

// ============================================================================
// State: Fault (Terminal safety state)
// ============================================================================
class StateFault : public etl::fsm_state<BridgeFsm, StateFault, STATE_FAULT,
                                          EvReset, EvCryptoFault>
{
public:
  etl::fsm_state_id_t on_enter_state() {
    // Entry action: halt all operations
    return STATE_FAULT;
  }

  etl::fsm_state_id_t on_event(const EvReset&) {
    // Allow recovery from Fault via explicit reset
    return STATE_UNSYNCHRONIZED;
  }

  etl::fsm_state_id_t on_event(const EvCryptoFault&) {
    return No_State_Change;  // Already in fault
  }

  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return No_State_Change;  // Fault state ignores all other events
  }
};

// ============================================================================
// FSM Class
// ============================================================================
class BridgeFsm : public etl::fsm
{
public:
  BridgeFsm()
    : etl::fsm(NUMBER_OF_STATES)
    , state_list_{}
  {
  }

  void begin() {
    // [SIL-2] Use static state instances to save stack/instance RAM
    // These are effectively singletons.
    static StateUnsynchronized state_unsynchronized;
    static StateIdle state_idle;
    static StateAwaitingAck state_awaiting_ack;
    static StateFault state_fault;

    // Register states with FSM
    state_list_[STATE_UNSYNCHRONIZED] = &state_unsynchronized;
    state_list_[STATE_IDLE] = &state_idle;
    state_list_[STATE_AWAITING_ACK] = &state_awaiting_ack;
    state_list_[STATE_FAULT] = &state_fault;
    
    set_states(state_list_, NUMBER_OF_STATES);
    start();
  }

  // State Accessors
  bool isUnsynchronized() const { return get_state_id() == STATE_UNSYNCHRONIZED; }
  bool isIdle() const { return get_state_id() == STATE_IDLE; }
  bool isAwaitingAck() const { return get_state_id() == STATE_AWAITING_ACK; }
  bool isFault() const { return get_state_id() == STATE_FAULT; }
  bool isSynchronized() const { return isIdle() || isAwaitingAck(); }

  // Event Triggers
  void handshakeComplete() { receive(EvHandshakeComplete()); }
  void sendCritical() { receive(EvSendCritical()); }
  void ackReceived() { receive(EvAckReceived()); }
  void timeout() { receive(EvTimeout()); }
  void cryptoFault() { receive(EvCryptoFault()); }
  void resetFsm() { receive(EvReset()); }

private:
  // State list for FSM
  etl::ifsm_state* state_list_[NUMBER_OF_STATES];
};

}  // namespace fsm

// ============================================================================
// Timer IDs - ETL Callback Timer Service
// [SIL-2] Centralized scheduler IDs for deterministic timing
// ============================================================================
namespace scheduler {

enum TimerId : uint8_t {
  TIMER_ACK_TIMEOUT = 0,
  TIMER_RX_DEDUPE = 1,
  TIMER_BAUDRATE_CHANGE = 2,
  TIMER_STARTUP_STABILIZATION = 3,
  NUMBER_OF_TIMERS = 4
};

using BridgeTimerService = etl::callback_timer<NUMBER_OF_TIMERS>;

}  // namespace scheduler
}  // namespace bridge

#endif  // BRIDGE_FSM_H

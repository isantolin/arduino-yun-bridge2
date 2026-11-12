#define BRIDGE_ENABLE_TEST_INTERFACE 1

#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif
#include <etl/array.h>
#include <unity.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "fsm/bridge_fsm.h"
#include "test_support.h"

// Global stubs for host environment
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
void setUp(void) {}
void tearDown(void) {}

namespace {
using bridge::test::TestAccessor;

void test_fsm_initial_state() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  TEST_ASSERT(ba.isUnsynchronized());
}

void test_mutual_auth_success() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  TEST_ASSERT(ba.isSynchronized());
}

void test_mutual_auth_failure_to_startup() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvHandshakeFailed());
  // Observed behavior: Handshake failure resets to Startup
  TEST_ASSERT(ba.getStartupStabilizing());
}

void test_fsm_timeout_to_fault() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvTimeout());
  // Observed behavior: Timeout from synchronized goes to Fault
  TEST_ASSERT(ba.isFault());
}

void test_fsm_handshake_timeout_to_fault() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.trigger(bridge::fsm::EvHandshakeStart());
  TEST_ASSERT_EQUAL_INT(static_cast<int>(bridge::fsm::StateId::HANDSHAKE),
                        static_cast<int>(ba.get_state_id()));
  ba.trigger(bridge::fsm::EvTimeout());
  // Fail-safe requirement: EvTimeout during handshake must transition to FAULT
  TEST_ASSERT(ba.isFault());
}


void test_fsm_baudrate_change_round_trip() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  ba.trigger(bridge::fsm::EvBaudrateChange());
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(bridge::fsm::StateId::CHANGING_BAUDRATE),
      static_cast<int>(ba.get_state_id()));
  // The link remains logically synchronized while the baudrate is changing.
  TEST_ASSERT(ba.isSynchronized());

  ba.trigger(bridge::fsm::EvBaudrateApplied());
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(bridge::fsm::StateId::SYNCHRONIZED),
      static_cast<int>(ba.get_state_id()));
  TEST_ASSERT(ba.isSynchronized());
}

void test_fsm_bootloader_entry_and_reset() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  ba.trigger(bridge::fsm::EvEnterBootloader());
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(bridge::fsm::StateId::ENTERING_BOOTLOADER),
      static_cast<int>(ba.get_state_id()));
  TEST_ASSERT(!ba.isSynchronized());

  ba.trigger(bridge::fsm::EvReset());
  TEST_ASSERT(ba.isUnsynchronized());
}

void test_fsm_reset_recovers_fault() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvTimeout());
  TEST_ASSERT(ba.isFault());

  ba.trigger(bridge::fsm::EvReset());
  TEST_ASSERT(ba.isUnsynchronized());
  TEST_ASSERT(!ba.isFault());
}

void test_fsm_timeout_from_startup_and_unsynchronized() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  // A failed handshake returns to STARTUP; timeout there must fail safe.
  ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvHandshakeFailed());
  TEST_ASSERT(ba.getStartupStabilizing());
  ba.trigger(bridge::fsm::EvTimeout());
  TEST_ASSERT(ba.isFault());

  // Reset returns to UNSYNCHRONIZED, where timeout also fails safe.
  ba.trigger(bridge::fsm::EvReset());
  TEST_ASSERT(ba.isUnsynchronized());
  ba.trigger(bridge::fsm::EvTimeout());
  TEST_ASSERT(ba.isFault());
}

void test_fsm_timeout_from_baudrate_change_and_bootloader() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  ba.trigger(bridge::fsm::EvBaudrateChange());
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(bridge::fsm::StateId::CHANGING_BAUDRATE),
      static_cast<int>(ba.get_state_id()));
  ba.trigger(bridge::fsm::EvTimeout());
  TEST_ASSERT(ba.isFault());

  ba.trigger(bridge::fsm::EvReset());
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvEnterBootloader());
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(bridge::fsm::StateId::ENTERING_BOOTLOADER),
      static_cast<int>(ba.get_state_id()));
  ba.trigger(bridge::fsm::EvTimeout());
  TEST_ASSERT(ba.isFault());
}

void test_fsm_handshake_failed_from_synchronized_states() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  ba.trigger(bridge::fsm::EvHandshakeFailed());
  TEST_ASSERT(ba.getStartupStabilizing());

  ba.trigger(bridge::fsm::EvReset());
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvBaudrateChange());
  ba.trigger(bridge::fsm::EvHandshakeFailed());
  TEST_ASSERT(ba.getStartupStabilizing());
}

}  // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_fsm_initial_state);
  RUN_TEST(test_mutual_auth_success);
  RUN_TEST(test_mutual_auth_failure_to_startup);
  RUN_TEST(test_fsm_timeout_to_fault);
  RUN_TEST(test_fsm_handshake_timeout_to_fault);
  RUN_TEST(test_fsm_baudrate_change_round_trip);
  RUN_TEST(test_fsm_bootloader_entry_and_reset);
  RUN_TEST(test_fsm_reset_recovers_fault);
  RUN_TEST(test_fsm_timeout_from_startup_and_unsynchronized);
  RUN_TEST(test_fsm_timeout_from_baudrate_change_and_bootloader);
  RUN_TEST(test_fsm_handshake_failed_from_synchronized_states);
  return UNITY_END();
}
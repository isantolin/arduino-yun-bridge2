#define BRIDGE_ENABLE_TEST_INTERFACE 1

#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif
#include <etl/array.h>
#include "pw_unit_test/framework.h"

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "fsm/bridge_fsm.h"
#include "test_support.h"

// Global stubs for host environment
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
namespace {
using bridge::test::TestAccessor;

TEST(FsmMutualAuth, FsmInitialState) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  EXPECT_TRUE(ba.isUnsynchronized());
}

TEST(FsmMutualAuth, MutualAuthSuccess) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  EXPECT_TRUE(ba.isSynchronized());
}

TEST(FsmMutualAuth, MutualAuthFailureToStartup) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvHandshakeFailed());
  // Observed behavior: Handshake failure resets to Startup
  EXPECT_TRUE(ba.getStartupStabilizing());
}

TEST(FsmMutualAuth, FsmTimeoutToFault) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvTimeout());
  // Observed behavior: Timeout from synchronized goes to Fault
  EXPECT_TRUE(ba.isFault());
}

TEST(FsmMutualAuth, FsmHandshakeTimeoutToFault) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.trigger(bridge::fsm::EvHandshakeStart());
  EXPECT_EQ(static_cast<int>(bridge::fsm::StateId::HANDSHAKE), static_cast<int>(ba.get_state_id()));
  ba.trigger(bridge::fsm::EvTimeout());
  // Fail-safe requirement: EvTimeout during handshake must transition to FAULT
  EXPECT_TRUE(ba.isFault());
}

TEST(FsmMutualAuth, FsmBaudrateChangeRoundTrip) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  ba.trigger(bridge::fsm::EvBaudrateChange());
  EXPECT_EQ(static_cast<int>(bridge::fsm::StateId::CHANGING_BAUDRATE), static_cast<int>(ba.get_state_id()));
  // The link remains logically synchronized while the baudrate is changing.
  EXPECT_TRUE(ba.isSynchronized());

  ba.trigger(bridge::fsm::EvBaudrateApplied());
  EXPECT_EQ(static_cast<int>(bridge::fsm::StateId::SYNCHRONIZED), static_cast<int>(ba.get_state_id()));
  EXPECT_TRUE(ba.isSynchronized());
}

TEST(FsmMutualAuth, FsmBootloaderEntryAndReset) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  ba.trigger(bridge::fsm::EvEnterBootloader());
  EXPECT_EQ(static_cast<int>(bridge::fsm::StateId::ENTERING_BOOTLOADER), static_cast<int>(ba.get_state_id()));
  EXPECT_TRUE(!ba.isSynchronized());

  ba.trigger(bridge::fsm::EvReset());
  EXPECT_TRUE(ba.isUnsynchronized());
}

TEST(FsmMutualAuth, FsmResetRecoversFault) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvTimeout());
  EXPECT_TRUE(ba.isFault());

  ba.trigger(bridge::fsm::EvReset());
  EXPECT_TRUE(ba.isUnsynchronized());
  EXPECT_TRUE(!ba.isFault());
}

TEST(FsmMutualAuth, FsmTimeoutFromStartupAndUnsynchronized) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  // A failed handshake returns to STARTUP; timeout there must fail safe.
  ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvHandshakeFailed());
  EXPECT_TRUE(ba.getStartupStabilizing());
  ba.trigger(bridge::fsm::EvTimeout());
  EXPECT_TRUE(ba.isFault());

  // Reset returns to UNSYNCHRONIZED, where timeout also fails safe.
  ba.trigger(bridge::fsm::EvReset());
  EXPECT_TRUE(ba.isUnsynchronized());
  ba.trigger(bridge::fsm::EvTimeout());
  EXPECT_TRUE(ba.isFault());
}

TEST(FsmMutualAuth, FsmTimeoutFromBaudrateChangeAndBootloader) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  ba.trigger(bridge::fsm::EvBaudrateChange());
  EXPECT_EQ(static_cast<int>(bridge::fsm::StateId::CHANGING_BAUDRATE), static_cast<int>(ba.get_state_id()));
  ba.trigger(bridge::fsm::EvTimeout());
  EXPECT_TRUE(ba.isFault());

  ba.trigger(bridge::fsm::EvReset());
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvEnterBootloader());
  EXPECT_EQ(static_cast<int>(bridge::fsm::StateId::ENTERING_BOOTLOADER), static_cast<int>(ba.get_state_id()));
  ba.trigger(bridge::fsm::EvTimeout());
  EXPECT_TRUE(ba.isFault());
}

TEST(FsmMutualAuth, FsmHandshakeFailedFromSynchronizedStates) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  ba.trigger(bridge::fsm::EvHandshakeFailed());
  EXPECT_TRUE(ba.getStartupStabilizing());

  ba.trigger(bridge::fsm::EvReset());
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvBaudrateChange());
  ba.trigger(bridge::fsm::EvHandshakeFailed());
  EXPECT_TRUE(ba.getStartupStabilizing());
}

}  // namespace

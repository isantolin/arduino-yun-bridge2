#include <memory>
#include <stdlib.h>

#include <pw_thread/thread.h>
#include <pw_thread_stl/options.h>

#include "BridgeFaultInjection.h"
#include "hal/hal_internal.h"

// Defines the global Bridge instance as a TestAccessor so that test code can
// access protected members of BridgeClass via TestAccessor::create().
// Replaces Bridge.cpp in test builds; never compiled in production firmware.

#define BRIDGE_NO_GLOBAL_EXTERN
#include "BridgeTestInterface.h"
#undef BRIDGE_NO_GLOBAL_EXTERN

static_assert(sizeof(bridge::test::TestAccessor) == sizeof(BridgeClass),
              "TestAccessor must be layout-compatible with BridgeClass");

bridge::test::TestAccessor Bridge(Serial);

namespace {

class HostTransferWorker {
 public:
  explicit HostTransferWorker(bridge::test::TestAccessor& bridge)
      : bridge_(bridge) {
    start();
  }

  HostTransferWorker(const HostTransferWorker&) = delete;
  HostTransferWorker& operator=(const HostTransferWorker&) = delete;

  ~HostTransferWorker() { stop(); }

  void start() {
    if (worker_) return;
    worker_ = std::make_unique<pw::thread::Thread>(
        pw::thread::stl::Options(), bridge_.transferThread());
  }

  void stop() {
    if (!worker_) return;
    bridge_.prepareForHostTransferWorkerStop();
    bridge_.transferThread().Terminate();
    worker_->join();
    worker_.reset();
  }

 private:
  bridge::test::TestAccessor& bridge_;
  std::unique_ptr<pw::thread::Thread> worker_;
};

HostTransferWorker g_host_transfer_worker(Bridge);

}  // namespace

namespace bridge::test {

void startHostTransferWorker() { g_host_transfer_worker.start(); }

void stopHostTransferWorker() { g_host_transfer_worker.stop(); }

}  // namespace bridge::test

// Host-only overrides belong to the test binary, never to the firmware HAL.
// The default production self-test implementation remains the code under test.
namespace bridge::hal {

bool run_power_on_self_tests() {
  if (bridge::test::fault::consume(
          bridge::test::fault::FaultPoint::BRIDGE_FORCE_POST_FAIL)) {
    return false;
  }
  return internal::run_power_on_self_tests_impl();
}

[[noreturn]] void enterBootloader() { ::_Exit(0); }

}  // namespace bridge::hal

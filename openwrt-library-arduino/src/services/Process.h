#ifndef SERVICES_PROCESS_H
#define SERVICES_PROCESS_H

#include "config/bridge_config.h"

#if BRIDGE_ENABLE_PROCESS
#include "etl/circular_buffer.h"
#include "etl/delegate.h"
#include "etl/string_view.h"
#include "protocol/rpc_protocol.h"

#if defined(BRIDGE_HOST_TEST)
namespace bridge {
namespace test {
class ProcessTestAccessor;
}
}  // namespace bridge
#endif

class BridgeClass;

class ProcessClass {
#if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::ProcessTestAccessor;
#endif
 public:
  using ProcessRunHandler = etl::delegate<void(
      rpc::StatusCode, const uint8_t*, uint16_t, const uint8_t*, uint16_t)>;
  using ProcessPollHandler =
      etl::delegate<void(rpc::StatusCode, uint8_t, const uint8_t*, uint16_t,
                         const uint8_t*, uint16_t)>;
  using ProcessRunAsyncHandler = etl::delegate<void(int16_t)>;

  ProcessClass();
  void reset();
  void run(etl::string_view command);
  void runAsync(etl::string_view command);
  void poll(int16_t pid);
  void kill(int16_t pid);

  inline void onProcessRunResponse(ProcessRunHandler handler) {
    _process_run_handler = handler;
  }
  inline void onProcessPollResponse(ProcessPollHandler handler) {
    _process_poll_handler = handler;
  }
  inline void onProcessRunAsyncResponse(ProcessRunAsyncHandler handler) {
    _process_run_async_handler = handler;
  }

 private:
  friend class BridgeClass;
  bool _pushPendingProcessPid(uint16_t pid);
  uint16_t _popPendingProcessPid();

  ProcessRunHandler _process_run_handler;
  ProcessPollHandler _process_poll_handler;
  ProcessRunAsyncHandler _process_run_async_handler;

  // [SIL-2] Use circular buffer for safe PID tracking
  etl::circular_buffer<uint16_t, BRIDGE_MAX_PENDING_PROCESS_POLLS>
      _pending_process_pids;
};

extern ProcessClass Process;
#endif

#endif

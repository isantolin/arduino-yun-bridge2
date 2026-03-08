#ifndef SERVICES_PROCESS_H
#define SERVICES_PROCESS_H

#include "config/bridge_config.h"

#if BRIDGE_ENABLE_PROCESS
#include "etl/circular_buffer.h"
#include "etl/delegate.h"
#include "etl/span.h"
#include "etl/string_view.h"
#include "protocol/rpc_protocol.h"
#include "router/command_router.h"
#include "etl/message_router.h"

class ProcessClass : public etl::imessage_router {
 public:
  using ProcessPollHandler = etl::delegate<void(rpc::StatusCode, uint8_t, etl::span<const uint8_t>, etl::span<const uint8_t>)>;
  using ProcessRunAsyncHandler = etl::delegate<void(int16_t)>;

  ProcessClass();

  // [SIL-2] imessage_router interface
  void receive(const etl::imessage& msg) override;
  bool accepts(etl::message_id_t id) const override;
  bool is_null_router() const override { return false; }
  bool is_producer() const override { return true; }
  bool is_consumer() const override { return true; }

  void reset();
  void runAsync(etl::string_view command);
  void poll(int16_t pid);
  void kill(int16_t pid);

  inline void onProcessPollResponse(ProcessPollHandler handler) { _process_poll_handler = handler; }
  inline void onProcessRunAsyncResponse(ProcessRunAsyncHandler handler) { _process_run_async_handler = handler; }

  bool _pushPendingProcessPid(uint16_t pid);
  uint16_t _popPendingProcessPid();

  ProcessPollHandler _process_poll_handler;
  ProcessRunAsyncHandler _process_run_async_handler;
  etl::circular_buffer<uint16_t, BRIDGE_MAX_PENDING_PROCESS_POLLS> _pending_process_pids;
};

extern ProcessClass Process;
#endif
#endif

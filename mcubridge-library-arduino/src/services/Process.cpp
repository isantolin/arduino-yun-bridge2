#include "services/Process.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_PROCESS

ProcessClass::ProcessClass() {}

void ProcessClass::_onKillNotification(const rpc_pb_ProcessKill& msg) {
  (void)msg;
}

void ProcessClass::_onRunAsyncResponse(const rpc_pb_ProcessRunAsyncResponse& msg) {
  if (_pending_run_async.empty()) return;
  PendingRunAsync pending = _pending_run_async.front();
  _pending_run_async.pop();
  if (pending.handler.is_valid()) {
    pending.handler(static_cast<int32_t>(msg.pid));
  }
}

void ProcessClass::_onPollResponse(const rpc_pb_ProcessPollResponse& msg) {
  if (_pending_polls.empty()) return;
  PendingPoll pending = _pending_polls.front();
  _pending_polls.pop();
  if (pending.handler.is_valid()) {
    pending.handler(static_cast<rpc::StatusCode>(msg.status),
                    static_cast<uint16_t>(msg.exit_code),
                    etl::span<const uint8_t>(msg.stdout_data.bytes, msg.stdout_data.size),
                    etl::span<const uint8_t>(msg.stderr_data.bytes, msg.stderr_data.size));
  }
}

void ProcessClass::runAsync(etl::string_view command, etl::span<const etl::string_view> args, ProcessRunHandler handler) {
  (void)args;
  rpc_pb_ProcessRunAsync p = rpc_pb_ProcessRunAsync_init_default;
  rpc::payload::copy_to_pb_string(p.command, command);
  if (Bridge.send(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, 0, rpc_pb_ProcessRunAsync_fields, p)) {
    PendingRunAsync pending = {handler};
    Process._pending_run_async.push(pending);
  }
}

void ProcessClass::poll(int32_t pid, ProcessPollHandler handler) {
  rpc_pb_ProcessPoll p = rpc_pb_ProcessPoll_init_default;
  p.pid = static_cast<uint32_t>(pid);
  if (Bridge.send(rpc::CommandId::CMD_PROCESS_POLL, 0, rpc_pb_ProcessPoll_fields, p)) {
    PendingPoll pending = {pid, handler};
    _pending_polls.push(pending);
  }
}

void ProcessClass::kill(int32_t pid) {
  rpc_pb_ProcessKill p = rpc_pb_ProcessKill_init_default;
  p.pid = static_cast<uint32_t>(pid);
  (void)Bridge.send(rpc::CommandId::CMD_PROCESS_KILL, 0, rpc_pb_ProcessKill_fields, p);
}

void ProcessClass::reset() {
  while (!_pending_run_async.empty()) _pending_run_async.pop();
  while (!_pending_polls.empty()) _pending_polls.pop();
}

ProcessClass Process;

#endif

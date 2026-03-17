#include "Process.h"
#include "Bridge.h"
#include "util/pb_copy.h"

#if BRIDGE_ENABLE_PROCESS

ProcessClass::ProcessClass() {}

void ProcessClass::runAsync(etl::string_view command, etl::span<const etl::string_view> args, ProcessRunAsyncHandler handler) {
  if (_pending_async_runs.full()) return;
  rpc::payload::ProcessRunAsync msg = {};
  rpc::util::pb_copy_join(command, args, msg.command, sizeof(msg.command));

  if (Bridge.sendPbCommand(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, msg)) {
    _pending_async_runs.push({handler});
  }
}

void ProcessClass::poll(int16_t pid, ProcessPollHandler handler) {
  if (_pending_polls.full()) return;
  rpc::payload::ProcessPoll msg = {};
  msg.pid = pid;
  if (Bridge.sendPbCommand(rpc::CommandId::CMD_PROCESS_POLL, msg)) {
    _pending_polls.push({pid, handler});
  }
}

void ProcessClass::kill(int16_t pid) {
  rpc::payload::ProcessKill msg = {};
  msg.pid = pid;
  Bridge.sendPbCommand(rpc::CommandId::CMD_PROCESS_KILL, msg);
}

void ProcessClass::_onRunAsyncResponse(const rpc::payload::ProcessRunAsyncResponse& msg) {
  if (_pending_async_runs.empty()) return;
  PendingAsyncRun pending = _pending_async_runs.front();
  _pending_async_runs.pop();
  if (pending.handler.is_valid()) {
    pending.handler(static_cast<int16_t>(msg.pid));
  }
}

void ProcessClass::_onPollResponse(const rpc::payload::ProcessPollResponse& msg, etl::span<const uint8_t> stdout_data, etl::span<const uint8_t> stderr_data) {
  if (_pending_polls.empty()) return;
  PendingPoll pending = _pending_polls.front();
  _pending_polls.pop();
  if (pending.handler.is_valid()) {
    pending.handler(static_cast<rpc::StatusCode>(msg.status), 
                    static_cast<uint8_t>(msg.exit_code),
                    stdout_data,
                    stderr_data);
  }
}

void ProcessClass::reset() {
  _pending_async_runs.clear();
  _pending_polls.clear();
}
#endif

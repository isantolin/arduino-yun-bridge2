#include "Process.h"
#include "Bridge.h"
#include "util/string_copy.h"

#if BRIDGE_ENABLE_PROCESS

ProcessClass::ProcessClass() {}

[[maybe_unused]] void ProcessClass::runAsync(etl::string_view command, etl::span<const etl::string_view> args, ProcessRunAsyncHandler handler) {
  if (_pending_async_runs.full()) return;
  rpc::payload::ProcessRunAsync msg = {};
  rpc::util::copy_join(command, args, msg.command, sizeof(msg.command));

  if (Bridge.sendPbCommand(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, 0, msg)) {
    _pending_async_runs.push({handler});
  }
}

[[maybe_unused]] void ProcessClass::poll(int32_t pid, ProcessPollHandler handler) {
  if (_pending_polls.full()) return;
  rpc::payload::ProcessPoll msg = {};
  msg.pid = pid;
  if (Bridge.sendPbCommand(rpc::CommandId::CMD_PROCESS_POLL, 0, msg)) {
    _pending_polls.push({pid, handler});
  }
}

[[maybe_unused]] void ProcessClass::kill(int32_t pid, ProcessKillHandler handler) {
  rpc::payload::ProcessKill msg = {};
  msg.pid = pid;
  if (Bridge.sendPbCommand(rpc::CommandId::CMD_PROCESS_KILL, 0, msg)) {
    if (handler.is_valid()) handler(rpc::StatusCode::STATUS_OK);
  } else {
    if (handler.is_valid()) handler(rpc::StatusCode::STATUS_ERROR);
  }
}

bool ProcessClass::_kill(uint32_t pid) {
  (void)pid;
  // [SIL-2] Single-task MCU: Kill is primarily an acknowledgement 
  // that a session or polling task should stop.
  return true;
}

void ProcessClass::_onRunAsyncResponse(const rpc::payload::ProcessRunAsyncResponse& msg) {
  if (_pending_async_runs.empty()) return;
  const auto& pending = _pending_async_runs.front();
  if (pending.handler.is_valid()) {
    pending.handler(static_cast<int32_t>(msg.pid));
  }
  _pending_async_runs.pop();
}

void ProcessClass::_onPollResponse(const rpc::payload::ProcessPollResponse& msg, etl::span<const uint8_t> stdout_data, etl::span<const uint8_t> stderr_data) {
  if (_pending_polls.empty()) return;
  const auto& pending = _pending_polls.front();
  if (pending.handler.is_valid()) {
    pending.handler(static_cast<rpc::StatusCode>(msg.status), 
                    static_cast<uint8_t>(msg.exit_code),
                    stdout_data,
                    stderr_data);
  }
  _pending_polls.pop();
}

void ProcessClass::reset() {
  _pending_async_runs.clear();
  _pending_polls.clear();
}
#endif

#include "services/Process.h"
#include "Bridge.h"

#if BRIDGE_ENABLE_PROCESS

ProcessClass::ProcessClass() {}

void ProcessClass::runAsync(etl::string_view cmd, etl::span<const etl::string_view> args, etl::delegate<void(int32_t)> handler) {
  (void)handler; // Async implementation detail
  (void)args;
  rpc::payload::ProcessRunAsync msg = {};
  msg.command = cmd;
  (void)Bridge.send(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, 0, msg);
}

void ProcessClass::poll(int32_t pid, ProcessPollHandler handler) {
  (void)handler; // Async implementation detail
  rpc::payload::ProcessPoll msg = {};
  msg.pid = static_cast<uint32_t>(pid);
  if (Bridge.send(rpc::CommandId::CMD_PROCESS_POLL, 0, msg)) {
    _pending_polls.push({pid});
  }
}

void ProcessClass::kill(int32_t pid) {
  rpc::payload::ProcessKill msg = {};
  msg.pid = static_cast<uint32_t>(pid);
  (void)Bridge.send(rpc::CommandId::CMD_PROCESS_KILL, 0, msg);
}

void ProcessClass::_kill(const rpc::payload::ProcessKill& msg) {
  (void)msg;
}

void ProcessClass::_onRunAsyncResponse(const rpc::payload::ProcessRunAsyncResponse& msg) {
  (void)msg; // Notification
}

void ProcessClass::_onPollResponse(const rpc::payload::ProcessPollResponse& msg) {
  (void)msg; // Notification
}

void ProcessClass::reset() {
  _pending_polls.clear();
}

#ifndef BRIDGE_TEST_NO_GLOBALS
ProcessClass Process;
#endif

#endif

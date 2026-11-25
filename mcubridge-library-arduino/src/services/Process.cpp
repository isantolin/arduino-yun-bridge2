#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#include "services/Process.h"

#include <etl/algorithm.h>
#include <pw_span/span.h>
#include <pw_string/string.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_PROCESS

namespace {

constexpr int32_t kProcessInvalidPid = -1;

}  // namespace

void ProcessClass::runAsync(etl::string_view cmd,
                            pw::span<const etl::string_view> args,
                            typename ProcessClass::ProcessRunHandler handler) {
  if (Process._run_call.active()) {
    Bridge.emitStatus(pw::Status::ResourceExhausted());
    if (handler.is_valid()) handler(kProcessInvalidPid);
    return;
  }

  pw::InlineString<rpc::MAX_PAYLOAD_SIZE> command_buffer;
  bool ok = (cmd.size() <= command_buffer.max_size());
  if (ok) {
    command_buffer.append(cmd.data(), cmd.size());
    ok = etl::all_of(args.begin(), args.end(), [&](const auto& arg) {
      if (1U + arg.size() > command_buffer.max_size() - command_buffer.size()) {
        return false;
      }
      command_buffer.push_back(' ');
      command_buffer.append(arg.data(), arg.size());
      return true;
    });
  }

  if (!ok) {
    Bridge.emitStatus(pw::Status::InvalidArgument());
    if (handler.is_valid()) handler(kProcessInvalidPid);
    return;
  }

  rpc_pb_ProcessRunAsync p = rpc_pb_ProcessRunAsync_init_default;
  bridge::pb::copy_string(
      p.command, sizeof(p.command),
      etl::string_view(command_buffer.data(), command_buffer.size()));

  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(pw::Status::Unavailable());
    if (handler.is_valid()) handler(kProcessInvalidPid);
    return;
  }
  Process._pending_run_handler = handler;
  Process._run_call = Bridge.processClient().RunAsync(
      p,
      [](const rpc_pb_ProcessRunAsyncResponse& resp, pw::Status status) {
        Process._onRunAsyncResponse(resp, status);
      },
      [](pw::Status status) { Process._onRunAsyncError(status); });
}

void ProcessClass::poll(int32_t pid,
                        typename ProcessClass::ProcessPollHandler handler) {
  if (_poll_call.active()) {
    Bridge.emitStatus(pw::Status::ResourceExhausted());
    if (handler.is_valid()) {
      handler(pw::Status::ResourceExhausted(), 0U, {}, {});
    }
    return;
  }

  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(pw::Status::Unavailable());
    if (handler.is_valid()) {
      handler(pw::Status::Unavailable(), 0U, {}, {});
    }
    return;
  }

  rpc_pb_ProcessPoll p = rpc_pb_ProcessPoll_init_default;
  p.pid = static_cast<uint32_t>(pid);

  _pending_poll_handler = handler;
  _poll_call = Bridge.processClient().Poll(
      p,
      [](const rpc_pb_ProcessPollResponse& resp, pw::Status status) {
        Process._onPollResponse(resp, status);
      },
      [](pw::Status status) { Process._onPollError(status); });
}

void ProcessClass::kill(int32_t pid) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(pw::Status::Unavailable());
    return;
  }
  rpc_pb_ProcessKill p = rpc_pb_ProcessKill_init_default;
  p.pid = static_cast<uint32_t>(pid);

  (void)Bridge.processClient().Kill(p);
}

void ProcessClass::_onRunAsyncResponse(
    const rpc_pb_ProcessRunAsyncResponse& msg, pw::Status status) {
  if (!status.ok()) {
    _onRunAsyncError(status);
    return;
  }
  if (_pending_run_handler.is_valid()) {
    ProcessRunHandler handler = _pending_run_handler;
    _pending_run_handler = {};
    handler(static_cast<int32_t>(msg.pid));
  }
}

void ProcessClass::_onRunAsyncError(pw::Status status) {
  Bridge.emitStatus(status);
  if (_pending_run_handler.is_valid()) {
    ProcessRunHandler handler = _pending_run_handler;
    _pending_run_handler = {};
    handler(kProcessInvalidPid);
  }
}

void ProcessClass::_onPollResponse(const rpc_pb_ProcessPollResponse& msg,
                                   pw::Status status) {
  if (!status.ok()) {
    _onPollError(status);
    return;
  }
  if (_pending_poll_handler.is_valid()) {
    ProcessPollHandler handler = _pending_poll_handler;
    _pending_poll_handler = {};
    handler(
        pw::Status(static_cast<pw::Status::Code>(msg.status)), msg.exit_code,
        pw::span<const uint8_t>(msg.stdout_data.bytes, msg.stdout_data.size),
        pw::span<const uint8_t>(msg.stderr_data.bytes, msg.stderr_data.size));
  }
}

void ProcessClass::_onPollError(pw::Status status) {
  Bridge.emitStatus(status);
  if (_pending_poll_handler.is_valid()) {
    ProcessPollHandler handler = _pending_poll_handler;
    _pending_poll_handler = {};
    handler(status, 0U, {}, {});
  }
}

void ProcessClass::reset() {
  _run_call = {};
  _poll_call = {};
  _pending_run_handler = {};
  _pending_poll_handler = {};
}

ProcessType Process;

#endif

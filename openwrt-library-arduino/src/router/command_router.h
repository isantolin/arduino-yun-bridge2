/**
 * @file command_router.h
 * @brief ETL-based Command Router for Arduino MCU Bridge v2
 * 
 * [SIL-2 COMPLIANCE - IEC 61508]
 * This module implements message-based command routing using ETL's
 * message_router framework. Commands are categorized by ID ranges
 * (defined in rpc_protocol.h, auto-generated from spec.toml) and
 * dispatched to appropriate handlers with minimal stack depth.
 *
 * Command Categories (from rpc_protocol.h):
 *   - MSG_STATUS: RPC_STATUS_CODE_MIN..MAX
 *   - MSG_SYSTEM: RPC_SYSTEM_COMMAND_MIN..MAX
 *   - MSG_GPIO: RPC_GPIO_COMMAND_MIN..MAX
 *   - MSG_CONSOLE: RPC_CONSOLE_COMMAND_MIN..MAX
 *   - MSG_DATASTORE: RPC_DATASTORE_COMMAND_MIN..MAX
 *   - MSG_MAILBOX: RPC_MAILBOX_COMMAND_MIN..MAX
 *   - MSG_FILESYSTEM: RPC_FILESYSTEM_COMMAND_MIN..MAX
 *   - MSG_PROCESS: RPC_PROCESS_COMMAND_MIN..MAX
 *   - MSG_UNKNOWN: Unrecognized commands
 */
#ifndef COMMAND_ROUTER_H
#define COMMAND_ROUTER_H

#include "etl/message.h"
#include "etl/message_router.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"

namespace bridge {
namespace router {

// ============================================================================
// Message IDs - One per command category for efficient routing
// ============================================================================
enum MessageId : etl::message_id_t {
  MSG_STATUS = 0,
  MSG_SYSTEM = 1,
  MSG_GPIO = 2,
  MSG_CONSOLE = 3,
  MSG_DATASTORE = 4,
  MSG_MAILBOX = 5,
  MSG_FILESYSTEM = 6,
  MSG_PROCESS = 7,
  MSG_UNKNOWN = 8,
  NUMBER_OF_MESSAGES = 9
};

// ============================================================================
// Command Message - Carries an RPC frame with category tag
// ============================================================================
// [RAM OPT] Use pointer to avoid frame copy during routing
struct CommandContext {
  const rpc::Frame* frame;         // Effective frame (decompressed if needed)
  uint16_t raw_command;            // Original command ID without flags
  bool is_duplicate;               // Deduplication check result
  bool requires_ack;               // Whether ACK should be sent after handling
};

// ============================================================================
// Category-specific Messages
// ============================================================================
struct MsgStatus : public etl::message<MSG_STATUS> {
  CommandContext ctx;
  explicit MsgStatus(const CommandContext& c) : ctx(c) {}
};

struct MsgSystem : public etl::message<MSG_SYSTEM> {
  CommandContext ctx;
  explicit MsgSystem(const CommandContext& c) : ctx(c) {}
};

struct MsgGpio : public etl::message<MSG_GPIO> {
  CommandContext ctx;
  explicit MsgGpio(const CommandContext& c) : ctx(c) {}
};

struct MsgConsole : public etl::message<MSG_CONSOLE> {
  CommandContext ctx;
  explicit MsgConsole(const CommandContext& c) : ctx(c) {}
};

struct MsgDataStore : public etl::message<MSG_DATASTORE> {
  CommandContext ctx;
  explicit MsgDataStore(const CommandContext& c) : ctx(c) {}
};

struct MsgMailbox : public etl::message<MSG_MAILBOX> {
  CommandContext ctx;
  explicit MsgMailbox(const CommandContext& c) : ctx(c) {}
};

struct MsgFileSystem : public etl::message<MSG_FILESYSTEM> {
  CommandContext ctx;
  explicit MsgFileSystem(const CommandContext& c) : ctx(c) {}
};

struct MsgProcess : public etl::message<MSG_PROCESS> {
  CommandContext ctx;
  explicit MsgProcess(const CommandContext& c) : ctx(c) {}
};

struct MsgUnknown : public etl::message<MSG_UNKNOWN> {
  CommandContext ctx;
  explicit MsgUnknown(const CommandContext& c) : ctx(c) {}
};

// ============================================================================
// Command Categorizer - Maps command ID to message category
// ============================================================================
inline MessageId categorize_command(uint16_t raw_command) {
  if (raw_command >= rpc::RPC_STATUS_CODE_MIN && raw_command <= rpc::RPC_STATUS_CODE_MAX) {
    return MSG_STATUS;
  }
  if (raw_command >= rpc::RPC_SYSTEM_COMMAND_MIN && raw_command <= rpc::RPC_SYSTEM_COMMAND_MAX) {
    return MSG_SYSTEM;
  }
  if (raw_command >= rpc::RPC_GPIO_COMMAND_MIN && raw_command <= rpc::RPC_GPIO_COMMAND_MAX) {
    return MSG_GPIO;
  }
  if (raw_command >= rpc::RPC_CONSOLE_COMMAND_MIN && raw_command <= rpc::RPC_CONSOLE_COMMAND_MAX) {
    return MSG_CONSOLE;
  }
  if (raw_command >= rpc::RPC_DATASTORE_COMMAND_MIN && raw_command <= rpc::RPC_DATASTORE_COMMAND_MAX) {
    return MSG_DATASTORE;
  }
  if (raw_command >= rpc::RPC_MAILBOX_COMMAND_MIN && raw_command <= rpc::RPC_MAILBOX_COMMAND_MAX) {
    return MSG_MAILBOX;
  }
  if (raw_command >= rpc::RPC_FILESYSTEM_COMMAND_MIN && raw_command <= rpc::RPC_FILESYSTEM_COMMAND_MAX) {
    return MSG_FILESYSTEM;
  }
  if (raw_command >= rpc::RPC_PROCESS_COMMAND_MIN && raw_command <= rpc::RPC_PROCESS_COMMAND_MAX) {
    return MSG_PROCESS;
  }
  return MSG_UNKNOWN;
}

// ============================================================================
// Handler Interface - Bridge implements this to receive routed commands
// ============================================================================
class ICommandHandler {
public:
  virtual ~ICommandHandler() {}
  virtual void onStatusCommand(const CommandContext& ctx) = 0;
  virtual void onSystemCommand(const CommandContext& ctx) = 0;
  virtual void onGpioCommand(const CommandContext& ctx) = 0;
  virtual void onConsoleCommand(const CommandContext& ctx) = 0;
  virtual void onDataStoreCommand(const CommandContext& ctx) = 0;
  virtual void onMailboxCommand(const CommandContext& ctx) = 0;
  virtual void onFileSystemCommand(const CommandContext& ctx) = 0;
  virtual void onProcessCommand(const CommandContext& ctx) = 0;
  virtual void onUnknownCommand(const CommandContext& ctx) = 0;
};

// ============================================================================
// Command Router - ETL message_router for command dispatch
// [SIL-2] Flattened call stack via message-based routing
// ============================================================================
class CommandRouter : public etl::message_router<CommandRouter,
                                                  MsgStatus,
                                                  MsgSystem,
                                                  MsgGpio,
                                                  MsgConsole,
                                                  MsgDataStore,
                                                  MsgMailbox,
                                                  MsgFileSystem,
                                                  MsgProcess,
                                                  MsgUnknown>
{
public:
  CommandRouter() 
    : message_router(ROUTER_ID)
    , _handler(nullptr)
  {
  }

  void setHandler(ICommandHandler* handler) {
    _handler = handler;
  }

  // Route a command context to the appropriate handler
  void route(const CommandContext& ctx) {
    switch (categorize_command(ctx.raw_command)) {
      case MSG_STATUS:     receive(MsgStatus(ctx));     break;
      case MSG_SYSTEM:     receive(MsgSystem(ctx));     break;
      case MSG_GPIO:       receive(MsgGpio(ctx));       break;
      case MSG_CONSOLE:    receive(MsgConsole(ctx));    break;
      case MSG_DATASTORE:  receive(MsgDataStore(ctx));  break;
      case MSG_MAILBOX:    receive(MsgMailbox(ctx));    break;
      case MSG_FILESYSTEM: receive(MsgFileSystem(ctx)); break;
      case MSG_PROCESS:    receive(MsgProcess(ctx));    break;
      default:             receive(MsgUnknown(ctx));    break;
    }
  }

  // ETL message handlers - dispatch to ICommandHandler
  void on_receive(const MsgStatus& msg)     { if (_handler) _handler->onStatusCommand(msg.ctx); }
  void on_receive(const MsgSystem& msg)     { if (_handler) _handler->onSystemCommand(msg.ctx); }
  void on_receive(const MsgGpio& msg)       { if (_handler) _handler->onGpioCommand(msg.ctx); }
  void on_receive(const MsgConsole& msg)    { if (_handler) _handler->onConsoleCommand(msg.ctx); }
  void on_receive(const MsgDataStore& msg)  { if (_handler) _handler->onDataStoreCommand(msg.ctx); }
  void on_receive(const MsgMailbox& msg)    { if (_handler) _handler->onMailboxCommand(msg.ctx); }
  void on_receive(const MsgFileSystem& msg) { if (_handler) _handler->onFileSystemCommand(msg.ctx); }
  void on_receive(const MsgProcess& msg)    { if (_handler) _handler->onProcessCommand(msg.ctx); }
  void on_receive(const MsgUnknown& msg)    { if (_handler) _handler->onUnknownCommand(msg.ctx); }

  void on_receive_unknown(const etl::imessage&) {
    // Should not happen - all categories are handled
  }

private:
  static constexpr etl::message_router_id_t ROUTER_ID = 1;
  ICommandHandler* _handler;
};

}  // namespace router
}  // namespace bridge

#endif // COMMAND_ROUTER_H

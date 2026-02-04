/**
 * @file command_router.h
 * @brief ETL-based Command Router for Arduino MCU Bridge v2
 * 
 * [SIL-2 COMPLIANCE - IEC 61508]
 * This module implements message-based command routing using ETL's
 * message_router framework. Commands are categorized by ID ranges and
 * dispatched to appropriate handlers with minimal stack depth.
 *
 * Command Categories (Message IDs):
 *   - MSG_STATUS (0): Status codes 0x30-0x3F
 *   - MSG_SYSTEM (1): System commands 0x40-0x4F
 *   - MSG_GPIO (2): GPIO commands 0x50-0x5F
 *   - MSG_CONSOLE (3): Console commands 0x60-0x6F
 *   - MSG_DATASTORE (4): DataStore responses 0x70-0x7F
 *   - MSG_MAILBOX (5): Mailbox responses 0x80-0x8F
 *   - MSG_FILESYSTEM (6): FileSystem responses 0x90-0x9F
 *   - MSG_PROCESS (7): Process responses 0xA0-0xAF
 *   - MSG_UNKNOWN (8): Unrecognized commands
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
  // GPIO: 0x50-0x5F (80-95)
  if (raw_command >= 0x50 && raw_command <= 0x5F) {
    return MSG_GPIO;
  }
  // Console: 0x60-0x6F (96-111)
  if (raw_command >= 0x60 && raw_command <= 0x6F) {
    return MSG_CONSOLE;
  }
  // DataStore: 0x70-0x7F (112-127)
  if (raw_command >= 0x70 && raw_command <= 0x7F) {
    return MSG_DATASTORE;
  }
  // Mailbox: 0x80-0x8F (128-143)
  if (raw_command >= 0x80 && raw_command <= 0x8F) {
    return MSG_MAILBOX;
  }
  // FileSystem: 0x90-0x9F (144-159)
  if (raw_command >= 0x90 && raw_command <= 0x9F) {
    return MSG_FILESYSTEM;
  }
  // Process: 0xA0-0xAF (160-175)
  if (raw_command >= 0xA0 && raw_command <= 0xAF) {
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

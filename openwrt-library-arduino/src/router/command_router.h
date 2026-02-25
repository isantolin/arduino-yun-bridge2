/**
 * @file command_router.h
 * @brief Optimized Command Router for Arduino MCU Bridge v2
 * 
 * [SIL-2 COMPLIANCE - IEC 61508]
 * This module implements a direct command routing mechanism.
 * It eliminates the overhead of generic message routers to save Flash space
 * on small microcontrollers while maintaining strict type safety and
 * deterministic dispatch logic.
 */
#ifndef COMMAND_ROUTER_H
#define COMMAND_ROUTER_H

#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"

namespace bridge {
namespace router {

/**
 * @brief Command Message - Carries an RPC frame with metadata.
 */
struct CommandContext {
  const rpc::Frame* frame;         // Effective frame (decompressed if needed)
  uint16_t raw_command;            // Original command ID without flags
  bool is_duplicate;               // Deduplication check result
  bool requires_ack;               // Whether ACK should be sent after handling
};

/**
 * @brief Handler Interface - Bridge implements this to receive routed commands.
 */
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

/**
 * @brief Command Router - Optimized direct dispatch.
 * [SIL-2] Minimal stack depth and zero template bloat.
 */
class CommandRouter {
public:
  CommandRouter() : _handler(nullptr) {}

  void setHandler(ICommandHandler* handler) {
    _handler = handler;
  }

  /**
   * @brief Route a command context to the appropriate handler.
   * Uses optimized range checks to minimize Flash footprint.
   */
  void route(const CommandContext& ctx) {
    if (!_handler) return;

    const uint16_t cmd = ctx.raw_command;

    if (cmd >= rpc::RPC_STATUS_CODE_MIN && cmd <= rpc::RPC_STATUS_CODE_MAX) {
      _handler->onStatusCommand(ctx);
    } else if (cmd >= rpc::RPC_SYSTEM_COMMAND_MIN && cmd <= rpc::RPC_SYSTEM_COMMAND_MAX) {
      _handler->onSystemCommand(ctx);
    } else if (cmd >= rpc::RPC_GPIO_COMMAND_MIN && cmd <= rpc::RPC_GPIO_COMMAND_MAX) {
      _handler->onGpioCommand(ctx);
    } else if (cmd >= rpc::RPC_CONSOLE_COMMAND_MIN && cmd <= rpc::RPC_CONSOLE_COMMAND_MAX) {
      _handler->onConsoleCommand(ctx);
    } else if (cmd >= rpc::RPC_DATASTORE_COMMAND_MIN && cmd <= rpc::RPC_DATASTORE_COMMAND_MAX) {
      _handler->onDataStoreCommand(ctx);
    } else if (cmd >= rpc::RPC_MAILBOX_COMMAND_MIN && cmd <= rpc::RPC_MAILBOX_COMMAND_MAX) {
      _handler->onMailboxCommand(ctx);
    } else if (cmd >= rpc::RPC_FILESYSTEM_COMMAND_MIN && cmd <= rpc::RPC_FILESYSTEM_COMMAND_MAX) {
      _handler->onFileSystemCommand(ctx);
    } else if (cmd >= rpc::RPC_PROCESS_COMMAND_MIN && cmd <= rpc::RPC_PROCESS_COMMAND_MAX) {
      _handler->onProcessCommand(ctx);
    } else {
      _handler->onUnknownCommand(ctx);
    }
  }

private:
  ICommandHandler* _handler;
};

}  // namespace router
}  // namespace bridge

#endif // COMMAND_ROUTER_H

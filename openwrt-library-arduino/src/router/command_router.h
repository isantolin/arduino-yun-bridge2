/**
 * @file command_router.h
 * @brief Optimized Command Router for Arduino MCU Bridge v2
 * 
 * [SIL-2 COMPLIANCE - IEC 61508]
 * This module implements an O(1) command routing mechanism using ETL's
 * message router framework.
 */
#ifndef COMMAND_ROUTER_H
#define COMMAND_ROUTER_H

#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "etl/message_router.h"
#include "etl/message.h"

namespace bridge {
namespace router {

/**
 * @brief Command Message - Carries an RPC frame with metadata.
 */
struct CommandContext : public etl::imessage {
  CommandContext(const rpc::Frame* f, uint16_t raw, bool dup, bool ack)
      : frame(f), raw_command(raw), is_duplicate(dup), requires_ack(ack) {}

  etl::message_id_t get_message_id() const override {
    return static_cast<etl::message_id_t>(raw_command);
  }

  const rpc::Frame* frame;         // Effective frame (decompressed if needed)
  uint16_t raw_command;            // Original command ID without flags
  bool is_duplicate;               // Deduplication check result
  bool requires_ack;               // Whether ACK should be sent after handling
};

/**
 * @brief Handler Interface - Bridge implements this to receive routed commands.
 */
class ICommandHandler : public etl::imessage_router {
public:
  ICommandHandler() : etl::imessage_router(0) {}
  virtual ~ICommandHandler() {}

  // Granular handlers for O(1) dispatch categories
  virtual void onStatusCommand(const CommandContext& ctx) = 0;
  virtual void onSystemCommand(const CommandContext& ctx) = 0;
  virtual void onGpioCommand(const CommandContext& ctx) = 0;
  virtual void onConsoleCommand(const CommandContext& ctx) = 0;
  virtual void onDataStoreCommand(const CommandContext& ctx) = 0;
  virtual void onMailboxCommand(const CommandContext& ctx) = 0;
  virtual void onFileSystemCommand(const CommandContext& ctx) = 0;
  virtual void onProcessCommand(const CommandContext& ctx) = 0;
  virtual void onUnknownCommand(const CommandContext& ctx) = 0;

  // ETL imessage_router interface
  void receive(const etl::imessage& msg) override {
    onUnknownCommand(static_cast<const CommandContext&>(msg));
  }

  bool accepts(etl::message_id_t) const override { return true; }
  bool is_null_router() const override { return false; }
  bool is_producer() const override { return true; }
  bool is_consumer() const override { return true; }
};

}  // namespace router
}  // namespace bridge

#endif // COMMAND_ROUTER_H

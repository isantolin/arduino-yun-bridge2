/**
 * @file command_router.h
 * @brief Optimized Command Router for Arduino MCU Bridge v2
 *
 * [SIL-2 COMPLIANCE - IEC 61508]
 * This module implements an O(1) command routing mechanism.
 *
 * [RAM-OPT] etl::imessage_router is intentionally NOT used here.
 * The ETL message_router framework adds vtable + router_id + state overhead
 * (~8-12 bytes RAM) with no functional benefit: dispatch() already uses a
 * direct switch statement for O(1) routing.  Keeping this as a pure virtual
 * interface saves RAM and Flash on constrained AVR targets.
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
  CommandContext(const rpc::Frame* f, uint16_t raw, bool dup, bool ack, uint16_t seq)
      : frame(f), raw_command(raw), is_duplicate(dup), requires_ack(ack), sequence_id(seq) {}

  const rpc::Frame* frame;  // Effective frame (decompressed if needed)
  uint16_t raw_command;     // Original command ID without flags
  bool is_duplicate;        // Deduplication check result
  bool requires_ack;        // Whether ACK should be sent after handling
  uint16_t sequence_id;     // Sequence ID for tracking
};

/**
 * @brief Handler Interface - Bridge implements this to receive routed commands.
 */
class ICommandHandler {
 public:
  virtual ~ICommandHandler() = default;

  // Granular handlers for O(1) dispatch categories
  virtual void onStatusCommand(const CommandContext& ctx) = 0;
  virtual void onSystemCommand(const CommandContext& ctx) = 0;
  virtual void onGpioCommand(const CommandContext& ctx) = 0;
  virtual void onConsoleCommand(const CommandContext& ctx) = 0;
  virtual void onDataStoreCommand(const CommandContext& ctx) = 0;
  virtual void onMailboxCommand(const CommandContext& ctx) = 0;
  virtual void onFileSystemCommand(const CommandContext& ctx) = 0;
  virtual void onProcessCommand(const CommandContext& ctx) = 0;
  virtual void onSpiCommand(const CommandContext& ctx) = 0;
  virtual void onUnknownCommand(const CommandContext& ctx) = 0;
};

}  // namespace router
}  // namespace bridge

#endif  // COMMAND_ROUTER_H

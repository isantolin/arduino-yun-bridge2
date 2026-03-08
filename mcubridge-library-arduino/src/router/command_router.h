/**
 * @file command_router.h
 * @brief ETL-based Command Router for Arduino MCU Bridge v2
 */
#ifndef COMMAND_ROUTER_H
#define COMMAND_ROUTER_H

#include "etl/message.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"

namespace bridge {
namespace router {

/**
 * @brief Command Message - Wraps an RPC frame as an ETL message.
 */
struct CommandMessage : public etl::imessage {
  CommandMessage(const rpc::Frame* f, uint16_t raw, bool dup, bool ack)
      : etl::imessage(),
        frame(f),
        raw_command(raw),
        is_duplicate(dup),
        requires_ack(ack) {}

  etl::message_id_t get_message_id() const override {
    return static_cast<etl::message_id_t>(raw_command);
  }

  const rpc::Frame* frame;  // Effective frame (decompressed if needed)
  uint16_t raw_command;     // Original command ID without flags
  bool is_duplicate;        // Deduplication check result
  bool requires_ack;        // Whether ACK should be sent after handling
};

}  // namespace router
}  // namespace bridge

#endif  // COMMAND_ROUTER_H

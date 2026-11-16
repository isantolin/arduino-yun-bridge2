#pragma once

#include "config/bridge_config.h"
#include "protocol/rpc_protocol.h"

#if BRIDGE_ENABLE_PW_TRANSFER

#include <avr/io.h>
#include <pw_transfer/transfer.h>
#include <stddef.h>
#include <stdint.h>

namespace bridge::services {

inline constexpr uint32_t kEepromTransferResourceId = 0x4D454741UL;
#if defined(E2END)
inline constexpr size_t kEepromPhysicalSize = static_cast<size_t>(E2END) + 1U;
#else
inline constexpr size_t kEepromPhysicalSize = 4096U;
#endif
// The EEPROM tail holds the link boot epoch (ADR-0005) and is never exposed.
inline constexpr size_t kEepromTransferResourceSize =
    kEepromPhysicalSize - bridge::config::LINK_EPOCH_EEPROM_RESERVED_BYTES;
// The cloud contract (ADR-0007) publishes this resource ID and the largest
// image size; keep the firmware in lock-step with the canonical constants.
static_assert(kEepromTransferResourceId == rpc::RPC_EEPROM_TRANSFER_RESOURCE_ID,
              "EEPROM resource ID diverges from config_schema.proto");
static_assert(kEepromTransferResourceSize <= rpc::RPC_EEPROM_RESOURCE_MAX_BYTES,
              "EEPROM resource exceeds the cloud contract size");

bool registerEepromTransferHandler(pw::transfer::TransferService& service);

}  // namespace bridge::services

#endif  // BRIDGE_ENABLE_PW_TRANSFER

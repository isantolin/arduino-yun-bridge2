#pragma once

#if defined(__AVR_ATmega2560__)

#include <cstddef>
#include <cstdint>

#include <pw_transfer/transfer.h>

namespace bridge::services {

inline constexpr uint32_t kEepromTransferResourceId = 0x4D454741UL;
inline constexpr size_t kEepromTransferResourceSize = 4096U;

bool registerEepromTransferHandler(pw::transfer::TransferService& service);

}  // namespace bridge::services

#endif  // defined(__AVR_ATmega2560__)

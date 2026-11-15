#pragma once

#if defined(ARDUINO_ARCH_AVR) || defined(__AVR__)

#include <avr/io.h>
#include <pw_transfer/transfer.h>
#include <stddef.h>
#include <stdint.h>

namespace bridge::services {

inline constexpr uint32_t kEepromTransferResourceId = 0x4D454741UL;
#if defined(E2END)
inline constexpr size_t kEepromTransferResourceSize =
    static_cast<size_t>(E2END) + 1U;
#else
inline constexpr size_t kEepromTransferResourceSize = 4096U;
#endif

bool registerEepromTransferHandler(pw::transfer::TransferService& service);

}  // namespace bridge::services

#endif  // defined(ARDUINO_ARCH_AVR) || defined(__AVR__)

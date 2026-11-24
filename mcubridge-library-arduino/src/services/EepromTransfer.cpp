#include "services/EepromTransfer.h"

#if defined(ARDUINO_ARCH_AVR) || defined(__AVR__)

#include <EEPROM.h>
#include <pw_stream/stream.h>
#include <pw_transfer/handler.h>
#include <stddef.h>
#include <stdint.h>

#include "services/CooperativeTransferHandler.h"

namespace bridge::services {
namespace {

constexpr size_t kEepromWriteWindowBytes = 1U;

class EepromStream final : public pw::stream::SeekableReaderWriter {
 public:
  void Reset(size_t offset = 0) { position_ = offset; }

 private:
  // Only the transfer resource is reachable; the reserved tail (link boot
  // epoch) is never read or written through pw_transfer.
  size_t Capacity() const { return kEepromTransferResourceSize; }

  size_t ConservativeLimit(LimitType type) const override {
    const size_t capacity = Capacity();
    const size_t remaining = position_ < capacity ? capacity - position_ : 0U;
    if (type == LimitType::kRead) return remaining;

    // EEPROM.update() waits about 3.3 ms per changed byte on the AVR EEPROM
    // controller. Keep the advertised write window at one byte so the UART
    // receive path can be serviced between EEPROM programming operations.
    // Larger windows can block the cooperative transfer poll long enough to
    // overrun the serial receive buffer; throughput is measured by Fleet Mesh.
    return remaining < kEepromWriteWindowBytes ? remaining
                                               : kEepromWriteWindowBytes;
  }

  pw::StatusWithSize DoRead(pw::ByteSpan destination) override {
    const size_t capacity = Capacity();
    if (position_ >= capacity) return pw::StatusWithSize::OutOfRange();

    const size_t remaining = capacity - position_;
    const size_t count = destination.size_bytes() < remaining
                             ? destination.size_bytes()
                             : remaining;
    for (size_t i = 0; i < count; ++i) {
      destination[i] = static_cast<pw::ByteSpan::element_type>(
          EEPROM.read(static_cast<int>(position_ + i)));
    }
    position_ += count;
    return pw::StatusWithSize(count);
  }

  pw::Status DoWrite(pw::ConstByteSpan data) override {
    const size_t capacity = Capacity();
    if (position_ > capacity || data.size_bytes() > capacity - position_) {
      return pw::Status::OutOfRange();
    }

    for (size_t i = 0; i < data.size_bytes(); ++i) {
      EEPROM.update(static_cast<int>(position_ + i),
                    static_cast<uint8_t>(data[i]));
    }
    position_ += data.size_bytes();
    return pw::OkStatus();
  }

  pw::Status DoSeek(ptrdiff_t offset, Whence origin) override {
    int32_t base = 0;
    const int32_t capacity = static_cast<int32_t>(Capacity());
    switch (origin) {
      case kBeginning:
        base = 0;
        break;
      case kCurrent:
        base = static_cast<int32_t>(position_);
        break;
      case kEnd:
        base = capacity;
        break;
      default:
        return pw::Status::InvalidArgument();
    }

    const int32_t target = base + static_cast<int32_t>(offset);
    if (target < 0 || target > capacity) return pw::Status::OutOfRange();
    position_ = static_cast<size_t>(target);
    return pw::OkStatus();
  }

  size_t DoTell() override { return position_; }

  size_t position_{0};
};

bool g_eeprom_handler_destroyed = false;

class EepromTransferHandler final : public pw::transfer::ReadWriteHandler {
 public:
  EepromTransferHandler()
      : pw::transfer::ReadWriteHandler(kEepromTransferResourceId) {}

  ~EepromTransferHandler() override {
    registration_.detach(*this);
    g_eeprom_handler_destroyed = true;
  }

  bool attach(pw::transfer::TransferService& service,
              pw::transfer::TransferThread& thread) {
    return registration_.attach(*this, service, thread);
  }

  bool detachFrom(const pw::transfer::TransferService& service) {
    return registration_.detachFrom(*this, service);
  }

  pw::Status PrepareRead() override {
    stream_.Reset();
    set_reader_writer(stream_);
    return CapacityStatus();
  }

  pw::Status PrepareRead(uint32_t offset) override {
    if (offset > kEepromTransferResourceSize) {
      return pw::Status::ResourceExhausted();
    }
    stream_.Reset(offset);
    set_reader_writer(stream_);
    return CapacityStatus();
  }

  pw::Status PrepareWrite() override {
    stream_.Reset();
    set_reader_writer(stream_);
    return CapacityStatus();
  }

  pw::Status PrepareWrite(uint32_t offset) override {
    if (offset > kEepromTransferResourceSize) {
      return pw::Status::ResourceExhausted();
    }
    stream_.Reset(offset);
    set_reader_writer(stream_);
    return CapacityStatus();
  }

  pw::Status FinalizeWrite(pw::Status status) override { return status; }

  size_t ResourceSize() const override { return kEepromTransferResourceSize; }

 private:
  pw::Status CapacityStatus() const {
    return EEPROM.length() == static_cast<int>(kEepromPhysicalSize)
               ? pw::OkStatus()
               : pw::Status::FailedPrecondition();
  }

  CooperativeTransferRegistration registration_;
  EepromStream stream_;
};

EepromTransferHandler g_eeprom_transfer_handler;

}  // namespace

bool registerEepromTransferHandler(pw::transfer::TransferService& service,
                                   pw::transfer::TransferThread& thread) {
  return g_eeprom_transfer_handler.attach(service, thread);
}

void unregisterEepromTransferHandler(
    const pw::transfer::TransferService& service) {
  if (g_eeprom_handler_destroyed) return;
  (void)g_eeprom_transfer_handler.detachFrom(service);
}

}  // namespace bridge::services

#endif  // AVR EEPROM hardware resource

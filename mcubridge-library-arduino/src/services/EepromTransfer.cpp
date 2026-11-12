#include "services/EepromTransfer.h"

#if defined(__AVR_ATmega2560__)

#include <EEPROM.h>

#include <cstddef>
#include <cstdint>

#include <pw_stream/stream.h>
#include <pw_transfer/handler.h>

namespace bridge::services {
namespace {

class EepromStream final : public pw::stream::SeekableReaderWriter {
 public:
  void Reset(size_t offset = 0) { position_ = offset; }

 private:
  size_t Capacity() const {
    const int length = EEPROM.length();
    return length > 0 ? static_cast<size_t>(length) : 0U;
  }

  size_t ConservativeLimit(LimitType type) const override {
    const size_t capacity = Capacity();
    const size_t remaining =
        position_ < capacity ? capacity - position_ : 0U;
    if (type == LimitType::kRead) return remaining;

    // EEPROM.update() may wait for the AVR EEPROM controller. Advertise a
    // one-byte receive window so pw_transfer applies backpressure while the
    // MCU completes each physical EEPROM write.
    return remaining == 0U ? 0U : 1U;
  }

  pw::StatusWithSize DoRead(pw::ByteSpan destination) override {
    const size_t capacity = Capacity();
    if (position_ >= capacity) return pw::StatusWithSize::OutOfRange();

    const size_t remaining = capacity - position_;
    const size_t count = destination.size_bytes() < remaining
                             ? destination.size_bytes()
                             : remaining;
    for (size_t i = 0; i < count; ++i) {
      destination[i] =
          static_cast<std::byte>(EEPROM.read(static_cast<int>(position_ + i)));
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
                    std::to_integer<uint8_t>(data[i]));
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

class EepromTransferHandler final : public pw::transfer::ReadWriteHandler {
 public:
  EepromTransferHandler()
      : pw::transfer::ReadWriteHandler(kEepromTransferResourceId) {}

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

  size_t ResourceSize() const override {
    return kEepromTransferResourceSize;
  }

 private:
  pw::Status CapacityStatus() const {
    return EEPROM.length() == static_cast<int>(kEepromTransferResourceSize)
               ? pw::OkStatus()
               : pw::Status::FailedPrecondition();
  }

  EepromStream stream_;
};

EepromTransferHandler g_eeprom_transfer_handler;

}  // namespace

bool registerEepromTransferHandler(pw::transfer::TransferService& service) {
  return service.RegisterHandler(g_eeprom_transfer_handler);
}

}  // namespace bridge::services

#endif  // defined(__AVR_ATmega2560__)

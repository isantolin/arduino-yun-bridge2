#pragma once

#include <pw_transfer/handler.h>
#include <pw_transfer/transfer.h>
#include <pw_transfer/transfer_thread.h>

namespace bridge::services {

// Zero-heap, zero-vtable lifecycle helper for cooperative pw::transfer
// handlers. Using composition instead of a polymorphic mixin avoids emitting
// intermediate vtables in AVR .data SRAM and shares a single non-template
// implementation across ReadOnlyHandler and ReadWriteHandler subclasses.
class CooperativeTransferRegistration final {
 public:
  bool attach(pw::transfer::Handler& handler,
              pw::transfer::TransferService& service,
              pw::transfer::TransferThread& thread) {
    if (service_ == &service) {
      return true;
    }
    detach(handler);
    drain(thread);
    if (!service.RegisterHandler(handler)) {
      return false;
    }
    drain(thread);
    service_ = &service;
    thread_ = &thread;
    return true;
  }

  void detach(pw::transfer::Handler& handler) {
    if (service_ == nullptr) {
      return;
    }
    pw::transfer::TransferService* const service = service_;
    pw::transfer::TransferThread* const thread = thread_;
    service_ = nullptr;
    thread_ = nullptr;
    drain(*thread);
    (void)service->UnregisterHandler(handler);
    drain(*thread);
  }

  bool detachFrom(pw::transfer::Handler& handler,
                  const pw::transfer::TransferService& service) {
    if (service_ != &service) {
      return false;
    }
    detach(handler);
    return true;
  }

 private:
  static void drain(pw::transfer::TransferThread& thread) {
#if defined(PW_TRANSFER_COOPERATIVE)
    (void)thread.Poll(PW_TRANSFER_COOPERATIVE_QUEUE_CAPACITY);
#else
    (void)thread;
#endif
  }

  pw::transfer::TransferService* service_ = nullptr;
  pw::transfer::TransferThread* thread_ = nullptr;
};

}  // namespace bridge::services

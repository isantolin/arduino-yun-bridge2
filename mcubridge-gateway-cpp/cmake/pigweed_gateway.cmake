# Minimal upstream Pigweed modules needed by the Gateway.
# These definitions retain the real upstream headers, sources, and dependency
# edges while excluding unrelated optional modules and their test-only graphs.

# The Gateway needs only the synchronization primitives used by RPC and Transfer.
pw_add_library(pw_sync.lock_annotations INTERFACE
  HEADERS "${PIGWEED_ROOT}/pw_sync/public/pw_sync/lock_annotations.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_sync/public"
  PUBLIC_DEPS pw_preprocessor)
pw_add_library(pw_sync.virtual_basic_lockable INTERFACE
  HEADERS "${PIGWEED_ROOT}/pw_sync/public/pw_sync/virtual_basic_lockable.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_sync/public"
  PUBLIC_DEPS pw_polyfill pw_sync.lock_annotations)
pw_add_facade(pw_sync.interrupt_spin_lock STATIC
  BACKEND pw_sync.interrupt_spin_lock_BACKEND
  HEADERS "${PIGWEED_ROOT}/pw_sync/public/pw_sync/interrupt_spin_lock.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_sync/public"
  PUBLIC_DEPS pw_sync.lock_annotations pw_sync.virtual_basic_lockable pw_preprocessor
  SOURCES "${PIGWEED_ROOT}/pw_sync/interrupt_spin_lock.cc")
pw_add_library(pw_sync_stl.interrupt_spin_lock INTERFACE
  HEADERS
    "${PIGWEED_ROOT}/pw_sync_stl/public/pw_sync_stl/interrupt_spin_lock_inline.h"
    "${PIGWEED_ROOT}/pw_sync_stl/public/pw_sync_stl/interrupt_spin_lock_native.h"
    "${PIGWEED_ROOT}/pw_sync_stl/public_overrides/pw_sync_backend/interrupt_spin_lock_inline.h"
    "${PIGWEED_ROOT}/pw_sync_stl/public_overrides/pw_sync_backend/interrupt_spin_lock_native.h"
  PUBLIC_INCLUDES
    "${PIGWEED_ROOT}/pw_sync_stl/public"
    "${PIGWEED_ROOT}/pw_sync_stl/public_overrides"
  PUBLIC_DEPS pw_sync.interrupt_spin_lock.facade pw_assert)
pw_add_facade(pw_sync.mutex STATIC
  BACKEND pw_sync.mutex_BACKEND
  HEADERS "${PIGWEED_ROOT}/pw_sync/public/pw_sync/mutex.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_sync/public"
  PUBLIC_DEPS pw_sync.lock_annotations pw_sync.virtual_basic_lockable pw_preprocessor
  SOURCES "${PIGWEED_ROOT}/pw_sync/mutex.cc")
pw_add_facade(pw_sync.binary_semaphore STATIC
  BACKEND pw_sync.binary_semaphore_BACKEND
  HEADERS "${PIGWEED_ROOT}/pw_sync/public/pw_sync/binary_semaphore.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_sync/public"
  PUBLIC_DEPS pw_chrono.system_clock pw_preprocessor
  SOURCES "${PIGWEED_ROOT}/pw_sync/binary_semaphore.cc")
pw_add_facade(pw_sync.thread_notification INTERFACE
  BACKEND pw_sync.thread_notification_BACKEND
  HEADERS "${PIGWEED_ROOT}/pw_sync/public/pw_sync/thread_notification.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_sync/public")
pw_add_facade(pw_sync.timed_thread_notification INTERFACE
  BACKEND pw_sync.timed_thread_notification_BACKEND
  HEADERS "${PIGWEED_ROOT}/pw_sync/public/pw_sync/timed_thread_notification.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_sync/public"
  PUBLIC_DEPS pw_sync.thread_notification pw_chrono.system_clock)
pw_add_library(pw_sync.binary_semaphore_thread_notification_backend INTERFACE
  HEADERS
    "${PIGWEED_ROOT}/pw_sync/public/pw_sync/backends/binary_semaphore_thread_notification_inline.h"
    "${PIGWEED_ROOT}/pw_sync/public/pw_sync/backends/binary_semaphore_thread_notification_native.h"
    "${PIGWEED_ROOT}/pw_sync/public_overrides/pw_sync_backend/thread_notification_inline.h"
    "${PIGWEED_ROOT}/pw_sync/public_overrides/pw_sync_backend/thread_notification_native.h"
  PUBLIC_INCLUDES
    "${PIGWEED_ROOT}/pw_sync/public"
    "${PIGWEED_ROOT}/pw_sync/public_overrides"
  PUBLIC_DEPS pw_sync.binary_semaphore)
pw_add_library(pw_sync.binary_semaphore_timed_thread_notification_backend INTERFACE
  HEADERS
    "${PIGWEED_ROOT}/pw_sync/public/pw_sync/backends/binary_semaphore_timed_thread_notification_inline.h"
    "${PIGWEED_ROOT}/pw_sync/public_overrides/pw_sync_backend/timed_thread_notification_inline.h"
  PUBLIC_INCLUDES
    "${PIGWEED_ROOT}/pw_sync/public"
    "${PIGWEED_ROOT}/pw_sync/public_overrides"
  PUBLIC_DEPS pw_chrono.system_clock
    pw_sync.binary_semaphore_thread_notification_backend)
pw_add_library(pw_sync_stl.mutex_backend STATIC
  HEADERS
    "${PIGWEED_ROOT}/pw_sync_stl/public/pw_sync_stl/mutex_inline.h"
    "${PIGWEED_ROOT}/pw_sync_stl/public/pw_sync_stl/mutex_native.h"
    "${PIGWEED_ROOT}/pw_sync_stl/public_overrides/pw_sync_backend/mutex_inline.h"
    "${PIGWEED_ROOT}/pw_sync_stl/public_overrides/pw_sync_backend/mutex_native.h"
  PUBLIC_INCLUDES
    "${PIGWEED_ROOT}/pw_sync_stl/public"
    "${PIGWEED_ROOT}/pw_sync_stl/public_overrides"
  PUBLIC_DEPS pw_sync.mutex.facade
  SOURCES "${PIGWEED_ROOT}/pw_sync_stl/mutex.cc"
  PRIVATE_DEPS pw_assert)
pw_add_library(pw_sync_stl.binary_semaphore_backend STATIC
  HEADERS
    "${PIGWEED_ROOT}/pw_sync_stl/public/pw_sync_stl/binary_semaphore_inline.h"
    "${PIGWEED_ROOT}/pw_sync_stl/public/pw_sync_stl/binary_semaphore_native.h"
    "${PIGWEED_ROOT}/pw_sync_stl/public_overrides/pw_sync_backend/binary_semaphore_inline.h"
    "${PIGWEED_ROOT}/pw_sync_stl/public_overrides/pw_sync_backend/binary_semaphore_native.h"
  PUBLIC_INCLUDES
    "${PIGWEED_ROOT}/pw_sync_stl/public"
    "${PIGWEED_ROOT}/pw_sync_stl/public_overrides"
  PUBLIC_DEPS pw_sync.binary_semaphore.facade
  SOURCES "${PIGWEED_ROOT}/pw_sync_stl/binary_semaphore.cc"
  PRIVATE_DEPS pw_assert pw_chrono.system_clock)

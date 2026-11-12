# Minimal dependency closure using the upstream Pigweed target declarations.
# The module-level pw_hdlc CMakeLists also registers an unrelated async router;
# this Gateway intentionally builds only the decoder, encoder, and RPC output.
pw_add_library(pw_hdlc.common INTERFACE
  HEADERS "${PIGWEED_ROOT}/pw_hdlc/public/pw_hdlc/internal/protocol.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_hdlc/public"
  PUBLIC_DEPS pw_varint)
pw_add_library(pw_hdlc.encoded_size INTERFACE
  HEADERS "${PIGWEED_ROOT}/pw_hdlc/public/pw_hdlc/encoded_size.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_hdlc/public"
  PUBLIC_DEPS pw_hdlc.common pw_bytes pw_span pw_varint)
pw_add_library(pw_hdlc.decoder STATIC
  HEADERS "${PIGWEED_ROOT}/pw_hdlc/public/pw_hdlc/decoder.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_hdlc/public"
  PUBLIC_DEPS pw_hdlc.common pw_bytes pw_checksum pw_checksum.crc32 pw_result pw_span pw_status
  PRIVATE_DEPS pw_log
  SOURCES "${PIGWEED_ROOT}/pw_hdlc/decoder.cc")
pw_add_library(pw_hdlc.encoder STATIC
  HEADERS "${PIGWEED_ROOT}/pw_hdlc/public/pw_hdlc/encoder.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_hdlc/public"
  PUBLIC_DEPS pw_hdlc.common pw_bytes pw_checksum pw_checksum.crc32 pw_span pw_status pw_stream
  PRIVATE_DEPS pw_hdlc.encoded_size
  SOURCES "${PIGWEED_ROOT}/pw_hdlc/encoder.cc")
pw_add_library(pw_hdlc INTERFACE PUBLIC_DEPS pw_hdlc.decoder pw_hdlc.encoded_size pw_hdlc.encoder)
pw_add_library(pw_hdlc.rpc_channel_output INTERFACE
  HEADERS "${PIGWEED_ROOT}/pw_hdlc/public/pw_hdlc/rpc_channel.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_hdlc/public"
  PUBLIC_DEPS pw_hdlc pw_rpc.server pw_span)

# Upstream pw_rpc config and core.
pw_add_module_config(pw_rpc_CONFIG)
pw_add_library(pw_rpc.config INTERFACE
  HEADERS "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/config.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_rpc/public"
  PUBLIC_DEPS ${pw_rpc_CONFIG})
pw_add_library(pw_rpc.log_config INTERFACE
  HEADERS "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/log_config.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_rpc/public"
  PUBLIC_DEPS pw_rpc.config)
pw_proto_library(pw_rpc.protos
  STRIP_PREFIX "${PIGWEED_ROOT}/pw_rpc"
  SOURCES
    "${PIGWEED_ROOT}/pw_rpc/benchmark.proto"
    "${PIGWEED_ROOT}/pw_rpc/internal/packet.proto"
    "${PIGWEED_ROOT}/pw_rpc/echo.proto"
    "${PIGWEED_ROOT}/pw_rpc/query.proto"
  INPUTS
    "${PIGWEED_ROOT}/pw_rpc/benchmark.options"
    "${PIGWEED_ROOT}/pw_rpc/benchmark.pwpb_options"
    "${PIGWEED_ROOT}/pw_rpc/echo.options"
    "${PIGWEED_ROOT}/pw_rpc/echo.pwpb_options"
    "${PIGWEED_ROOT}/pw_rpc/query.options"
    "${PIGWEED_ROOT}/pw_rpc/query.pwpb_options"
  PREFIX pw_rpc)
pw_add_library(pw_rpc.common STATIC
  HEADERS
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/channel.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/call.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/call_context.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/channel_list.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/encoding_buffer.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/endpoint.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/hash.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/lock.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/method.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/method_info.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/method_lookup.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/method_union.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/packet.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/method_id.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/method_info.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/method_type.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/packet_meta.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/service.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/service_id.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/writer.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_rpc/public"
  PUBLIC_DEPS pw_assert pw_bytes pw_containers.intrusive_list pw_function pw_polyfill
    pw_rpc.config pw_rpc.protos.pwpb pw_span pw_status pw_sync.lock_annotations
    pw_memory.no_destructor
  SOURCES
    "${PIGWEED_ROOT}/pw_rpc/call.cc"
    "${PIGWEED_ROOT}/pw_rpc/channel.cc"
    "${PIGWEED_ROOT}/pw_rpc/channel_list.cc"
    "${PIGWEED_ROOT}/pw_rpc/endpoint.cc"
    "${PIGWEED_ROOT}/pw_rpc/packet.cc"
    "${PIGWEED_ROOT}/pw_rpc/packet_meta.cc"
  PRIVATE_DEPS pw_log pw_preprocessor pw_rpc.log_config)
if(NOT "${pw_sync.mutex_BACKEND}" STREQUAL "")
  pw_target_link_targets(pw_rpc.common PUBLIC pw_sync.mutex)
endif()
if(NOT "${pw_thread.sleep_BACKEND}" STREQUAL "")
  pw_target_link_targets(pw_rpc.common PUBLIC pw_thread.sleep)
endif()
pw_add_library(pw_rpc.server STATIC
  HEADERS
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/server.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/call_allocator.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/grpc.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/server_call.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_rpc/public"
  SOURCES "${PIGWEED_ROOT}/pw_rpc/server.cc" "${PIGWEED_ROOT}/pw_rpc/server_call.cc"
    "${PIGWEED_ROOT}/pw_rpc/service.cc"
  PUBLIC_DEPS pw_rpc.common
  PRIVATE_DEPS pw_log pw_rpc.log_config)
pw_add_library(pw_rpc.client STATIC
  HEADERS
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/client.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/client_call.h"
    "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/internal/service_client.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_rpc/public"
  PUBLIC_DEPS pw_result pw_rpc.common pw_span
  SOURCES "${PIGWEED_ROOT}/pw_rpc/client.cc" "${PIGWEED_ROOT}/pw_rpc/client_call.cc"
  PRIVATE_DEPS pw_log pw_rpc.log_config)
pw_add_library(pw_rpc.client_server STATIC
  HEADERS "${PIGWEED_ROOT}/pw_rpc/public/pw_rpc/client_server.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_rpc/public"
  PUBLIC_DEPS pw_rpc.client pw_rpc.server
  SOURCES "${PIGWEED_ROOT}/pw_rpc/client_server.cc")
pw_add_library(pw_rpc.raw.server_api STATIC
  HEADERS
    "${PIGWEED_ROOT}/pw_rpc/raw/public/pw_rpc/raw/internal/method.h"
    "${PIGWEED_ROOT}/pw_rpc/raw/public/pw_rpc/raw/internal/method_union.h"
    "${PIGWEED_ROOT}/pw_rpc/raw/public/pw_rpc/raw/server_reader_writer.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_rpc/raw/public"
  PUBLIC_DEPS pw_rpc.server pw_bytes
  SOURCES "${PIGWEED_ROOT}/pw_rpc/raw/method.cc")
pw_add_library(pw_rpc.raw.client_api INTERFACE
  HEADERS "${PIGWEED_ROOT}/pw_rpc/raw/public/pw_rpc/raw/client_reader_writer.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_rpc/raw/public"
  PUBLIC_DEPS pw_rpc.client pw_bytes)
pw_add_library(pw_rpc.pwpb.common INTERFACE
  HEADERS
    "${PIGWEED_ROOT}/pw_rpc/pwpb/public/pw_rpc/pwpb/internal/common.h"
    "${PIGWEED_ROOT}/pw_rpc/pwpb/public/pw_rpc/pwpb/serde.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_rpc/pwpb/public"
  PUBLIC_DEPS pw_assert.assert pw_bytes pw_log pw_protobuf pw_rpc.client pw_rpc.common
    pw_rpc.log_config pw_rpc.server pw_span pw_status)
pw_add_library(pw_rpc.pwpb.server_api STATIC
  HEADERS
    "${PIGWEED_ROOT}/pw_rpc/pwpb/public/pw_rpc/pwpb/internal/method.h"
    "${PIGWEED_ROOT}/pw_rpc/pwpb/public/pw_rpc/pwpb/internal/method_union.h"
    "${PIGWEED_ROOT}/pw_rpc/pwpb/public/pw_rpc/pwpb/server_reader_writer.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_rpc/pwpb/public"
  PUBLIC_DEPS pw_bytes pw_rpc.config pw_rpc.pwpb.common pw_rpc.raw.server_api pw_rpc.server
  SOURCES "${PIGWEED_ROOT}/pw_rpc/pwpb/server_reader_writer.cc"
  PRIVATE_DEPS pw_log pw_rpc.log_config)
pw_add_library(pw_rpc.pwpb.client_api INTERFACE
  HEADERS "${PIGWEED_ROOT}/pw_rpc/pwpb/public/pw_rpc/pwpb/client_reader_writer.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_rpc/pwpb/public"
  PUBLIC_DEPS pw_function pw_rpc.pwpb.common pw_rpc.common)

# Upstream transfer protocol and server implementation; omit optional allocator
# and atomic-file helper libraries because the Gateway uses its own dirfd-safe
# filesystem handler.
pw_add_module_config(pw_transfer_CONFIG)
pw_add_library(pw_transfer.config INTERFACE
  HEADERS "${PIGWEED_ROOT}/pw_transfer/public/pw_transfer/internal/config.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_transfer/public"
  PUBLIC_DEPS ${pw_transfer_CONFIG} pw_chrono.system_timer)
pw_proto_library(pw_transfer.proto
  STRIP_PREFIX "${PIGWEED_ROOT}/pw_transfer"
  SOURCES "${PIGWEED_ROOT}/pw_transfer/transfer.proto"
  PREFIX pw_transfer)
pw_add_library(pw_transfer.core STATIC
  HEADERS
    "${PIGWEED_ROOT}/pw_transfer/public/pw_transfer/client.h"
    "${PIGWEED_ROOT}/pw_transfer/public/pw_transfer/handler.h"
    "${PIGWEED_ROOT}/pw_transfer/public/pw_transfer/internal/chunk.h"
    "${PIGWEED_ROOT}/pw_transfer/public/pw_transfer/internal/client_context.h"
    "${PIGWEED_ROOT}/pw_transfer/public/pw_transfer/internal/context.h"
    "${PIGWEED_ROOT}/pw_transfer/public/pw_transfer/internal/event.h"
    "${PIGWEED_ROOT}/pw_transfer/public/pw_transfer/internal/protocol.h"
    "${PIGWEED_ROOT}/pw_transfer/public/pw_transfer/internal/server_context.h"
    "${PIGWEED_ROOT}/pw_transfer/public/pw_transfer/rate_estimate.h"
    "${PIGWEED_ROOT}/pw_transfer/public/pw_transfer/transfer_thread.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_transfer/public"
  PUBLIC_DEPS pw_bytes pw_chrono.system_clock pw_containers.intrusive_list pw_function
    pw_result pw_rpc.client pw_rpc.raw.client_api pw_rpc.raw.server_api pw_status
    pw_stream pw_sync.binary_semaphore pw_sync.timed_thread_notification
    pw_thread.thread_core pw_transfer.config pw_transfer.proto.pwpb
    pw_transfer.proto.raw_rpc
  SOURCES
    "${PIGWEED_ROOT}/pw_transfer/chunk.cc"
    "${PIGWEED_ROOT}/pw_transfer/client.cc"
    "${PIGWEED_ROOT}/pw_transfer/client_context.cc"
    "${PIGWEED_ROOT}/pw_transfer/context.cc"
    "${PIGWEED_ROOT}/pw_transfer/rate_estimate.cc"
    "${PIGWEED_ROOT}/pw_transfer/server_context.cc"
    "${PIGWEED_ROOT}/pw_transfer/transfer_thread.cc"
  PRIVATE_DEPS pw_log pw_log.rate_limited pw_protobuf pw_varint)
pw_add_library(pw_transfer STATIC
  HEADERS "${PIGWEED_ROOT}/pw_transfer/public/pw_transfer/transfer.h"
  PUBLIC_INCLUDES "${PIGWEED_ROOT}/pw_transfer/public"
  SOURCES "${PIGWEED_ROOT}/pw_transfer/transfer.cc"
  PUBLIC_DEPS pw_assert pw_bytes pw_result pw_status pw_stream pw_sync.mutex
    pw_transfer.core pw_transfer.proto.raw_rpc
  PRIVATE_DEPS pw_log pw_log.rate_limited pw_transfer.proto.pwpb)

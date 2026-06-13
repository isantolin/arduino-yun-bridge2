/**
 * @file rpc_frame.h
 * @brief Zero-Wrapper Protobuf Framing (SIL-2).
 */
#ifndef RPC_FRAME_H
#define RPC_FRAME_H

#ifdef min
#undef min
#endif
#ifdef max
#undef max
#endif

#include <Arduino.h>
#include <etl/crc32.h>
#include <etl/expected.h>
#include <etl/span.h>

#include "rpc_protocol.h"
#include "rpc_structs.h"

namespace rpc {

inline constexpr size_t AEAD_NONCE_SIZE = rpc::RPC_AEAD_NONCE_SIZE;
inline constexpr size_t AEAD_TAG_SIZE = rpc::RPC_AEAD_TAG_SIZE;
inline constexpr size_t CRC_TRAILER_SIZE = rpc::RPC_CRC_SIZE;
inline constexpr size_t MAX_ENVELOPE_SIZE = rpc_pb_RpcEnvelope_size;
inline constexpr size_t MAX_FRAME_SIZE = MAX_ENVELOPE_SIZE + CRC_TRAILER_SIZE;

namespace checksum {
inline uint32_t compute(etl::span<const uint8_t> data) {
  return etl::crc32(data.begin(), data.end());
}
}

namespace Payload {

template <typename T>
inline uint32_t get_tag();

template <typename T>
inline void set_field(rpc_pb_RpcEnvelope& env, const T& packet);

template <typename T>
inline etl::expected<T, FrameError> parse(const rpc_pb_RpcEnvelope& env);

#define DEFN_PAYLOAD_HELPERS(type, field, tag) \
template <> inline uint32_t get_tag<type>() { return 0; } \
template <> inline void set_field<type>(rpc_pb_RpcEnvelope& env, const type& packet) { \
    (void)env; (void)packet; \
} \
template <> inline etl::expected<type, FrameError> parse<type>(const rpc_pb_RpcEnvelope& env) { \
    if (env.which_payload_type == rpc_pb_RpcEnvelope_encrypted_payload_tag) { \
        type msg = {}; \
        pb_istream_t stream = pb_istream_from_buffer(env.payload_type.encrypted_payload.bytes, env.payload_type.encrypted_payload.size); \
        if (!pb_decode(&stream, rpc::Payload::get_fields<type>(), &msg)) return etl::unexpected<FrameError>(FrameError::MALFORMED); \
        return msg; \
    } \
    return etl::unexpected<FrameError>(FrameError::MALFORMED); \
}

DEFN_PAYLOAD_HELPERS(rpc_pb_VersionResponse, version_resp, rpc_pb_RpcEnvelope_version_resp_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_FreeMemoryResponse, free_memory_resp, rpc_pb_RpcEnvelope_free_memory_resp_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_Capabilities, capabilities, rpc_pb_RpcEnvelope_capabilities_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_PinMode, pin_mode, rpc_pb_RpcEnvelope_pin_mode_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_DigitalWrite, digital_write, rpc_pb_RpcEnvelope_digital_write_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_AnalogWrite, analog_write, rpc_pb_RpcEnvelope_analog_write_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_PinRead, pin_read, rpc_pb_RpcEnvelope_pin_read_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_DigitalReadResponse, digital_read_resp, rpc_pb_RpcEnvelope_digital_read_resp_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_AnalogReadResponse, analog_read_resp, rpc_pb_RpcEnvelope_analog_read_resp_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_ConsoleWrite, console_write, rpc_pb_RpcEnvelope_console_write_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_DatastorePut, datastore_put, rpc_pb_RpcEnvelope_datastore_put_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_DatastoreGet, datastore_get, rpc_pb_RpcEnvelope_datastore_get_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_DatastoreGetResponse, datastore_get_resp, rpc_pb_RpcEnvelope_datastore_get_resp_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_MailboxPush, mailbox_push, rpc_pb_RpcEnvelope_mailbox_push_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_MailboxProcessed, mailbox_processed, rpc_pb_RpcEnvelope_mailbox_processed_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_MailboxAvailableResponse, mailbox_available_resp, rpc_pb_RpcEnvelope_mailbox_available_resp_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_MailboxReadResponse, mailbox_read_resp, rpc_pb_RpcEnvelope_mailbox_read_resp_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_FileWrite, file_write, rpc_pb_RpcEnvelope_file_write_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_FileRead, file_read, rpc_pb_RpcEnvelope_file_read_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_FileRemove, file_remove, rpc_pb_RpcEnvelope_file_remove_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_FileReadResponse, file_read_resp, rpc_pb_RpcEnvelope_file_read_resp_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_ProcessRunAsync, process_run_async, rpc_pb_RpcEnvelope_process_run_async_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_ProcessRunAsyncResponse, process_run_async_resp, rpc_pb_RpcEnvelope_process_run_async_resp_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_ProcessPoll, process_poll, rpc_pb_RpcEnvelope_process_poll_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_ProcessPollResponse, process_poll_resp, rpc_pb_RpcEnvelope_process_poll_resp_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_ProcessKill, process_kill, rpc_pb_RpcEnvelope_process_kill_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_GenericResponse, generic_resp, rpc_pb_RpcEnvelope_generic_resp_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_AckPacket, ack_packet, rpc_pb_RpcEnvelope_ack_packet_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_HandshakeConfig, handshake_config, rpc_pb_RpcEnvelope_handshake_config_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_SetBaudratePacket, set_baudrate_packet, rpc_pb_RpcEnvelope_set_baudrate_packet_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_LinkSync, link_sync, rpc_pb_RpcEnvelope_link_sync_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_EnterBootloader, enter_bootloader, rpc_pb_RpcEnvelope_enter_bootloader_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_SpiTransfer, spi_transfer, rpc_pb_RpcEnvelope_spi_transfer_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_SpiTransferResponse, spi_transfer_resp, rpc_pb_RpcEnvelope_spi_transfer_resp_tag)
DEFN_PAYLOAD_HELPERS(rpc_pb_SpiConfig, spi_config, rpc_pb_RpcEnvelope_spi_config_tag)

#undef DEFN_PAYLOAD_HELPERS

template <typename T>
inline etl::expected<size_t, FrameError> serialize(const T& msg, etl::span<uint8_t> buffer) {
  pb_ostream_t stream = pb_ostream_from_buffer(buffer.data(), buffer.size());
  if (!pb_encode(&stream, rpc::Payload::get_fields<T>(), &msg)) return etl::unexpected<FrameError>(FrameError::MALFORMED);
  return stream.bytes_written;
}

} // namespace Payload

inline size_t serialize_frame(const rpc_pb_RpcEnvelope& env, etl::span<uint8_t> buffer) {
  if (buffer.size() < CRC_TRAILER_SIZE) return 0;
  pb_ostream_t mem_stream = pb_ostream_from_buffer(buffer.data(), buffer.size() - CRC_TRAILER_SIZE);
  if (!pb_encode(&mem_stream, rpc_pb_RpcEnvelope_fields, &env)) return 0;
  const size_t encoded_size = mem_stream.bytes_written;
  const uint32_t crc = checksum::compute(buffer.subspan(0, encoded_size));
  buffer[encoded_size]     = static_cast<uint8_t>(crc & 0xFF);
  buffer[encoded_size + 1] = static_cast<uint8_t>((crc >> 8) & 0xFF);
  buffer[encoded_size + 2] = static_cast<uint8_t>((crc >> 16) & 0xFF);
  buffer[encoded_size + 3] = static_cast<uint8_t>((crc >> 24) & 0xFF);
  return encoded_size + CRC_TRAILER_SIZE;
}

inline etl::expected<rpc_pb_RpcEnvelope, FrameError> parse_frame(etl::span<const uint8_t> buffer) {
  if (buffer.size() < CRC_TRAILER_SIZE + 2U) return etl::unexpected<FrameError>(FrameError::MALFORMED);
  const size_t crc_offset = buffer.size() - CRC_TRAILER_SIZE;
  const uint32_t crc_calc = checksum::compute(buffer.subspan(0, crc_offset));
  const uint32_t crc_received = (static_cast<uint32_t>(buffer[crc_offset])) |
                                (static_cast<uint32_t>(buffer[crc_offset + 1]) << 8) |
                                (static_cast<uint32_t>(buffer[crc_offset + 2]) << 16) |
                                (static_cast<uint32_t>(buffer[crc_offset + 3]) << 24);
  if (crc_received != crc_calc) return etl::unexpected<FrameError>(FrameError::CRC_MISMATCH);
  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  pb_istream_t stream = pb_istream_from_buffer(buffer.data(), crc_offset);
  if (!pb_decode(&stream, rpc_pb_RpcEnvelope_fields, &env)) return etl::unexpected<FrameError>(FrameError::MALFORMED);
  if (env.version != PROTOCOL_VERSION) return etl::unexpected<FrameError>(FrameError::MALFORMED);
  return env;
}

} // namespace rpc
#endif

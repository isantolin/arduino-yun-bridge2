/**
 * @file rpc_frame.h
 * @brief Zero-Wrapper Protobuf Framing (SIL-2).
 *
 * This file implements the wire-format framing using Nanopb directly.
 * All manual wrappers and redundant abstractions have been erradicated.
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
#include <etl/byte_stream.h>
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
/**
 * @brief Computes CRC32 using ETL directly (SIL-2).
 */
inline uint32_t compute(etl::span<const uint8_t> data) {
  return etl::crc32(data.begin(), data.end());
}
}  // namespace checksum

namespace Payload {

template <typename T>
inline void set_envelope_field(rpc_pb_RpcEnvelope& env, const T& packet);

#define DEFN_SET_ENVELOPE_FIELD(type, field, tag) \
template <> \
inline void set_envelope_field<type>(rpc_pb_RpcEnvelope& env, const type& packet) { \
  env.which_payload_type = tag; \
  env.payload_type.field = packet; \
}

DEFN_SET_ENVELOPE_FIELD(rpc_pb_VersionResponse, version_resp, rpc_pb_RpcEnvelope_version_resp_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_FreeMemoryResponse, free_memory_resp, rpc_pb_RpcEnvelope_free_memory_resp_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_Capabilities, capabilities, rpc_pb_RpcEnvelope_capabilities_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_PinMode, pin_mode, rpc_pb_RpcEnvelope_pin_mode_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_DigitalWrite, digital_write, rpc_pb_RpcEnvelope_digital_write_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_AnalogWrite, analog_write, rpc_pb_RpcEnvelope_analog_write_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_PinRead, pin_read, rpc_pb_RpcEnvelope_pin_read_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_DigitalReadResponse, digital_read_resp, rpc_pb_RpcEnvelope_digital_read_resp_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_AnalogReadResponse, analog_read_resp, rpc_pb_RpcEnvelope_analog_read_resp_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_ConsoleWrite, console_write, rpc_pb_RpcEnvelope_console_write_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_DatastorePut, datastore_put, rpc_pb_RpcEnvelope_datastore_put_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_DatastoreGet, datastore_get, rpc_pb_RpcEnvelope_datastore_get_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_DatastoreGetResponse, datastore_get_resp, rpc_pb_RpcEnvelope_datastore_get_resp_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_MailboxPush, mailbox_push, rpc_pb_RpcEnvelope_mailbox_push_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_MailboxProcessed, mailbox_processed, rpc_pb_RpcEnvelope_mailbox_processed_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_MailboxAvailableResponse, mailbox_available_resp, rpc_pb_RpcEnvelope_mailbox_available_resp_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_MailboxReadResponse, mailbox_read_resp, rpc_pb_RpcEnvelope_mailbox_read_resp_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_FileWrite, file_write, rpc_pb_RpcEnvelope_file_write_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_FileRead, file_read, rpc_pb_RpcEnvelope_file_read_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_FileRemove, file_remove, rpc_pb_RpcEnvelope_file_remove_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_FileReadResponse, file_read_resp, rpc_pb_RpcEnvelope_file_read_resp_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_ProcessRunAsync, process_run_async, rpc_pb_RpcEnvelope_process_run_async_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_ProcessRunAsyncResponse, process_run_async_resp, rpc_pb_RpcEnvelope_process_run_async_resp_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_ProcessPoll, process_poll, rpc_pb_RpcEnvelope_process_poll_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_ProcessPollResponse, process_poll_resp, rpc_pb_RpcEnvelope_process_poll_resp_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_ProcessKill, process_kill, rpc_pb_RpcEnvelope_process_kill_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_GenericResponse, generic_resp, rpc_pb_RpcEnvelope_generic_resp_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_AckPacket, ack_packet, rpc_pb_RpcEnvelope_ack_packet_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_HandshakeConfig, handshake_config, rpc_pb_RpcEnvelope_handshake_config_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_SetBaudratePacket, set_baudrate_packet, rpc_pb_RpcEnvelope_set_baudrate_packet_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_LinkSync, link_sync, rpc_pb_RpcEnvelope_link_sync_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_EnterBootloader, enter_bootloader, rpc_pb_RpcEnvelope_enter_bootloader_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_SpiTransfer, spi_transfer, rpc_pb_RpcEnvelope_spi_transfer_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_SpiTransferResponse, spi_transfer_resp, rpc_pb_RpcEnvelope_spi_transfer_resp_tag)
DEFN_SET_ENVELOPE_FIELD(rpc_pb_SpiConfig, spi_config, rpc_pb_RpcEnvelope_spi_config_tag)


/**
 * @brief Decodes a specific payload type from an envelope.
 */
template <typename T>
inline etl::expected<T, FrameError> parse(const rpc_pb_RpcEnvelope& envelope);

#define DEFN_PARSE_SPECIALIZATION(type, field, tag) \
template <> \
inline etl::expected<type, FrameError> parse<type>(const rpc_pb_RpcEnvelope& envelope) { \
  if (envelope.which_payload_type == tag) { \
    return envelope.payload_type.field; \
  } \
  type msg = {}; \
  if (envelope.which_payload_type == rpc_pb_RpcEnvelope_encrypted_payload_tag) { \
    pb_istream_t stream = pb_istream_from_buffer( \
        envelope.payload_type.encrypted_payload.bytes, \
        envelope.payload_type.encrypted_payload.size); \
    if (!pb_decode(&stream, rpc::Payload::get_fields<type>(), &msg)) { \
      return etl::unexpected<FrameError>(FrameError::MALFORMED); \
    } \
    return etl::expected<type, FrameError>(msg); \
  } \
  return etl::unexpected<FrameError>(FrameError::MALFORMED); \
}

DEFN_PARSE_SPECIALIZATION(rpc_pb_VersionResponse, version_resp, rpc_pb_RpcEnvelope_version_resp_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_FreeMemoryResponse, free_memory_resp, rpc_pb_RpcEnvelope_free_memory_resp_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_Capabilities, capabilities, rpc_pb_RpcEnvelope_capabilities_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_PinMode, pin_mode, rpc_pb_RpcEnvelope_pin_mode_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_DigitalWrite, digital_write, rpc_pb_RpcEnvelope_digital_write_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_AnalogWrite, analog_write, rpc_pb_RpcEnvelope_analog_write_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_PinRead, pin_read, rpc_pb_RpcEnvelope_pin_read_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_DigitalReadResponse, digital_read_resp, rpc_pb_RpcEnvelope_digital_read_resp_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_AnalogReadResponse, analog_read_resp, rpc_pb_RpcEnvelope_analog_read_resp_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_ConsoleWrite, console_write, rpc_pb_RpcEnvelope_console_write_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_DatastorePut, datastore_put, rpc_pb_RpcEnvelope_datastore_put_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_DatastoreGet, datastore_get, rpc_pb_RpcEnvelope_datastore_get_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_DatastoreGetResponse, datastore_get_resp, rpc_pb_RpcEnvelope_datastore_get_resp_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_MailboxPush, mailbox_push, rpc_pb_RpcEnvelope_mailbox_push_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_MailboxProcessed, mailbox_processed, rpc_pb_RpcEnvelope_mailbox_processed_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_MailboxAvailableResponse, mailbox_available_resp, rpc_pb_RpcEnvelope_mailbox_available_resp_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_MailboxReadResponse, mailbox_read_resp, rpc_pb_RpcEnvelope_mailbox_read_resp_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_FileWrite, file_write, rpc_pb_RpcEnvelope_file_write_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_FileRead, file_read, rpc_pb_RpcEnvelope_file_read_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_FileRemove, file_remove, rpc_pb_RpcEnvelope_file_remove_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_FileReadResponse, file_read_resp, rpc_pb_RpcEnvelope_file_read_resp_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_ProcessRunAsync, process_run_async, rpc_pb_RpcEnvelope_process_run_async_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_ProcessRunAsyncResponse, process_run_async_resp, rpc_pb_RpcEnvelope_process_run_async_resp_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_ProcessPoll, process_poll, rpc_pb_RpcEnvelope_process_poll_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_ProcessPollResponse, process_poll_resp, rpc_pb_RpcEnvelope_process_poll_resp_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_ProcessKill, process_kill, rpc_pb_RpcEnvelope_process_kill_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_GenericResponse, generic_resp, rpc_pb_RpcEnvelope_generic_resp_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_AckPacket, ack_packet, rpc_pb_RpcEnvelope_ack_packet_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_HandshakeConfig, handshake_config, rpc_pb_RpcEnvelope_handshake_config_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_SetBaudratePacket, set_baudrate_packet, rpc_pb_RpcEnvelope_set_baudrate_packet_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_LinkSync, link_sync, rpc_pb_RpcEnvelope_link_sync_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_EnterBootloader, enter_bootloader, rpc_pb_RpcEnvelope_enter_bootloader_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_SpiTransfer, spi_transfer, rpc_pb_RpcEnvelope_spi_transfer_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_SpiTransferResponse, spi_transfer_resp, rpc_pb_RpcEnvelope_spi_transfer_resp_tag)
DEFN_PARSE_SPECIALIZATION(rpc_pb_SpiConfig, spi_config, rpc_pb_RpcEnvelope_spi_config_tag)


/**
 * @brief Encodes a payload directly to a buffer (Zero-Wrapper).
 */
template <typename T>
inline etl::expected<size_t, FrameError> serialize(const T& msg, etl::span<uint8_t> buffer) {
  pb_ostream_t stream = pb_ostream_from_buffer(buffer.data(), buffer.size());
  if (!pb_encode(&stream, rpc::Payload::get_fields<T>(), &msg)) {
    return etl::unexpected<FrameError>(FrameError::MALFORMED);
  }
  return stream.bytes_written;
}

}  // namespace Payload

/**
 * @brief Serializes an envelope directly to buffer with CRC (Zero-Wrapper).
 */
inline size_t serialize_frame(const rpc_pb_RpcEnvelope& env, etl::span<uint8_t> buffer) {
  if (buffer.size() < CRC_TRAILER_SIZE) return 0;

  pb_ostream_t stream =
      pb_ostream_from_buffer(buffer.data(), buffer.size() - CRC_TRAILER_SIZE);
  if (!pb_encode(&stream, rpc::Payload::get_fields<rpc_pb_RpcEnvelope>(), &env)) return 0;

  const size_t encoded_size = stream.bytes_written;
  const uint32_t crc = checksum::compute(buffer.subspan(0, encoded_size));

  etl::byte_stream_writer writer(buffer.begin() + encoded_size,
                                 CRC_TRAILER_SIZE, etl::endian::little);
  writer.write<uint32_t>(crc);

  return encoded_size + CRC_TRAILER_SIZE;
}

/**
 * @brief Parses a raw buffer into an envelope with CRC validation (Zero-Wrapper).
 */
inline etl::expected<rpc_pb_RpcEnvelope, FrameError> parse_frame(
    etl::span<const uint8_t> buffer) {
  if (buffer.size() < CRC_TRAILER_SIZE + 2U)
    return etl::unexpected<FrameError>(FrameError::MALFORMED);

  const size_t crc_offset = buffer.size() - CRC_TRAILER_SIZE;
  const uint32_t crc_calc = checksum::compute(buffer.subspan(0, crc_offset));

  uint32_t crc_received = 0;
  etl::byte_stream_reader reader(buffer.begin() + crc_offset,
                                 CRC_TRAILER_SIZE, etl::endian::little);
  crc_received = reader.read<uint32_t>().value_or(0U);

  if (crc_received != crc_calc)
    return etl::unexpected<FrameError>(FrameError::CRC_MISMATCH);

  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  pb_istream_t stream = pb_istream_from_buffer(buffer.data(), crc_offset);
  if (!pb_decode(&stream, rpc::Payload::get_fields<rpc_pb_RpcEnvelope>(), &env))
    return etl::unexpected<FrameError>(FrameError::MALFORMED);

  if (env.version != PROTOCOL_VERSION)
    return etl::unexpected<FrameError>(FrameError::MALFORMED);

  return env;
}


}  // namespace rpc

#endif

#include <etl/array.h>
#include <etl/byte_stream.h>
#include <etl/delegate.h>
#include <etl/expected.h>
#include <etl/span.h>
#include <stdint.h>

#include "etl_profile.h"
#include "protocol/rpc_frame.h"

// [SIL-2] Instanciaciones explícitas de plantillas para reducir el crecimiento
// binario (bloat). Esto asegura que estos tipos comunes se compilen exactamente
// una sola vez en el firmware, optimizando drásticamente el espacio en la
// memoria Flash para microcontroladores AVR.

namespace etl {
template class span<uint8_t>;
template class span<const uint8_t>;
template class span<char>;
template class span<const char>;

// Instanciaciones explícitas de arrays estáticos para consolidar la lógica de
// límites en Flash. Corresponden a los tamaños más críticos de buffers
// criptográficos, temporales y de tramas.
template class array<uint8_t, 32U>;
template class array<uint8_t, 64U>;
template class array<uint8_t, 256U>;

// Delegados comunes de callbacks registrados en el patrón de
// suscripción/observador.
template class delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>;
template class delegate<void(const rpc_pb_RpcEnvelope&)>;

// NOTA DE SEGURIDAD (SIL-2): Se ha omitido la instanciación explícita de
// 'expected' debido a una limitación de diseño en las aserciones de la ETL para
// arquitecturas host x86/64. Al instanciarse explícitamente, el compilador g++
// intenta compilar los caminos de error que retornan un puntero nulo
// (ETL_NULLPTR) e inicializar con este una referencia C++, lo cual causa un
// error estándar. La instanciación implícita en 'parse_frame' sigue funcionando
// de forma segura sin este problema.
}  // namespace etl

namespace rpc {

// Helper consolidado y no templatizado para la decodificación y validación de
// tramas seriales. Esto centraliza la lógica con Nanopb reduciendo el overhead
// de la tabla de símbolos.
etl::expected<rpc_pb_RpcEnvelope, FrameError> parse_frame(
    etl::span<const uint8_t> buffer) {
  if (buffer.size() < CRC_TRAILER_SIZE + 2U) {
    return etl::unexpected<FrameError>(FrameError::MALFORMED);
  }

  const size_t crc_offset = buffer.size() - CRC_TRAILER_SIZE;
  const uint32_t crc_calc = checksum::compute(buffer.subspan(0, crc_offset));
  const auto crc_tail = buffer.subspan(crc_offset);

  // Deserialización segura del CRC de cola mediante etl::byte_stream_reader
  etl::byte_stream_reader reader(crc_tail.data(), crc_tail.size(),
                                 etl::endian::little);
  const uint32_t crc_received = reader.read<uint32_t>().value_or(0U);

  if (crc_received != crc_calc) {
    return etl::unexpected<FrameError>(FrameError::CRC_MISMATCH);
  }

  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  pb_istream_t stream = pb_istream_from_buffer(buffer.data(), crc_offset);

  if (!pb_decode(&stream, rpc_pb_RpcEnvelope_fields, &env)) {
    return etl::unexpected<FrameError>(FrameError::MALFORMED);
  }

  if (env.version != PROTOCOL_VERSION) {
    return etl::unexpected<FrameError>(FrameError::MALFORMED);
  }

  return env;
}

}  // namespace rpc

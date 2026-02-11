#ifndef STREAM_SENDER_H
#define STREAM_SENDER_H

#include <stdint.h>
#include <stddef.h>
#include <etl/string.h>
#include <etl/to_string.h>
#include <etl/string_view.h>

namespace util {

/**
 * @brief Template class to provide generic print/write capabilities to bridge services.
 * [SIL-2 COMPLIANT] No dynamic allocation, uses ETL for string conversion.
 */
template <typename TDerived>
class StreamSender {
public:
    size_t print(etl::string_view sv) {
        return static_cast<TDerived*>(this)->write(reinterpret_cast<const uint8_t*>(sv.data()), sv.length());
    }

    size_t print(const char* s) {
        return print(etl::string_view(s));
    }

    template <typename T>
    size_t print(T value) {
        etl::string<32> s;
        etl::to_string(value, s);
        return print(etl::string_view(s));
    }

    size_t println() {
        return print("\n");
    }

    template <typename T>
    size_t println(T value) {
        size_t n = print(value);
        n += println();
        return n;
    }
};

} // namespace util

#endif // STREAM_SENDER_H

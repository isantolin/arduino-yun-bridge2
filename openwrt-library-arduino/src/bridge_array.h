/*
 * This file is part of Arduino Yun Ecosystem v2.
 * (C) 2025 Ignacio Santolin
 */
#ifndef BRIDGE_ARRAY_H
#define BRIDGE_ARRAY_H

#include <stddef.h>

namespace bridge {

/*
 * Implementation of std::array compliant structure for Embedded Systems.
 * STRICT REQUIREMENT: NO STL Dependency (<array> is FORBIDDEN).
 * This ensures compatibility with AVR, SAMD, ESP32 and Linux hosts 
 * without relying on libstdc++.
 */
template<typename T, size_t N>
struct array {
    T _elements[N];

    // --- Element access ---
    
    T& operator[](size_t i) { 
        return _elements[i]; 
    }
    
    const T& operator[](size_t i) const { 
        return _elements[i]; 
    }

    T& at(size_t i) {
        // Safety clamping: unlike std::at, we don't throw exceptions.
        if (i >= N) i = N - 1; 
        return _elements[i];
    }
    
    const T& at(size_t i) const {
        if (i >= N) i = N - 1;
        return _elements[i];
    }

    T* data() { return _elements; }
    const T* data() const { return _elements; }

    // --- Capacity ---
    
    constexpr size_t size() const { return N; }

    // --- Operations ---
    
    void fill(const T& value) {
        for (size_t i = 0; i < N; ++i) {
            _elements[i] = value;
        }
    }

    // --- Iterators ---
    
    T* begin() { return _elements; }
    const T* begin() const { return _elements; }
    
    T* end() { return _elements + N; }
    const T* end() const { return _elements + N; }
};

} // namespace bridge

#endif // BRIDGE_ARRAY_H
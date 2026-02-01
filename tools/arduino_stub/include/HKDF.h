/**
 * @file HKDF.h
 * @brief Arduino Crypto HKDF stub for host-based unit testing.
 * 
 * Mirrors the API from OperatorFoundation/Crypto v0.4.0.
 * This stub provides minimal implementation for compilation only.
 */
#ifndef HKDF_H
#define HKDF_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

// Forward declaration for clean() function (from Crypto.h)
extern void clean(void* buf, size_t len);

class HKDFCommon {
public:
    virtual ~HKDFCommon() {}
    
    void setKey(const void* key, size_t keyLen, const void* salt = 0, size_t saltLen = 0) {
        (void)key; (void)keyLen; (void)salt; (void)saltLen;
    }
    
    void extract(void* out, size_t outLen, const void* info = 0, size_t infoLen = 0) {
        // Stub: zero-fill output for testing
        if (out && outLen > 0) {
            memset(out, 0x42, outLen);  // Deterministic test pattern
        }
        (void)info; (void)infoLen;
    }
    
    void clear() {}

protected:
    HKDFCommon() {}
};

template <typename T>
class HKDF : public HKDFCommon {
public:
    HKDF() {}
    ~HKDF() {}
};

/**
 * @brief Convenience function for one-shot HKDF derivation.
 */
template <typename T>
void hkdf(void* out, size_t outLen, 
          const void* key, size_t keyLen,
          const void* salt, size_t saltLen, 
          const void* info, size_t infoLen) {
    HKDF<T> context;
    context.setKey(key, keyLen, salt, saltLen);
    context.extract(out, outLen, info, infoLen);
}

#endif  // HKDF_H

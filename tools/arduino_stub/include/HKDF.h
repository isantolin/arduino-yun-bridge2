#ifndef HKDF_H
#define HKDF_H

#include <stddef.h>
#include <stdint.h>

template <typename HashType>
class HKDF {
public:
    void extract(const void* salt, size_t saltLen, const void* ikm, size_t ikmLen) {}
    void expand(const void* info, size_t infoLen, void* output, size_t outputLen) {}
};

#endif

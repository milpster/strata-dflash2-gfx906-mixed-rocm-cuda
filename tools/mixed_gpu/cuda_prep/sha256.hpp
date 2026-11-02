// tools/mixed_gpu/cuda_prep/sha256.hpp - SHA-256 for fixture provenance (FIPS 180-4, the public algorithm).
//
// A small self-contained implementation in the new scope so the fixture manifest's hashes need no external
// dependency (no openssl link in a GPU-free test binary).  Provenance use only; not a security primitive here.
#ifndef STRATA_MIXED_GPU_CUDA_PREP_SHA256_HPP
#define STRATA_MIXED_GPU_CUDA_PREP_SHA256_HPP

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace strata::prep {

class Sha256 {
  public:
    Sha256();
    void update(const void* data, size_t len);
    std::string hex();  // finishes the digest, 64 lowercase hex characters

  private:
    uint32_t h_[8];
    uint64_t len_ = 0;
    uint8_t buf_[64] = {};
    size_t fill_ = 0;
    bool done_ = false;
    std::string hex_;
};

// The hex SHA-256 of a whole file; false when the file cannot be read.
bool sha256_file(const std::string& path, std::string& hex);

// FIPS 180-4 / RFC 6234 test vectors, checked on the spot (whole-buffer and streamed updates).
// True only when every vector matches; `report` gets one line per vector.  Every tool that writes or
// verifies a manifest calls this BEFORE hashing anything - a broken constant table fails loudly there
// (the first version of sha256.cpp had a mistyped K table: see the history note in sha256.cpp).
bool sha256_selftest(std::string& report);

}  // namespace strata::prep

#endif  // STRATA_MIXED_GPU_CUDA_PREP_SHA256_HPP

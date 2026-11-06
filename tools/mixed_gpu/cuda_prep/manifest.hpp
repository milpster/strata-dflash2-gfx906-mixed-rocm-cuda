// tools/mixed_gpu/cuda_prep/manifest.hpp - the fixture manifest format for the PREPARATION ABI v1 assets.
//
// A manifest is one fixture's self-description: the layout, the seed, SHA-256 provenance for every recorded
// file, and the SYNTHETIC flag.  A fixture with synthetic=1 is a ggml-quantized random-weight blob: parity
// against it proves the pipeline, NOT a real-blob test.  A real blob (a GGUF slice from a pack, written by
// tools/iq_pack.py) is recorded with synthetic=0 plus its provenance, and is exercised only in an explicitly
// authorized runtime window - the future file-input path for real-blob parity.
//
// The parser is shared: the CPU guard test (abi_guard_test.cpp) and the compile-only parity driver
// (cuda_prep_check.cpp) parse the same format, and the driver runs only in the runtime window.
#ifndef STRATA_MIXED_GPU_CUDA_PREP_MANIFEST_HPP
#define STRATA_MIXED_GPU_CUDA_PREP_MANIFEST_HPP

#include "abi.hpp"

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace strata::prep {

struct Manifest {
    int version = 0;
    std::string label;
    int synthetic = -1;  // 1 = the synthetic fixture this phase ships; 0 = a real blob (later window)
    long long seed = -1;
    int gu_type = -1, d_type = -1;
    long long n_embd = 0, n_ff = 0;
    long long nt = 0, k = 0;
    unsigned long long blob_bytes = 0, gu_row = 0, d_row = 0, up_off = 0, down_off = 0, tail_off = 0;
    std::map<std::string, std::string> sha256;  // "sha256_<file>" -> hex
    std::string provenance;                       // free-form source note (the ggml checkout identity)
};

// Parse a manifest file.  Refuses: a missing/unknown version, missing required keys, non-integer values,
// a type outside the bounded v1 set, nt != 1, k outside 1..15, a layout that disagrees with the derived
// geometry, blob_bytes that disagree.  Returns false with a reason; the caller must not touch any file it
// refused to describe.
bool manifest_parse(const std::string& path, Manifest& m, std::string& err);

// The required recorded files for a synthetic fixture (their names, for the SHA-256 checks).
std::vector<std::string> manifest_files();

}  // namespace strata::prep

#endif  // STRATA_MIXED_GPU_CUDA_PREP_MANIFEST_HPP

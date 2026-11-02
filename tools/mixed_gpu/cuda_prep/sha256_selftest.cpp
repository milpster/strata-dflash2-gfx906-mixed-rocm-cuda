// tools/mixed_gpu/cuda_prep/sha256_selftest.cpp - CPU-only SHA-256 selftest runner (R1).
//
// Runs the published FIPS 180-4 / RFC 6234 vectors through the same sha256.cpp the fixture exporters use,
// prints one line per vector and exits 0 (all match) / 1 (any mismatch).  Plain host code: no ggml, no GPU
// runtime, no dlopen - it is the CPU-safe way to prove the provenance hashes before trusting a manifest.
#include "sha256.hpp"

#include <cstdio>
#include <string>

int main() {
    std::string report;
    const bool ok = strata::prep::sha256_selftest(report);
    std::fputs(report.c_str(), stdout);
    std::printf("sha256_selftest: %s\n", ok ? "PASS (all published vectors match sha256sum)" : "FAIL");
    return ok ? 0 : 1;
}

// tools/mixed_gpu/cuda_prep/manifest.cpp - the manifest parser (CPU-side, host-only, no GPU runtime).
#include "manifest.hpp"

#include "abi_validate.hpp"

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <sstream>

namespace strata::prep {

namespace {

bool parse_ll(const std::string& s, long long& out) {
    if (s.empty()) return false;
    size_t i = 0;
    if (s[0] == '-' || s[0] == '+') i = 1;
    if (i >= s.size()) return false;
    for (; i < s.size(); ++i)
        if (!std::isdigit((unsigned char) s[i])) return false;
    errno = 0;
    out = std::strtoll(s.c_str(), nullptr, 10);
    return errno == 0;
}

bool parse_u64(const std::string& s, unsigned long long& out) {
    if (s.empty()) return false;
    for (char c : s)
        if (!std::isdigit((unsigned char) c)) return false;
    out = 0;
    for (char c : s) {
        const unsigned digit = (unsigned) (c - '0');
        if (out > (0xFFFFFFFFFFFFFFFFull - digit) / 10) return false;  // overflow: refuse
        out = out * 10 + digit;
    }
    return true;
}

std::string trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && std::isspace((unsigned char) s[a])) ++a;
    while (b > a && std::isspace((unsigned char) s[b - 1])) --b;
    return s.substr(a, b - a);
}

bool need(const std::map<std::string, std::string>& kv, const char* key, std::string& out, std::string& err) {
    const auto it = kv.find(key);
    if (it == kv.end()) {
        err = std::string("manifest: missing key '") + key + "'";
        return false;
    }
    out = it->second;
    return true;
}

}  // namespace

std::vector<std::string> manifest_files() {
    return {"blob.bin",   "x.bin",        "ref_f32.bin",       "cpu_out.bin",
            "combine_parts.bin", "combine_weights.bin", "combine_shared.bin", "combine_oracle.bin"};
}

bool manifest_parse(const std::string& path, Manifest& m, std::string& err) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        err = "manifest: cannot open " + path;
        return false;
    }
    std::map<std::string, std::string> kv;
    std::string line;
    while (std::getline(in, line)) {
        line = trim(line);
        if (line.empty() || line[0] == '#') continue;
        const size_t eq = line.find('=');
        if (eq == std::string::npos) {
            err = "manifest: malformed line (no '='): " + line;
            return false;
        }
        kv[trim(line.substr(0, eq))] = trim(line.substr(eq + 1));
    }

    std::string v;
    if (!need(kv, "version", v, err)) return false;
    long long vv = 0;
    if (!parse_ll(v, vv)) {
        err = "manifest: version is not an integer";
        return false;
    }
    if (vv != 1) {
        err = "manifest: version " + v + " is not 1 (a newer/older manifest this build must not read)";
        return false;
    }
    m.version = 1;

    for (const char* key : {"label", "synthetic", "seed", "gu_type", "d_type", "n_embd", "n_ff", "nt", "k",
                            "blob_bytes"}) {
        std::string tmp;
        if (!need(kv, key, tmp, err)) return false;
    }
    if (!(need(kv, "label", m.label, err))) return false;
    long long syn = -1;
    if (!parse_ll(kv["synthetic"], syn) || (syn != 0 && syn != 1)) {
        err = "manifest: synthetic must be 0 (a real blob) or 1 (the ggml-quantized fixture)";
        return false;
    }
    m.synthetic = (int) syn;
    long long ll = 0;
    if (!parse_ll(kv["seed"], ll)) {
        err = "manifest: seed is not an integer";
        return false;
    }
    m.seed = ll;
    long long ll_gu = 0, ll_d = 0;
    if (!parse_ll(kv["gu_type"], ll_gu) || !parse_ll(kv["d_type"], ll_d)) {
        err = "manifest: gu_type/d_type are not integers";
        return false;
    }
    m.gu_type = (int) ll_gu;
    m.d_type = (int) ll_d;
    if (!parse_ll(kv["n_embd"], m.n_embd) || !parse_ll(kv["n_ff"], m.n_ff)) {
        err = "manifest: n_embd/n_ff are not integers";
        return false;
    }
    if (!parse_ll(kv["nt"], m.nt) || m.nt != 1) {
        err = "manifest: nt must be 1 (v1 is decode-only, one token)";
        return false;
    }
    if (!parse_ll(kv["k"], m.k) || m.k < 1 || m.k > STRATA_PREP_V1_MAX_K) {
        err = "manifest: k must be 1..15 (the pinned native_moe_combine contract)";
        return false;
    }
    if (!parse_u64(kv["blob_bytes"], m.blob_bytes)) {
        err = "manifest: blob_bytes is not a non-negative integer (overflow refused)";
        return false;
    }

    // The layout keys are required for a recorded blob: the manifest must describe the derived geometry, and
    // a disagreement is a refusal (the driver trusts nothing it was told).
    const char* layout_keys[] = {"gu_row", "d_row", "up_off", "down_off", "tail_off"};
    for (const char* key : layout_keys) {
        std::string tmp;
        if (!need(kv, key, tmp, err)) return false;
        unsigned long long u = 0;
        if (!parse_u64(tmp, u)) {
            err = std::string("manifest: ") + key + " is not a non-negative integer";
            return false;
        }
        if (key == std::string("gu_row")) m.gu_row = u;
        if (key == std::string("d_row")) m.d_row = u;
        if (key == std::string("up_off")) m.up_off = u;
        if (key == std::string("down_off")) m.down_off = u;
        if (key == std::string("tail_off")) m.tail_off = u;
    }
    if (m.tail_off != 0) {
        err = "manifest: tail_off must be 0 in v1 (no NVFP4/Hadamard fixtures)";
        return false;
    }

    PrepV1Layout L;
    const int rc = derive_layout(m.gu_type, m.d_type, m.n_embd, m.n_ff, L, err);
    if (rc) {
        // Truthful reason for the refusal: the type set, the role rules and the geometry gate are different
        // refusals (R1).  Do not describe a dimension refusal as a type refusal.
        err += rc == STRATA_PREP_V1_E_DIM ? " (manifest dimensions do not give whole blocks for these types)"
                                          : " (manifest types/roles outside the bounded v1 set)";
        return false;
    }
    if (m.blob_bytes != L.bytes || m.gu_row != L.gu_row || m.d_row != L.d_row || m.up_off != L.up_off ||
        m.down_off != L.down_off) {
        std::fprintf(stderr,
                     "[manifest-geometry-diff] gu=%d d=%d n_embd=%lld n_ff=%lld :: declared blob=%llu gu_row=%llu d_row=%llu "
                     "up=%llu down=%llu vs derived blob=%llu gu_row=%llu d_row=%llu up=%llu down=%llu\n",
                     m.gu_type, m.d_type, (long long) m.n_embd, (long long) m.n_ff,
                     (unsigned long long) m.blob_bytes, (unsigned long long) m.gu_row, (unsigned long long) m.d_row,
                     (unsigned long long) m.up_off, (unsigned long long) m.down_off, (unsigned long long) L.bytes,
                     (unsigned long long) L.gu_row, (unsigned long long) L.d_row, (unsigned long long) L.up_off,
                     (unsigned long long) L.down_off);
        err = "manifest: the declared layout disagrees with the derived geometry";
        return false;
    }

    for (const auto& [key, val] : kv)
        if (key.rfind("sha256_", 0) == 0) m.sha256[key.substr(7)] = val;
    for (const std::string& file : manifest_files()) {
        if (m.sha256.count(file) == 0) {
            err = "manifest: missing sha256_" + file;
            return false;
        }
        if (m.sha256[file].size() != 64) {
            err = "manifest: sha256_" + file + " is not 64 hex characters";
            return false;
        }
    }
    auto prov = kv.find("ggml");
    m.provenance = prov != kv.end() ? prov->second : "";
    return true;
}

}  // namespace strata::prep

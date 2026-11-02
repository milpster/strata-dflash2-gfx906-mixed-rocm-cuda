// tools/mixed_gpu/cuda_prep/sha256.cpp - the FIPS 180-4 SHA-256 (public algorithm) for fixture provenance.
//
// HISTORY (why this file was rewritten): the first version of this file carried a hand-typed K table with two
// defects - K[17] = 0xefbe4787 (FIPS: 0xefbe4786, off by one) and a shifted tail K[47..63] that dropped the
// three real round constants 0x106aa070 / 0x391c0cb3 / 0x5b9cca4f and used 0x1e376c08 / 0x5b96a391 /
// 0x69596b03 at those places.  Every digest it produced was wrong (SHA-256("abc") came out
// b9e8abd8... instead of ba7816bf...), so the manifests written with it did not match `sha256sum`.  The table
// below is FIPS 180-4 / RFC 6234, cross-checked two ways: against the published constant list and against
// floor(2^32 * frac(cbrt(p))) for the first 64 primes computed in exact integer arithmetic (p=61 ->
// 0xefbe4786, p=227 -> 0x19a4c116, p=317 -> 0xd186b8c7).  sha256_selftest() runs the published test vectors
// and every exporter/test calls it before hashing anything, so a broken table can never write a manifest again.
#include "sha256.hpp"

#include <cstdio>
#include <cstring>
#include <fstream>

namespace strata::prep {
namespace {

// Round constants: first 32 bits of the fractional parts of the cube roots of the first 64 primes 2..311.
const uint32_t K[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
    0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
    0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
    0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
    0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u, 0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u,
};

// Initial hash values: first 32 bits of the fractional parts of the square roots of the first 8 primes 2..19.
const uint32_t H0[8] = {0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
                        0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u};

inline uint32_t rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

void compress(uint32_t h[8], const uint8_t b[64]) {
    uint32_t w[64];
    for (int i = 0; i < 16; ++i)
        w[i] = ((uint32_t) b[4 * i] << 24) | ((uint32_t) b[4 * i + 1] << 16) | ((uint32_t) b[4 * i + 2] << 8) |
               (uint32_t) b[4 * i + 3];
    for (int i = 16; i < 64; ++i) {
        const uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
        const uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = h[0], bb = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (int i = 0; i < 64; ++i) {
        const uint32_t S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        const uint32_t ch = (e & f) ^ ((~e) & g);
        const uint32_t t1 = hh + S1 + ch + K[i] + w[i];
        const uint32_t S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        const uint32_t maj = (a & bb) ^ (a & c) ^ (bb & c);
        const uint32_t t2 = S0 + maj;
        hh = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = bb;
        bb = a;
        a = t1 + t2;
    }
    h[0] += a;
    h[1] += bb;
    h[2] += c;
    h[3] += d;
    h[4] += e;
    h[5] += f;
    h[6] += g;
    h[7] += hh;
}

}  // namespace

Sha256::Sha256() {
    for (int i = 0; i < 8; ++i) h_[i] = H0[i];
}

void Sha256::update(const void* data, size_t len) {
    const uint8_t* p = (const uint8_t*) data;
    len_ += (uint64_t) len;
    while (len) {
        const size_t take = (len < 64 - fill_) ? len : 64 - fill_;
        std::memcpy(buf_ + fill_, p, take);
        fill_ += take;
        p += take;
        len -= take;
        if (fill_ == 64) {
            compress(h_, buf_);
            fill_ = 0;
        }
    }
}

std::string Sha256::hex() {
    if (done_) return hex_;
    done_ = true;
    const uint64_t bits = len_ * 8;
    const uint8_t one = 0x80;
    update(&one, 1);
    uint8_t z = 0;
    while (fill_ != 56) update(&z, 1);  // zeros up to the 56-byte boundary of a block
    uint8_t len_be[8];
    for (int i = 0; i < 8; ++i) len_be[i] = (uint8_t) (bits >> (56 - 8 * i));
    update(len_be, 8);
    if (fill_ != 0) return hex_;  // impossible for a correct padding; never emit a partial digest
    static const char* hexd = "0123456789abcdef";
    for (int i = 0; i < 8; ++i)
        for (int k = 3; k >= 0; --k) {
            hex_.push_back(hexd[(h_[i] >> (8 * k + 4)) & 0xf]);
            hex_.push_back(hexd[(h_[i] >> (8 * k)) & 0xf]);
        }
    return hex_;
}

bool sha256_file(const std::string& path, std::string& hex) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return false;
    Sha256 s;
    char buf[65536];
    while (in) {
        in.read(buf, sizeof buf);
        const std::streamsize got = in.gcount();
        if (got > 0) s.update(buf, (size_t) got);
    }
    hex = s.hex();
    return !hex.empty() && hex.size() == 64;
}

namespace {

std::string hex_of(const std::string& s) {
    Sha256 h;
    h.update(s.data(), s.size());
    return h.hex();
}

std::string hex_of_chunked(const std::string& s, size_t chunk) {
    Sha256 h;
    for (size_t off = 0; off < s.size(); off += chunk) {
        const size_t n = (s.size() - off < chunk) ? s.size() - off : chunk;
        h.update(s.data() + off, n);
    }
    return h.hex();
}

std::string repeat(char c, size_t n) { return std::string(n, c); }

}  // namespace

bool sha256_selftest(std::string& report) {
    // The published vectors (FIPS 180-4 / RFC 6234) plus the boundary lengths that the padding
    // {55,56,57,63,64,65,112,119,120,121,128} exercises, cross-checked against sha256sum(1) on this machine.
    struct Vec {
        const char* name;
        std::string data;
        const char* want;
    };
    const std::vector<Vec> v = {
        {"empty", "", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
        {"abc", "abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"},
        {"abc-80", "abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmnhijklmnoijklmnopjklmnopqklmnopqrlmnopqrsmnopqrstnopqrstu",
         "cf5b16a778af8380036ce59e7b0492370b249b11e8f07a51afac45037afee9d1"},
        {"a-55", repeat('a', 55), "9f4390f8d30c2dd92ec9f095b65e2b9ae9b0a925a5258e241c9f1e910f734318"},
        {"a-56", repeat('a', 56), "b35439a4ac6f0948b6d6f9e3c6af0f5f590ce20f1bde7090ef7970686ec6738a"},
        {"a-57", repeat('a', 57), "f13b2d724659eb3bf47f2dd6af1accc87b81f09f59f2b75e5c0bed6589dfe8c6"},
        {"a-63", repeat('a', 63), "7d3e74a05d7db15bce4ad9ec0658ea98e3f06eeecf16b4c6fff2da457ddc2f34"},
        {"a-64", repeat('a', 64), "ffe054fe7ae0cb6dc65c3af9b61d5209f439851db43d0ba5997337df154668eb"},
        {"a-65", repeat('a', 65), "635361c48bb9eab14198e76ea8ab7f1a41685d6ad62aa9146d301d4f17eb0ae0"},
        {"a-112", repeat('a', 112), "f54353008a2553262ecdc4a34749563ba0950e8b0fc8652780b0a614b99683c1"},
        {"a-119", repeat('a', 119), "31eba51c313a5c08226adf18d4a359cfdfd8d2e816b13f4af952f7ea6584dcfb"},
        {"a-120", repeat('a', 120), "2f3d335432c70b580af0e8e1b3674a7c020d683aa5f73aaaedfdc55af904c21c"},
        {"a-121", repeat('a', 121), "e9615320128cc7a3d6078e9af05603188e5ccbf0d07d8b735d3df5e8e0c1281f"},
        {"a-128", repeat('a', 128), "6836cf13bac400e9105071cd6af47084dfacad4e5e302c94bfed24e013afb73e"},
        {"a-1000000", repeat('a', 1000000), "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"},
    };
    bool ok = true;
    report.clear();
    for (const Vec& t : v) {
        const std::string whole = hex_of(t.data);
        const std::string chunked = hex_of_chunked(t.data, 7);  // the streaming path, 7-byte updates
        const bool pass = whole == t.want && chunked == t.want;
        if (!pass) ok = false;
        char line[256];
        std::snprintf(line, sizeof line, "sha256 selftest %-10s len=%-7zu whole=%s chunked=%s  %s\n", t.name,
                      t.data.size(), (whole == t.want ? "match" : whole.c_str()),
                      (chunked == t.want ? "match" : chunked.c_str()), (pass ? "PASS" : "FAIL"));
        report += line;
    }
    return ok;
}

}  // namespace strata::prep

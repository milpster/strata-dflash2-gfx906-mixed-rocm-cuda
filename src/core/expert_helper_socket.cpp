// src/core/expert_helper_socket.cpp - the engine's expert-helper client (CPU-only, see the header).
//
// No CUDA/HIP header is included and none is needed: this file's I/O is the shared frame transport.
#include "strata/core/expert_helper_socket.hpp"

#include "strata/core/expert_helper_protocol.hpp"
#include "strata/core/finite_rows.hpp"   // helper-ipc-diet: the AVX2 twin of the reply's finiteness pass

#include "ggml.h"   // the fp16 contract of the q8_1 activation blocks the proxy sends (CPU-safe, no ggml compute)

#include <cstdlib>   // std::getenv (the Q8_K wire's opt-in)

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdio>   // H12: the timeout-close observability line (the missed deadline, logged before the close)
#include <cstring>
#include <map>
#include <mutex>
#include <set>
#include <thread>
#include <unistd.h>   // ::close of the adopted descriptor (AF_UNIX transport; Linux's socket API)

#include <sys/mman.h>   // the prompt tier's OWN pinned activation buffer (mmap + mlock; no device call)

namespace strata::core {
namespace {
// The client's deadlines are RELATIVE (the engine's --expert-helper-timeout-ms is "ms from
// now"); the transport takes ABSOLUTE stamps.  Every call below converts with deadline_after.
constexpr int64_t kDeadlineDefaultMs = 3000;
// M7: the drafter lane's own default.  A kDraftCtx frame carries up to kMaxDraftCtxRows rows
// (~100 MB at the published geometry) and a kDraftReq waits on the helper's whole 5-layer forward
// SYNCHRONOUSLY - the window's 20 ms budget (tuned for replies computed under the CPU pool) does
// not describe either.  The engine overrides with --expert-helper-timeout-ms-derived values
// (RemoteExperts::draft_*); this is the floor of a call that passed no positive timeout.
constexpr int64_t kDeadlineDraftMs = 5000;
int64_t rel(int64_t ms, int64_t dflt) { return expert_helper::deadline_after(ms > 0 ? ms : dflt); }
}
}  // namespace

// The STRATA_CLAIM_BATCH knob (default 1 = EXACTLY today's wire), declared here and read by the
// hello latch and the prompt tier's begin path.  NOT cached: the value is read per client and per
// begin, so a claim loop cannot mix a queued part built under one K with a batch built under
// another within one process (the host test sets the env between handshakes; the engine sets it
// once at startup).  Values above kMaxClaimBatch clamp (the frame's own part cap); a non-numeric or
// non-positive reading is 1.
static int64_t claim_batch_env() {
    const char* v = std::getenv("STRATA_CLAIM_BATCH");
    if (v == nullptr) return 1;
    const long long n = std::atoll(v);
    if (n < 1) return 1;
    return n > (long long) strata::core::expert_helper::kMaxClaimBatch
               ? (int64_t) strata::core::expert_helper::kMaxClaimBatch
               : (int64_t) n;
}

namespace strata::core {

int validate_rows_payload(const uint8_t* payload, size_t n, int64_t n_embd,
                          const std::vector<int32_t>& claims, float* out, int32_t& rows) {
    rows = 0;
    if (n < sizeof(expert_helper::RowsResp)) return expert_helper::kErrShort;
    expert_helper::RowsResp h;
    std::memcpy(&h, payload, sizeof(h));
    if (h.n_rows < 0 || h.reserved0 != 0) return expert_helper::kErrShort;
    if ((int64_t) h.n_rows > (int64_t) claims.size()) return expert_helper::kErrShort;   // more rows than claims
    const size_t row_bytes = (size_t) (4 + 4 * (size_t) n_embd);
    if (n != sizeof(h) + (size_t) h.n_rows * row_bytes) return expert_helper::kErrShort;  // length must be exact
    const uint8_t* p = payload + sizeof(h);
    // helper-ipc-diet: ONE sorted index of the claims, then a binary search per row.  The pre-fix
    // code compared every returned row against every claim - 2330 rows x 2330 claims = 5.4 M
    // comparisons per kRows reply, the engine-side twin of the wire cost the bundle attacks.  The
    // answers are identical: an index is claimed iff the binary search finds it; the slot the seen
    // flag occupies is the index's position in the sorted list (unique per claim), so duplicates,
    // unclaimed indices and the missing-claim sweep reject exactly what the scan rejected.
    std::vector<int32_t> sorted(claims.begin(), claims.end());
    std::sort(sorted.begin(), sorted.end());
    std::vector<uint8_t> seen(sorted.size(), 0);
    // Validate EVERYTHING first: one bad index, one duplicate, one missing claim, one non-finite
    // value, and no row is applied - the dispatch's CPU recompute owns the whole claimed set then.
    for (int32_t i = 0; i < h.n_rows; ++i) {
        int32_t idx = 0;
        std::memcpy(&idx, p + (size_t) i * row_bytes, 4);
        if (idx < 0) return expert_helper::kErrBadId;
        const auto slot = std::lower_bound(sorted.begin(), sorted.end(), idx);
        if (slot == sorted.end() || *slot != idx) return expert_helper::kErrBadId;   // not a claimed entry
        const size_t s = (size_t) (slot - sorted.begin());
        if (seen[s]) return expert_helper::kErrBadId;                                // a row the engine would add twice
        const float* row = (const float*) (p + (size_t) i * row_bytes + 4);
        if (!expert_helper::all_finite(row, (size_t) n_embd)) return expert_helper::kErrNonFinite;
        seen[s] = 1;
    }
    for (size_t c = 0; c < sorted.size(); ++c)
        if (!seen[c]) return expert_helper::kErrMissing;
    for (int32_t i = 0; i < h.n_rows; ++i) {
        int32_t idx = 0;
        std::memcpy(&idx, p + (size_t) i * row_bytes, 4);
        std::memcpy(out + (size_t) idx * (size_t) n_embd, p + (size_t) i * row_bytes + 4, (size_t) 4 * (size_t) n_embd);
    }
    rows = h.n_rows;
    return expert_helper::kOk;
}

/// R2 proxy: the engine-side q8_1 quantizer of the window's activations (the kBegin payload).
///
/// PROVEN bit-identical to what `quantize_q8_1_kernel`/`q8_1_store` (src/kernels/cuda/iq_kernels.cu:2712)
/// writes for the same input values: `amax = max|x|` over the 32-value block (max is associative and
/// commutative, so the device's reduction ORDER cannot change it), `d = amax/127` with the same #606
/// clamp as include/strata/kernels/q8_1_finite.hpp:26-28, and `q = roundf(x/d)` clamped to +-127 -
/// `q` depends on `d` alone, so an identical `d` yields identical quantized bytes.
///
/// NOT proven, and not claimed: the fp16 `sum` field of `ds`.  The CUDA path reduces the block's sum with
/// the butterfly `sum += __shfl_xor_sync(sum, o)` for o = 16,8,4,2,1, a hypercube tree
/// (((x0+x16)+(x8+x24))+((x4+x20)+(x12+x28)))+..., while the loop below accumulates x0+x1+x2+...+x31.
/// IEEE-754 addition is NOT associative, so the two fp32 sums differ by a few ULP; after the fp16 store
/// of the sum they usually land on the same half, and can flip by one fp16 ULP near a rounding boundary.
/// The acceptance test is therefore the helper's ROWS on hardware against the same seed with no helper
/// (docs/MIXED_GPU_HELPER.md, hardware list item 4): ULP-scale deltas, not bit equality of this field.
/// fp16 uses ggml's CPU contract, the same round-to-nearest-even the device's `__float2half` performs for
/// the normal range (subnormal/edge scales are not compared here).
///
/// Lives with the proxy client (not in the CUDA proxy's own TU) so the host test pins it with no device
/// in sight: tests/core/expert_helper_socket_test.cpp pins exact golden bytes.  Those pins are a
/// REGRESSION pin of this function - they do not claim device bit-parity, which hardware measures.
void quantize_q8_1_cpu(const float* x, int64_t n, uint8_t* y) {
    // The same constant and the same clamp as include/strata/kernels/q8_1_finite.hpp #606 (that header
    // carries CUDA's __device__ helpers; this file compiles as host C++, so the rule is restated here
    // with the same bits: |v| > 65504 -> +-65504).
    constexpr float kHalfMax = 65504.0f;
    auto finite = [](float v) { return std::fabs(v) > kHalfMax ? std::copysign(kHalfMax, v) : v; };
    for (int64_t ib = 0, nb = n / 32; ib < nb; ++ib) {
        const float* xb = x + ib * 32;
        float amax = 0.0f, sum = 0.0f;
        for (int i = 0; i < 32; ++i) {
            amax = std::fmax(amax, std::fabs(xb[i]));
            sum += xb[i];
        }
        const float d = finite(amax / 127.0f);
        ggml_fp16_t ds[2];
        ds[0] = ggml_fp32_to_fp16(finite(d));
        ds[1] = ggml_fp32_to_fp16(finite(sum));
        int8_t* qs = (int8_t*) (y + ib * 36 + 4);
        for (int i = 0; i < 32; ++i) {
            int8_t q;
            if (amax == 0.0f) q = 0;
            else {
                const float qf = std::roundf(xb[i] / d);
                q = (int8_t) (qf > 127.0f ? 127.0f : qf < -127.0f ? -127.0f : qf);
            }
            qs[i] = q;
        }
        std::memcpy(y + ib * 36, ds, 4);
    }
}

// ---- The prompt tier's Q8_K row quantizer (the kBeginP image_format 2 payload). ----
//
// ggml's scalar reference `quantize_row_q8_K_ref` (ggml-quants.c) semantics, restated with the
// same bits so the frame's image is byte-for-byte the CPU reference's own gate/up activation:
// per 256 values, amax/max with strict `>` (the first maximum wins); the all-zero block writes
// d = 0 and zeroed qs; otherwise `iscale = -127/max` (fp32), `v = nearest_int(iscale * x)` with
// ggml's magic-number round-to-nearest, `qs = MIN(127, v)`, `d = 1/iscale` (the RECIPROCAL, not
// -max/127 - a different rounding), and 16 int16 bsums (the sums of each 16 qs).  ggml's
// reference leaves the zero block's bsums unwritten; this twin zeroes them: the Q8_K gate/up
// kernel reads only d and qs of an activation block (native_gu_q8k_kernel), so the bytes it
// consumes are identical either way and the zeroed tail is deterministic on the wire.  The
// producer the CPU-offloaded rows themselves run is native_quant_act, whose AVX2/AVX1 Q8_K
// writers are ggml-byte-identical by their own pins - so this twin and the reference agree with
// them on every value.  The acceptance test is the same rule as quantize_q8_1_cpu's above: the
// helper's rows on hardware, with the golden vectors in tests/core/expert_helper_socket_test.cpp
// as a regression pin of THIS code.
void quantize_row_q8k_cpu(const float* x, int64_t n, uint8_t* y) {
    auto nearest_int = [](float fval) {   // ggml's magic-number round-to-nearest, same bits
        const float val = fval + 12582912.0f;
        int32_t i;
        std::memcpy(&i, &val, sizeof(i));
        return (i & 0x007fffff) - 0x00400000;
    };
    constexpr int64_t kQK = 256;   // ggml's QK_K
    for (int64_t ib = 0, nb = n / kQK; ib < nb; ++ib) {
        const float* xb = x + ib * kQK;
        uint8_t* yb = y + ib * 292;   // sizeof(block_q8_K): fp32 d + qs[256] + 16 int16 bsums
        float mx = 0.0f, amax = 0.0f;
        for (int j = 0; j < kQK; ++j) {
            const float ax = std::fabs(xb[j]);
            if (ax > amax) { amax = ax; mx = xb[j]; }
        }
        float* d = (float*) (void*) yb;
        int8_t* qs = (int8_t*) (yb + 4);
        int16_t* bsums = (int16_t*) (void*) (yb + 4 + kQK);
        if (amax == 0.0f) {
            *d = 0.0f;
            std::memset(qs, 0, (size_t) kQK);
            std::memset(bsums, 0, 16 * sizeof(int16_t));
            continue;
        }
        const float iscale = -127.0f / mx;
        for (int j = 0; j < kQK; ++j) {
            const int v = nearest_int(iscale * xb[j]);
            qs[j] = (int8_t) (v < 127 ? v : 127);
        }
        for (int j = 0; j < kQK / 16; ++j) {
            int sum = 0;
            for (int ii = 0; ii < 16; ++ii) sum += qs[j * 16 + ii];
            bsums[j] = (int16_t) sum;
        }
        *d = 1.0f / iscale;
    }
}

// ---- R5 P1: the selected-token PROMPT tier's library (see the header). ----

int select_prompt_rows(const int32_t* cnt, const int32_t* toks, const int32_t* host_res, int64_t layer,
                       const uint8_t* holds, const uint8_t* stable, int64_t n_expert, int64_t k,
                       int64_t max_rows_per_expert, int64_t max_rows_per_layer, int64_t max_sel_tok,
                       ExpertHelperPromptPlan& out) {
    out.clear();
    using namespace expert_helper;
    if (k != kK) return kErrGeometry;                          // the pack's routing width, not a parameter
    if (n_expert <= 0 || n_expert > kMaxPairs) return kErrGeometry;
    if (cnt == nullptr || toks == nullptr) return kErrGeometry;
    if (max_rows_per_expert <= 0 || max_rows_per_layer <= 0 || max_sel_tok <= 0) return kErrGeometry;

    // The per-expert list offsets (the concatenated shape: expert e owns toks[off[e] .. +cnt[e]).
    std::vector<int64_t> off((size_t) n_expert + 1, 0);
    for (int64_t e = 0; e < n_expert; ++e) {
        if (cnt[e] < 0) return kErrGeometry;                   // a negative count is not data
        off[(size_t) e + 1] = off[(size_t) e] + (int64_t) cnt[e];
    }
    if (off[(size_t) n_expert] > kMaxPairs) return kErrGeometry;   // more pairs than any frame can carry

    // The eligibility screen, exactly the design's conjunction.  cnt[e] > max_rows_per_expert is
    // SKIPPED (the design's gate): an expert whose fan-out exceeds the cap never enters the tier.
    std::vector<int64_t> cand;
    for (int64_t e = 0; e < n_expert; ++e) {
        if (cnt[e] == 0) continue;
        if (host_res != nullptr && host_res[(size_t) layer * (size_t) n_expert + (size_t) e] >= 0) continue;
        if (holds != nullptr && !holds[(size_t) e]) continue;
        if (stable != nullptr && !stable[(size_t) e]) continue;
        if ((int64_t) cnt[e] > max_rows_per_expert) continue;
        cand.push_back(e);
    }
    // The deterministic order: count DESC, expert ASC (the CPU share's sorted-by-count shape), so the
    // same input always yields the same plan.
    std::sort(cand.begin(), cand.end(), [&](int64_t a, int64_t b) {
        if (cnt[a] != cnt[b]) return cnt[a] > cnt[b];
        return a < b;
    });

    std::set<int32_t> used;
    int64_t rows = 0;
    for (const int64_t e : cand) {
        const int64_t c = (int64_t) cnt[e];
        if (rows + c > max_rows_per_layer) continue;   // the Σ cap: skip this expert, keep scanning
        int64_t fresh = 0;
        for (int64_t i = 0; i < c; ++i) {
            const int32_t t = toks[(size_t) (off[(size_t) e] + i)];
            if (t < 0) return kErrGeometry;            // a token row that does not exist
            if (used.find(t) == used.end()) ++fresh;
        }
        if ((int64_t) used.size() + fresh > max_sel_tok) continue;   // the distinct-token cap
        // Claim the expert WHOLE (all of its tokens): a partial claim would leave a row this expert
        // owns split across the helper and the local path, which is the double-compute the design
        // forbids.  A repeated (token, expert) pair is refused: one row, once.
        for (int64_t i = 0; i < c; ++i) {
            const int32_t t = toks[(size_t) (off[(size_t) e] + i)];
            for (int64_t q = 0; q < (int64_t) out.pair_token.size(); ++q)
                if (out.pair_token[(size_t) q] == t && out.pair_expert[(size_t) q] == (int32_t) e)
                    return kErrGeometry;
            out.pair_token.push_back(t);
            out.pair_expert.push_back((int32_t) e);
            used.insert(t);
        }
        rows += c;
    }
    out.tokens.assign(used.begin(), used.end());   // ascending: the q8 image's row order
    if ((int64_t) out.tokens.size() > max_sel_tok) return kErrGeometry;   // the caps are hard, not advisory
    return kOk;
}

ExpertHelperClient::ExpertHelperClient() : claim_batch_(claim_batch_env()) {}
ExpertHelperClient::~ExpertHelperClient() { close(); release_prompt_buffer(); }

int ExpertHelperClient::attach_socket(int fd, bool owned) {
    close();
    fd_ = fd;
    owned_ = owned;
    begin_outstanding_ = false;
    seq_ = 0;
    req_id_ = 0;
    return fd_ >= 0 ? expert_helper::kOk : expert_helper::kErrConnect;
}

int ExpertHelperClient::connect(const std::string& path, int64_t connect_timeout_ms) {
    expert_helper::Socket s;
    const int rc = s.connect(path, connect_timeout_ms);
    if (rc != expert_helper::kOk) return rc;
    fd_ = s.fd();
    s.release();
    owned_ = true;
    begin_outstanding_ = false;
    seq_ = 0;
    req_id_ = 0;
    prompt_parts_.clear();   // a fresh channel starts with no queued claim
    batch_rows_ = 0;
    batch_answered_ = false;
    return expert_helper::kOk;
}

void ExpertHelperClient::close() {
    if (fd_ >= 0 && owned_) ::close(fd_);
    fd_ = -1;
    owned_ = false;
    begin_outstanding_ = false;
    prompt_pairs_ = 0;
    prompt_parts_.clear();   // the claim batch dies with the channel: no queued claim survives a close
    batch_rows_ = 0;
    batch_answered_ = false;
}

void ExpertHelperClient::bye() {
    if (fd_ < 0) return;
    expert_helper::Socket s;
    s.adopt(fd_);
    // best effort: a helper that already closed its end makes this write fail, which is the normal exit
    (void) s.write_frame(expert_helper::kBye, req_id_, nullptr, 0, rel(kDeadlineDefaultMs, kDeadlineDefaultMs));
    s.release();
    fd_ = -1;
    owned_ = false;
    begin_outstanding_ = false;
}

int ExpertHelperClient::hello(int64_t n_layers, int64_t n_expert, int64_t n_embd, int64_t n_ff,
                              int64_t layer_lo, int64_t layer_hi, int64_t slots, int64_t reserve_mib,
                              const std::vector<uint8_t>& claimed, int64_t deadline_ms, int64_t& free_mib) {
    if (fd_ < 0) return expert_helper::kErrUnhealthy;
    expert_helper::HelloReq req;
    req.n_layers = (int32_t) n_layers;
    req.n_expert = (int32_t) n_expert;
    req.n_embd = (int32_t) n_embd;
    req.n_ff = (int32_t) n_ff;
    req.layer_lo = (int32_t) layer_lo;
    req.layer_hi = (int32_t) layer_hi;
    req.slots = slots;
    req.reserve_mib = reserve_mib;
    const uint32_t claimed_bytes = (uint32_t) ((n_layers * n_expert + 7) / 8);
    req.claimed_bytes = claimed_bytes;
    if ((int64_t) claimed.size() < n_layers * n_expert) return expert_helper::kErrGeometry;
    std::vector<uint8_t> payload(sizeof(req) + claimed_bytes, 0);
    std::memcpy(payload.data(), &req, sizeof(req));
    for (int64_t i = 0; i < n_layers * n_expert; ++i)
        if (claimed[(size_t) i]) payload[sizeof(req) + (size_t) (i >> 3)] |= (uint8_t) (1u << (i & 7));
    expert_helper::Socket s;
    s.adopt(fd_);
    const int64_t deadline = rel(deadline_ms, kDeadlineDefaultMs);
    int rc = s.write_frame(expert_helper::kHello, ++req_id_, payload.data(), payload.size(), deadline);
    if (rc != expert_helper::kOk) { s.release(); close(); return rc; }
    expert_helper::Header head;
    expert_helper::PayloadView body;
    rc = s.read_frame(head, rx_, body, deadline);
    if (rc != expert_helper::kOk) { s.release(); close(); return rc; }
    if (head.type != expert_helper::kHelloAck) { s.release(); close(); return head.type == expert_helper::kError ? expert_helper::kErrRequest : expert_helper::kErrProtocol; }
    if (head.req_id != req_id_) { s.release(); close(); return expert_helper::kErrSeq; }
    // R5 P1: the ack's size is negotiated by SHAPE, not assumed.  An R2 helper answers the 24-byte
    // legacy HelloAck (no features field); a new helper answers the full struct.  Both are read as a
    // prefix, so a missing features field is features = 0 - the prompt tier is OFF, and this client
    // never sends kBeginP to that peer.  A short ack (below the legacy shape) is a refusal.
    if (body.size() < expert_helper::kHelloAckLegacyBytes) { s.release(); close(); return expert_helper::kErrShort; }
    expert_helper::HelloAck ack{};
    std::memcpy(&ack, body.data(), body.size() < sizeof(ack) ? body.size() : sizeof(ack));
    if (ack.status != 0) { s.release(); close(); return ack.status; }
    if (ack.max_tok > expert_helper::kMaxTok || ack.k != expert_helper::kK ||
        ack.max_entries > expert_helper::kMaxEntries) { s.release(); close(); return expert_helper::kErrGeometry; }
    free_mib = ack.free_mib;
    n_expert_ = n_expert;
    n_embd_ = n_embd;
    layer_lo_ = layer_lo;
    layer_hi_ = layer_hi;
    prompt_rows_ = (body.size() >= sizeof(expert_helper::HelloAck)) &&
                   (ack.features & expert_helper::kFeaturePromptRows) != 0;
    prompt_f32_ = prompt_rows_ &&
                  (ack.features & expert_helper::kFeaturePromptF32) != 0;   // M4c (b)
    draft_ = (body.size() >= sizeof(expert_helper::HelloAck)) &&
             (ack.features & expert_helper::kFeatureDraft) != 0;   // M7: the drafter lane
    draft_bf16_ = (body.size() >= sizeof(expert_helper::HelloAck)) &&
                  (ack.features & expert_helper::kFeatureDraftBf16) != 0;   // L8: bf16 taps
    q8k_ = (body.size() >= sizeof(expert_helper::HelloAck)) &&
           (ack.features & expert_helper::kFeatureQ8K) != 0;   // the Q8_K activation wire
    // The claim batch's PEER half of the gate: the bit says this helper serves kBeginPB/kRowsPB.
    // The ENGINE half is STRATA_CLAIM_BATCH (default 1).  Both must hold for a batch frame to exist;
    // with the env unset the prompt tier writes kBeginP exactly as today, so the bit changes nothing
    // on the wire for a K=1 engine, and a K>1 engine against a bit-less helper keeps kBeginP too.
    prompt_batch_ = (body.size() >= sizeof(expert_helper::HelloAck)) &&
                    (ack.features & expert_helper::kFeaturePromptBatch) != 0;
    claim_batch_ = claim_batch_env();
    seq_ = head.seq;
    expert_helper::log_socket_buffers(fd_, "engine");   // helper-ipc-diet: the achieved buffer pair, once per hello
    s.release();
    return expert_helper::kOk;
}

int ExpertHelperClient::fill_report(const std::vector<std::pair<int32_t, int32_t>>& pairs, int64_t deadline_ms,
                                    std::vector<std::pair<int32_t, int32_t>>& resident_pairs, int64_t& bytes) {
    if (fd_ < 0) return expert_helper::kErrUnhealthy;
    expert_helper::FillReq req;
    req.n_pairs = (int32_t) pairs.size();
    std::vector<uint8_t> payload(sizeof(req) + pairs.size() * 8);
    std::memcpy(payload.data(), &req, sizeof(req));
    for (size_t i = 0; i < pairs.size(); ++i) {
        std::memcpy(payload.data() + sizeof(req) + i * 8, &pairs[i].first, 4);
        std::memcpy(payload.data() + sizeof(req) + i * 8 + 4, &pairs[i].second, 4);
    }
    expert_helper::Socket s;
    s.adopt(fd_);
    int rc = s.write_frame(expert_helper::kFill, ++req_id_, payload.data(), payload.size(),
                           rel(deadline_ms, kDeadlineDefaultMs));
    if (rc != expert_helper::kOk) { s.release(); close(); return rc; }
    expert_helper::Header head;
    expert_helper::PayloadView body;
    rc = s.read_frame(head, rx_, body, rel(deadline_ms, 600000));   // the fill copies GiB: a long, bounded wait
    if (rc != expert_helper::kOk) { s.release(); close(); return rc; }
    if (head.type == expert_helper::kError) { s.release(); close(); return expert_helper::kErrRequest; }
    if (head.type != expert_helper::kFillReport || head.req_id != req_id_) { s.release(); close(); return expert_helper::kErrSeq; }
    if (body.size() < sizeof(expert_helper::FillReport)) { s.release(); close(); return expert_helper::kErrShort; }
    expert_helper::FillReport rep;
    std::memcpy(&rep, body.data(), sizeof(rep));
    if (rep.n_pairs < 0 || body.size() != sizeof(rep) + (size_t) rep.n_pairs * 8) { s.release(); close(); return expert_helper::kErrShort; }
    resident_pairs.clear();
    for (int32_t i = 0; i < rep.n_pairs; ++i) {
        int32_t l = 0, e = 0;
        std::memcpy(&l, body.data() + sizeof(rep) + (size_t) i * 8, 4);
        std::memcpy(&e, body.data() + sizeof(rep) + (size_t) i * 8 + 4, 4);
        resident_pairs.push_back({l, e});
    }
    if (rep.resident != (int64_t) rep.n_pairs) { s.release(); close(); return expert_helper::kErrShort; }
    bytes = rep.bytes;
    seq_ = head.seq;
    s.release();
    return expert_helper::kOk;
}

int ExpertHelperClient::begin(int32_t layer, int32_t n_tok, const int32_t* ids, const int32_t* primary_res,
                              const uint8_t* q8, size_t q8_bytes, int64_t deadline_ms) {
    if (fd_ < 0) return expert_helper::kErrUnhealthy;
    if (n_expert_ <= 0) return expert_helper::kErrGeometry;   // the handshake pins the geometry
    const int64_t n_expert = n_expert_;
    if (n_tok < 1 || n_tok > expert_helper::kMaxTok) return expert_helper::kErrGeometry;
    const int64_t n = n_tok * expert_helper::kK;
    const size_t res_bytes = (size_t) ((n_tok * n_expert + 7) / 8);   // the layer's primary residency bitmap
    expert_helper::BeginReq req;
    req.layer = layer;
    req.n_tok = n_tok;
    req.k = (int32_t) expert_helper::kK;
    req.ids_bytes = (int32_t) (n * 4);
    req.n_q8 = (int32_t) q8_bytes;
    std::vector<uint8_t> payload(sizeof(req) + (size_t) n * 4 + res_bytes + q8_bytes, 0);
    std::memcpy(payload.data(), &req, sizeof(req));
    std::memcpy(payload.data() + sizeof(req), ids, (size_t) n * 4);   // the routed ids, routing order
    for (int64_t e = 0; e < n_expert; ++e) {   // the layer's PRIMARY residency (slot >= 0), expert-indexed
        if (primary_res && primary_res[(size_t) layer * (size_t) n_expert + (size_t) e] >= 0)
            payload[sizeof(req) + (size_t) n * 4 + (size_t) (e >> 3)] |= (uint8_t) (1u << (e & 7));
    }
    std::memcpy(payload.data() + sizeof(req) + (size_t) n * 4 + res_bytes, q8, q8_bytes);
    // M7 (sect12a, BUG E): the window tier's begin/finish pair spans a layer's compute (the reply
    // is read by finish), so this request is the ONE outstanding request of the protocol for that
    // whole span.  io_mtx_ is held across the write and the outstanding-flag update, and finish
    // holds it across the reply's read: the drafter lane's gate (gate_draft_frame) reads the same
    // flag under the same lock, so a draft frame can never be written behind this reply.
    std::lock_guard<std::mutex> io_lk(io_mtx_);
    expert_helper::Socket s;
    s.adopt(fd_);
    const auto t0 = std::chrono::steady_clock::now();
    const int rc = s.write_frame(expert_helper::kBegin, ++req_id_, payload.data(), payload.size(),
                                 rel(deadline_ms, kDeadlineDefaultMs));
    ms_begin_ += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    if (rc != expert_helper::kOk) { s.release(); close(); return rc; }
    begin_outstanding_ = true;
    begin_seq_ = s.seq_out();
    seq_ = begin_seq_;
    ++requests_;
    s.release();
    return expert_helper::kOk;
}

int ExpertHelperClient::begin_q8k(int32_t layer, int32_t n_tok, const int32_t* ids, const int32_t* primary_res,
                                  const uint8_t* q8k, size_t q8k_bytes, int64_t deadline_ms) {
    if (fd_ < 0) return expert_helper::kErrUnhealthy;
    // The negotiation gate, BEFORE any write: kBeginQ8K is a frame type an unadvertised peer cannot
    // type-decode, so it is refused HERE and never reaches the wire (the same rule as begin_prompt's).
    if (!q8k_) return expert_helper::kErrRequest;
    if (n_expert_ <= 0) return expert_helper::kErrGeometry;   // the handshake pins the geometry
    const int64_t n_expert = n_expert_;
    if (n_tok < 1 || n_tok > expert_helper::kMaxTok) return expert_helper::kErrGeometry;
    const int64_t n = n_tok * expert_helper::kK;
    const size_t res_bytes = (size_t) ((n_tok * n_expert + 7) / 8);   // the layer's primary residency bitmap
    expert_helper::BeginReq req;
    req.layer = layer;
    req.n_tok = n_tok;
    req.k = (int32_t) expert_helper::kK;
    req.ids_bytes = (int32_t) (n * 4);
    req.n_q8 = (int32_t) q8k_bytes;   // Q8_K blocks: n_tok * (n_embd/256) * 292
    std::vector<uint8_t> payload(sizeof(req) + (size_t) n * 4 + res_bytes + q8k_bytes, 0);
    std::memcpy(payload.data(), &req, sizeof(req));
    std::memcpy(payload.data() + sizeof(req), ids, (size_t) n * 4);   // the routed ids, routing order
    for (int64_t e = 0; e < n_expert; ++e)
        if (primary_res && primary_res[(size_t) layer * (size_t) n_expert + (size_t) e] >= 0)
            payload[sizeof(req) + (size_t) n * 4 + (size_t) (e >> 3)] |= (uint8_t) (1u << (e & 7));
    std::memcpy(payload.data() + sizeof(req) + (size_t) n * 4 + res_bytes, q8k, q8k_bytes);
    // The ONE outstanding request rule is begin's own (io_mtx_ across the write; finish reads the reply).
    std::lock_guard<std::mutex> io_lk(io_mtx_);
    expert_helper::Socket s;
    s.adopt(fd_);
    const auto t0 = std::chrono::steady_clock::now();
    const int rc = s.write_frame(expert_helper::kBeginQ8K, ++req_id_, payload.data(), payload.size(),
                                 rel(deadline_ms, kDeadlineDefaultMs));
    ms_begin_ += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    if (rc != expert_helper::kOk) { s.release(); close(); return rc; }
    begin_outstanding_ = true;
    begin_seq_ = s.seq_out();
    seq_ = begin_seq_;
    ++requests_;
    s.release();
    return expert_helper::kOk;
}

int ExpertHelperClient::finish(float* out, int64_t n_embd, int32_t n_tok, int64_t k,
                               const std::vector<int32_t>& claims, int64_t timeout_ms, int32_t& rows) {
    rows = 0;
    if (fd_ < 0) return expert_helper::kErrUnhealthy;
    // M7 (sect12a, BUG E): the whole read + pairing + outstanding-flag clear runs under io_mtx_
    // (see begin) - this is the point where the drafter lane's gate sees the channel as free.
    std::lock_guard<std::mutex> io_lk(io_mtx_);
    if (!begin_outstanding_) return expert_helper::kErrSeq;   // a reply with no outstanding request
    expert_helper::Socket s;
    s.adopt(fd_);
    const auto t0 = std::chrono::steady_clock::now();
    expert_helper::Header head;
    expert_helper::PayloadView body;
    int rc = s.read_frame(head, rx_, body, rel(timeout_ms, kDeadlineDefaultMs));
    ms_wait_ += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    if (rc == expert_helper::kErrTimeout) {
        ++timeouts_;
        // H12 observability: this close is the tier's death for the rest of the run - say WHY,
        // with the deadline that was missed (r14/r15 died here in silence).
        std::fprintf(stderr, "expert-helper: the reply missed its %lld ms deadline (timeouts now %lld); "
                             "closing the channel - the tier is out for this request\n",
                     (long long) timeout_ms, (long long) timeouts_);
        s.release(); close(); return rc;
    }
    if (rc != expert_helper::kOk) {
        std::fprintf(stderr, "expert-helper: reading the reply failed: %s; closing the channel\n",
                     expert_helper::status_name(rc));
        s.release(); close(); return rc;
    }
    if (head.type == expert_helper::kError) {
        expert_helper::ErrResp err{};
        if (body.size() >= sizeof(err)) std::memcpy(&err, body.data(), sizeof(err));
        std::fprintf(stderr, "expert-helper: the helper refused a window: %lld %s; closing the channel\n",
                     (long long) err.status, err.text);
        s.release();
        close();
        return expert_helper::kErrRequest;
    }
    if (head.type != expert_helper::kRows) {
        std::fprintf(stderr, "expert-helper: unexpected reply frame type %u; closing the channel\n", head.type);
        s.release(); close(); return expert_helper::kErrProtocol;
    }
    if (head.req_id != req_id_ || head.seq != begin_seq_) {
        std::fprintf(stderr, "expert-helper: reply rejected: req_id %u (want %u) seq %u (want %u); "
                             "closing the channel\n", head.req_id, req_id_, head.seq, begin_seq_);
        s.release(); close(); return expert_helper::kErrSeq;
    }
    (void) n_tok;
    (void) k;
    rc = validate_rows_payload(body.data(), body.size(), n_embd, claims, out, rows);
    if (rc != expert_helper::kOk) {
        std::fprintf(stderr, "expert-helper: the reply failed row validation: %s (%lld rows for %lld claims); "
                             "closing the channel\n", expert_helper::status_name(rc),
                     (long long) rows, (long long) claims.size());
        s.release(); close(); return rc;
    }
    bytes_ += (uint64_t) rows * (uint64_t) n_embd * 4ull;
    rows_ += (uint64_t) rows;
    begin_outstanding_ = false;
    s.release();
    return expert_helper::kOk;
}

// ---- R5 P1: the prompt tier's own pinned activation buffer, request, and reply validation. ----

int ExpertHelperClient::prompt_buffer_ensure(int64_t n_embd) {
    if (n_embd <= 0 || (n_embd % 32) != 0) return expert_helper::kErrGeometry;   // whole q8_1 blocks
    const size_t cap = (size_t) expert_helper::kMaxSelTok * (size_t) n_embd * sizeof(float);
    if (ext_x_ != nullptr) {
        if (ext_x_embd_ == n_embd && ext_x_bytes_ == cap) return expert_helper::kOk;   // GROWN ONCE
        return expert_helper::kErrGeometry;   // the tier never changes its activation geometry
    }
    // Page-locked host memory: MAP_LOCKED where the limit allows it, otherwise mmap + mlock.  Pinned
    // or refused - a tier that cannot pin its rows does not run (the design's allocation-failure
    // policy: tier disabled for the request, printed notice, full local compute; P2 owns the print).
    void* p = ::mmap(nullptr, cap, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_LOCKED, -1, 0);
    if (p == MAP_FAILED) {
        p = ::mmap(nullptr, cap, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (p == MAP_FAILED) return expert_helper::kErrInternal;
        if (::mlock(p, cap) != 0) {
            const int e = errno;
            ::munmap(p, cap);
            errno = e;
            return expert_helper::kErrInternal;
        }
    }
    ext_x_ = p;
    ext_x_bytes_ = cap;
    ext_x_embd_ = n_embd;
    return expert_helper::kOk;
}

void ExpertHelperClient::release_prompt_buffer() {
    if (ext_x_ != nullptr) {
        ::munmap(ext_x_, ext_x_bytes_);
        ext_x_ = nullptr;
        ext_x_bytes_ = 0;
        ext_x_embd_ = 0;
    }
}

// The prompt tier's OWN request body: every gate and cap begin_prompt runs, and the frame's exact
// bytes (BeginPReq + pairs + the layer's PRIMARY residency bitmap + the image quantized from the
// tier's own rows).  Shared by the single-claim path (writes it as kBeginP, today's bytes exactly)
// and the claim batch (queues it as one part of a kBeginPB), so a batched part is byte-for-byte the
// request a standalone kBeginP carries.
int ExpertHelperClient::build_beginp_payload(int32_t layer, const ExpertHelperPromptPlan& plan,
                                             const int32_t* host_res, std::vector<uint8_t>& payload,
                                             int64_t& rows_out) {
    using namespace expert_helper;
    // The negotiation gate, BEFORE any write: a peer that never advertised the tier (an R2 helper,
    // whose 24-byte ack has no features field) gets a typed refusal here and the new frame type never
    // reaches it.  There is nothing to misparse and nothing to wait on.
    if (!prompt_rows_) return kErrRequest;
    if (n_expert_ <= 0 || n_embd_ <= 0) return kErrGeometry;      // the handshake pins the geometry
    const int64_t rows = plan.rows(), n_sel = plan.n_sel_tok();
    if (rows < 1 || rows > kMaxRowsPerLayer) return kErrGeometry;
    if (n_sel < 1 || n_sel > kMaxSelTok) return kErrGeometry;
    if (layer < layer_lo_ || layer > layer_hi_) return kErrGeometry;   // the hello window (32..47)
    if (ext_x_ == nullptr || ext_x_embd_ != n_embd_) return kErrGeometry;   // the tier owns its rows
    if ((size_t) n_sel * (size_t) n_embd_ * sizeof(float) > ext_x_bytes_) return kErrGeometry;

    // Pair sanity and the per-expert cap, before the frame is built.  The wire's token IS the q8
    // image row (0..n_sel-1), so a pair whose token is not in the plan has no activation to send.
    std::map<int64_t, int64_t> per_expert;
    std::vector<int32_t> wire_token((size_t) rows), wire_expert((size_t) rows);
    for (int64_t i = 0; i < rows; ++i) {
        const int64_t e = plan.pair_expert[(size_t) i];
        if (e < 0 || e >= n_expert_) return kErrGeometry;
        const int row = plan.image_row(plan.pair_token[(size_t) i]);
        if (row < 0 || row >= n_sel) return kErrGeometry;
        wire_token[(size_t) i] = row;
        wire_expert[(size_t) i] = (int32_t) e;
        if (++per_expert[e] > kMaxRowsPerExpert) return kErrGeometry;
    }
    // M4c (b): with an fp32-capable peer the image ships as the tier's OWN float rows - ONE
    // contiguous copy, no engine-side quantize (that cost leaves this critical path; the helper
    // quantizes in its own process, overlapped with the primary's compute).  q8 peers keep the
    // engine-side quantizer the decode contract pins.
    //
    // The Q8_K wire (default OFF: STRATA_EXPERT_HELPER_Q8K with a peer that advertised
    // kFeatureQ8K): the image is the CPU reference's own gate/up activation - quantize_row_q8k_cpu
    // above restates ggml's reference quantizer with the same bits - so the helper's grouped Q8_K
    // kernel reproduces the reference arithmetic (measured, docs/work/MIXED_P2_REFERENCE.md: the
    // q8_1 wire's worst output rel 2.77e-2 falls to 1.78e-7, the down kernel's summation-order
    // floor).  Any miss keeps TODAY's image exactly.
    //
    // The client's own gates are ENV + PEER BIT + GEOMETRY alone: whether the LAYER belongs on the
    // Q8_K wire (its pack format is Q8_K gate/up and its weight type has a Q8_K kernel) is the
    // HELPER's check, like kBeginP's other refusals - it answers image_format 2 with this
    // transport's typed kError, the prompt tier's same-layer CPU fallback serves the rows, and a
    // mixed build can never misparse (the format never reaches a peer without the bit).
    const bool q8k_img = std::getenv("STRATA_EXPERT_HELPER_Q8K") != nullptr && q8k_ &&
                         n_embd_ > 0 && n_embd_ % 256 == 0;
    const bool f32_img = prompt_f32_ && !q8k_img;
    const int64_t n_img = q8k_img ? n_sel * (n_embd_ / 256) * 292
                                  : f32_img ? n_sel * n_embd_ * (int64_t) sizeof(float)
                                            : n_sel * (n_embd_ / 32) * 36;
    const size_t res_bytes = (size_t) ((n_sel * n_expert_ + 7) / 8);   // kBegin's own bitmap shape
    const size_t total = sizeof(BeginPReq) + (size_t) rows * 8 + res_bytes + (size_t) n_img;
    if (n_img <= 0 || total > kMaxFramePayload) return kErrGeometry;   // 84 MB fp32 at the caps; over = refusal

    payload.assign(total, 0);
    BeginPReq req;
    req.layer = layer;
    req.n_sel_tok = (int32_t) n_sel;
    req.k = (int32_t) kK;
    req.n_pairs = (int32_t) rows;
    req.n_q8 = (int32_t) n_img;
    req.image_format = q8k_img ? 2 : (f32_img ? 1 : 0);
    std::memcpy(payload.data(), &req, sizeof(req));
    for (int64_t i = 0; i < rows; ++i) {
        std::memcpy(payload.data() + sizeof(req) + (size_t) i * 8, &wire_token[(size_t) i], 4);
        std::memcpy(payload.data() + sizeof(req) + (size_t) i * 8 + 4, &wire_expert[(size_t) i], 4);
    }
    uint8_t* res = payload.data() + sizeof(req) + (size_t) rows * 8;
    for (int64_t e = 0; e < n_expert_; ++e)   // the layer's PRIMARY residency (same semantics as kBegin)
        if (host_res != nullptr && host_res[(size_t) layer * (size_t) n_expert_ + (size_t) e] >= 0)
            res[(size_t) (e >> 3)] |= (uint8_t) (1u << (e & 7));
    // The image, built HERE from the tier's OWN rows: fp32 ships them verbatim (one contiguous
    // copy - ext_x_ holds rows 0..n_sel-1 densely); q8 uses the SAME quantizer the decode contract
    // pins; q8k uses the CPU reference's own quantizer, per row, at the pack's declared row size.
    uint8_t* q8 = res + res_bytes;
    const float* src = (const float*) ext_x_;
    if (q8k_img) {
        for (int64_t r = 0; r < n_sel; ++r)
            quantize_row_q8k_cpu(src + (size_t) r * (size_t) n_embd_, n_embd_,
                                 q8 + (size_t) r * (size_t) (n_embd_ / 256) * 292);
    } else if (f32_img) {
        std::memcpy(q8, src, (size_t) n_img);
    } else {
        for (int64_t r = 0; r < n_sel; ++r)
            quantize_q8_1_cpu(src + (size_t) r * (size_t) n_embd_, n_embd_, q8 + (size_t) r * (size_t) (n_embd_ / 32) * 36);
    }
    rows_out = rows;
    return kOk;
}

int ExpertHelperClient::begin_prompt(int32_t layer, const ExpertHelperPromptPlan& plan,
                                     const int32_t* host_res, int64_t deadline_ms) {
    using namespace expert_helper;
    if (fd_ < 0) return kErrUnhealthy;
    std::vector<uint8_t> payload;
    int64_t rows = 0;
    const int rc_build = build_beginp_payload(layer, plan, host_res, payload, rows);
    if (rc_build != kOk) return rc_build;

    // ---- The claim batch (default OFF: STRATA_CLAIM_BATCH = 1 is the path below, byte for byte). ----
    //
    // K>1 with a peer that advertised kFeaturePromptBatch: this claim becomes ONE PART of a kBeginPB
    // and NOTHING goes on the wire yet.  The part carries exactly the bytes a standalone kBeginP
    // would (the builder above ran on the tier's rows, which are final at this call - the caller
    // synced its activation download before it), so the helper parses it with kBeginP's own checks.
    // The batch's FIRST finish_prompt flushes the queue as one frame and reads its one reply; the
    // parts answer in queue order, which is the engine's claim order, so the order of application is
    // unchanged.  The caps are hard refusals BEFORE any write: the queue's part count, the request's
    // byte sum, and the reply's row sum (rows * (4 + n_embd*4) inside kMaxBatchPayload).  A refusal
    // leaves the queue exactly as it was: the layer stays with the caller's same-layer fallback.
    if (claim_batch_ > 1 && prompt_batch_) {
        // A new claim cannot join a batch whose reply is already in hand: its staged parts answer the
        // calls that are already queued, and a part appended behind them would never be answered.
        // The caller's rule for a refused begin is its same-layer fallback, so this is a named
        // refusal with nothing written and the queue untouched.
        if (batch_answered_) return kErrBusy;
        if ((int64_t) prompt_parts_.size() >= claim_batch_) return kErrGeometry;   // the queue is full
        size_t req_bytes = sizeof(BeginPBReq) + payload.size();
        for (const PromptPart& p : prompt_parts_) req_bytes += p.request.size();
        if (req_bytes > kMaxBatchPayload) return kErrGeometry;   // a batch the reader would refuse
        const int64_t row_bytes = 4 + n_embd_ * (int64_t) sizeof(float);
        if (row_bytes <= 0 || batch_rows_ + rows > (int64_t) (kMaxBatchPayload / (size_t) row_bytes))
            return kErrGeometry;                                  // a reply no single frame can carry
        PromptPart part;
        part.request = std::move(payload);
        part.claims.resize((size_t) rows);
        for (int64_t i = 0; i < rows; ++i) part.claims[(size_t) i] = (int32_t) i;   // the dense pair list
        prompt_parts_.push_back(std::move(part));
        batch_rows_ += rows;
        return kOk;
    }

    expert_helper::Socket s;
    s.adopt(fd_);
    const auto t0 = std::chrono::steady_clock::now();
    // M7 (sect12a, BUG E): see begin/finish - the prompt tier's pair is the ONE outstanding
    // request across the layer's compute, under io_mtx_, and the drafter lane's gate (the FIX A
    // tap callback on the successor stage's thread) waits for exactly this flag.
    std::lock_guard<std::mutex> io_lk(io_mtx_);
    const int rc = s.write_frame(kBeginP, ++req_id_, payload.data(), payload.size(),
                                 rel(deadline_ms, kDeadlineDefaultMs));
    ms_begin_ += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    if (rc != kOk) { s.release(); close(); return rc; }
    begin_outstanding_ = true;
    begin_seq_ = s.seq_out();
    seq_ = begin_seq_;
    prompt_pairs_ = rows;
    ++prompt_requests_;
    s.release();
    return kOk;
}

int ExpertHelperClient::finish_prompt(float* out, int64_t n_embd, const std::vector<int32_t>& claims,
                                      int64_t timeout_ms, int32_t& rows) {
    rows = 0;
    if (fd_ < 0) return expert_helper::kErrUnhealthy;
    // M7 (sect12a, BUG E): see finish - the read, the pairing and the flag clear all run under
    // io_mtx_, and the flag is what the drafter lane's gate waits on.
    std::lock_guard<std::mutex> io_lk(io_mtx_);
    // The claim batch (default OFF, K = 1 never queues): the queued parts answer in claim order, the
    // first call flushing the batch and the rest reading their staged parts.
    if (!prompt_parts_.empty()) return finish_prompt_batch(out, n_embd, claims, timeout_ms, rows);
    if (!begin_outstanding_) return expert_helper::kErrSeq;   // a reply with no outstanding request
    expert_helper::Socket s;
    s.adopt(fd_);
    const auto t0 = std::chrono::steady_clock::now();
    expert_helper::Header head;
    expert_helper::PayloadView body;
    int rc = s.read_frame(head, rx_, body, rel(timeout_ms, kDeadlineDefaultMs));
    ms_wait_ += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    if (rc == expert_helper::kErrTimeout) {
        ++timeouts_;
        std::fprintf(stderr, "expert-helper: the prompt row reply missed its %lld ms deadline "
                             "(timeouts now %lld); closing the channel - the tier falls back for this request\n",
                     (long long) timeout_ms, (long long) timeouts_);
        s.release(); close(); return rc;
    }
    if (rc != expert_helper::kOk) {
        std::fprintf(stderr, "expert-helper: reading the prompt row reply failed: %s; closing the channel\n",
                     expert_helper::status_name(rc));
        s.release(); close(); return rc;
    }
    if (head.type == expert_helper::kError) {
        expert_helper::ErrResp err{};
        if (body.size() >= sizeof(err)) std::memcpy(&err, body.data(), sizeof(err));
        std::fprintf(stderr, "expert-helper: the helper refused the prompt row request: %lld %s; "
                             "closing the channel\n", (long long) err.status, err.text);
        s.release(); close(); return expert_helper::kErrRequest;
    }
    if (head.type != expert_helper::kRowsP) {
        std::fprintf(stderr, "expert-helper: unexpected prompt reply frame type %u; closing the channel\n",
                     head.type);
        s.release(); close(); return expert_helper::kErrProtocol;
    }
    if (head.req_id != req_id_ || head.seq != begin_seq_) {
        std::fprintf(stderr, "expert-helper: prompt reply rejected: req_id %u (want %u) seq %u (want %u); "
                             "closing the channel\n", head.req_id, req_id_, head.seq, begin_seq_);
        s.release(); close(); return expert_helper::kErrSeq;
    }
    // kRowsP carries kRows' row layout exactly (RowsPResp and RowsResp are the same 8-byte shape,
    // rows are (int32 index, n_embd floats)), so the SAME validator runs: every claim answered, no
    // unclaimed index, no duplicate, every float finite, applied WHOLE or not at all.
    rc = validate_rows_payload(body.data(), body.size(), n_embd, claims, out, rows);
    if (rc != expert_helper::kOk) {
        std::fprintf(stderr, "expert-helper: the prompt reply failed row validation: %s (%lld rows for "
                             "%lld claims); closing the channel\n", expert_helper::status_name(rc),
                      (long long) rows, (long long) claims.size());
        s.release(); close(); return rc;
    }
    prompt_rows_returned_ += (uint64_t) rows;
    prompt_bytes_ += (uint64_t) rows * (uint64_t) n_embd * 4ull;
    prompt_pairs_ = 0;
    begin_outstanding_ = false;
    s.release();
    return expert_helper::kOk;
}

// ---- The claim batch's reply path (STRATA_CLAIM_BATCH > 1 AND the peer's kFeaturePromptBatch). ----
//
// io_mtx_ is held by finish_prompt for the whole call, so the flush's write, the reply's read and the
// pairing test are ONE transaction of the protocol's ONE-outstanding-request rule - identical to what
// begin_prompt/finish_prompt do for a single claim.  The batch is transport-only:
//   * the flush's payload is BeginPBReq + the queued parts' bytes in queue order, and every part is
//     the exact kBeginP payload build_beginp_payload produced, so the helper runs kBeginP's own parse
//     and kBeginP's own caps per part;
//   * the reply's parts are split by length (RowsPResp + n_rows * (4 + n_embd*4) each) and EACH part
//     is validated by validate_rows_payload against THAT part's own claims - the same whole-or-nothing
//     rule a standalone kRowsP has, so a missing row is the caller's same-layer fallback and a
//     reordering cannot survive (an index the part did not claim is kErrBadId);
//   * the parts answer in queue order, which is the order the claims were made, so the order of
//     application is unchanged.
int ExpertHelperClient::finish_prompt_batch(float* out, int64_t n_embd, const std::vector<int32_t>& claims,
                                            int64_t timeout_ms, int32_t& rows) {
    using namespace expert_helper;
    PromptPart& front = prompt_parts_.front();
    // The batch's parts answer in CLAIM order, so a caller asking for any other set is a caller
    // that cannot use these rows.  The refusal is checked BEFORE the flush: the batch never
    // reaches the wire, so no reply is burned and no row can be applied out of order.
    if (front.claims != claims) {
        std::fprintf(stderr, "expert-helper: the prompt batch answers its parts in claim order; the "
                             "caller's %zu claims do not match the front part's %zu (first claim %d, "
                             "asked %d); closing the channel before the batch is sent\n",
                     claims.size(), front.claims.size(),
                     front.claims.empty() ? -1 : front.claims.front(),
                     claims.empty() ? -1 : claims.front());
        close();
        return kErrSeq;
    }
    if (!batch_answered_) {
        const int64_t parts = (int64_t) prompt_parts_.size();
        size_t total = sizeof(BeginPBReq);
        for (const PromptPart& p : prompt_parts_) total += p.request.size();
        std::vector<uint8_t> payload(total, 0);
        BeginPBReq head;
        head.n_parts = (int32_t) parts;
        head.flags = 0;
        head.reserved0 = 0;
        std::memcpy(payload.data(), &head, sizeof(head));
        size_t off = sizeof(head);
        for (const PromptPart& p : prompt_parts_) {
            std::memcpy(payload.data() + off, p.request.data(), p.request.size());
            off += p.request.size();
        }
        expert_helper::Socket s;
        s.adopt(fd_);
        const auto t0 = std::chrono::steady_clock::now();
        const int64_t deadline = rel(timeout_ms, kDeadlineDefaultMs);
        int rc = s.write_frame(kBeginPB, ++req_id_, payload.data(), payload.size(), deadline);
        ms_begin_ += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
        if (rc != kOk) { s.release(); close(); return rc; }
        begin_outstanding_ = true;
        begin_seq_ = s.seq_out();
        seq_ = begin_seq_;
        prompt_pairs_ = batch_rows_;
        ++prompt_requests_;   // ONE exchange served K claims: the counter counts exchanges
        const auto t1 = std::chrono::steady_clock::now();
        Header head2;
        PayloadView body;
        rc = s.read_frame(head2, rx_, body, deadline);
        ms_wait_ += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t1).count();
        if (rc == kErrTimeout) {
            ++timeouts_;
            std::fprintf(stderr, "expert-helper: the prompt batch reply (%lld parts) missed its %lld ms "
                                 "deadline (timeouts now %lld); closing the channel - the tier falls back "
                                 "for this request\n", (long long) parts, (long long) timeout_ms,
                         (long long) timeouts_);
            s.release(); close(); return rc;
        }
        if (rc != kOk) {
            std::fprintf(stderr, "expert-helper: reading the prompt batch reply failed: %s; closing the "
                                 "channel\n", status_name(rc));
            s.release(); close(); return rc;
        }
        if (head2.type == kError) {
            ErrResp err{};
            if (body.size() >= sizeof(err)) std::memcpy(&err, body.data(), sizeof(err));
            std::fprintf(stderr, "expert-helper: the helper refused the prompt batch request: %lld %s; "
                                 "closing the channel\n", (long long) err.status, err.text);
            s.release(); close(); return kErrRequest;
        }
        if (head2.type != kRowsPB) {
            std::fprintf(stderr, "expert-helper: unexpected prompt batch reply frame type %u; closing "
                                 "the channel\n", head2.type);
            s.release(); close(); return kErrProtocol;
        }
        if (head2.req_id != req_id_ || head2.seq != begin_seq_) {
            std::fprintf(stderr, "expert-helper: prompt batch reply rejected: req_id %u (want %u) seq "
                                 "%u (want %u); closing the channel\n", head2.req_id, req_id_,
                         head2.seq, begin_seq_);
            s.release(); close(); return kErrSeq;
        }
        if (body.size() < sizeof(RowsPBResp)) { s.release(); close(); return kErrShort; }
        RowsPBResp rep;
        std::memcpy(&rep, body.data(), sizeof(rep));
        if (rep.n_parts != (int32_t) parts || rep.reserved0 != 0) {
            std::fprintf(stderr, "expert-helper: the prompt batch reply answered %d parts, %lld were "
                                 "asked; closing the channel\n", rep.n_parts, (long long) parts);
            s.release(); close(); return kErrShort;
        }
        const size_t row_bytes = 4 + (size_t) n_embd * 4;
        size_t off2 = sizeof(rep);
        for (int64_t i = 0; i < parts; ++i) {
            if (off2 + sizeof(RowsPResp) > body.size()) { s.release(); close(); return kErrShort; }
            RowsPResp pr;
            std::memcpy(&pr, body.data() + off2, sizeof(pr));
            if (pr.n_rows < 0 || pr.reserved0 != 0) { s.release(); close(); return kErrShort; }
            const size_t need = sizeof(pr) + (size_t) pr.n_rows * row_bytes;
            if (off2 + need > body.size()) { s.release(); close(); return kErrShort; }
            const uint8_t* p = body.data() + off2;
            prompt_parts_[(size_t) i].reply.assign(p, p + need);
            off2 += need;
        }
        if (off2 != body.size()) { s.release(); close(); return kErrShort; }   // the parts' total is exact
        s.release();
        begin_outstanding_ = false;
        batch_answered_ = true;
    }
    // The front part, validated and applied by the SAME whole-or-nothing rule a standalone kRowsP runs.
    const int rc = validate_rows_payload(front.reply.data(), front.reply.size(), n_embd, front.claims,
                                         out, rows);
    if (rc != kOk) {
        std::fprintf(stderr, "expert-helper: the prompt batch's part failed row validation: %s (%lld "
                             "rows for %lld claims); closing the channel\n", status_name(rc),
                      (long long) rows, (long long) front.claims.size());
        close();
        return rc;
    }
    prompt_rows_returned_ += (uint64_t) rows;
    prompt_bytes_ += (uint64_t) rows * (uint64_t) n_embd * 4ull;
    const int64_t claimed = (int64_t) front.claims.size();   // read before the erase invalidates it
    prompt_parts_.erase(prompt_parts_.begin());
    batch_rows_ -= claimed;
    if (prompt_parts_.empty()) { batch_answered_ = false; prompt_pairs_ = 0; }
    return kOk;
}

// ---- M7 (the drafter lane, docs/work/MIXED_BACKEND_RPC_PLAN.md sect12/12a): kDraftCtx / kDraftReq. ----
//
// Same transport, same rules as the tiers: every size checked BEFORE anything is written, every
// failure a named status, a send/recv failure or timeout closes the channel (the transport's
// established rule: no reconnect exists, and the helper's own error reply ends its request loop, so
// a dead draft reply IS a dead channel), and one outstanding request at a time.
//
// BUG E (the run-2 live fault, m3-engine.log:11517/11791/12066 "draft ctx reply rejected: req_id 986
// (want 987) seq 1 (want 1); closing the channel"): the assumption this block used to state - "the
// window tier's begin/finish never interleave with it" - holds only when the draft frame and the
// tier's begin/finish run on the SAME thread.  With a layer split they do not: the chain's last
// stage fires the prompt tap callback (src/program/generate.cpp:9383 -> :9432 draft_context) from
// the successor stage's own thread (src/prefill/prefill.cpp:5360-5369 runs next_->run_impl on the
// hand-off worker / std::async), while the first stage's thread sits between begin_prompt
// (expert_helper_socket.cpp:546, reply left outstanding) and finish_prompt (:559, the reply read).
// Two threads, one fd, one req_id_ counter, no lock: the draft frame's ack read consumes the PEER
// thread's kRowsP reply, and the pairing test fails on req_id (off by exactly one).  The seq test
// cannot catch it: every client call adopts a fresh Socket whose seq_out_ counts from 0, so both
// sides carry seq 1 (see the H13 note in expert_helper_protocol.hpp and the seq-fault section of
// tests/core/expert_helper_socket_test.cpp, which pins exactly that per-call pair).
//
// The fix restores the protocol's own rule - ONE outstanding request, enforced: io_mtx_ serializes
// every socket transaction, and gate_draft_frame waits until the tiers have no in-flight request
// before a draft frame goes on the wire.  A draft frame that cannot fit inside its own bound
// answers kErrBusy with NOTHING written: the channel stays open, the tiers keep using it, and the
// caller disables drafting for the rest of the request (plan sect12's failure rule).
// The rows arrive WIRE ROW-MAJOR (the caller's df_wire_rows transposed them from the verifier's
// tap-major taps); they are copied verbatim, exactly as the helper's stage_draft_rows expects them.

bool ExpertHelperClient::gate_draft_frame(std::unique_lock<std::mutex>& lk, int64_t deadline_ms) {
    // Bounded, sleeping wait for the shared channel to hold no in-flight request.  ON TRUE THE LOCK
    // STAYS HELD (the caller ships its frame and reads its reply inside the same lock, so no tier
    // can start a request, and no tier's reply can be read, between the check and the pairing
    // test); on false the lock is released and nothing was written.
    const int64_t bound = expert_helper::deadline_after(deadline_ms > 0 ? deadline_ms : kDeadlineDraftMs);
    for (;;) {
        if (fd_ < 0 || !begin_outstanding_) return fd_ >= 0 && !begin_outstanding_;
        if (expert_helper::deadline_after(0) >= bound) return false;   // the bound expired: kErrBusy
        lk.unlock();
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
        lk.lock();
    }
}

int ExpertHelperClient::draft_set_geometry(int64_t n_planes, int64_t taps_hidden) {
    using namespace expert_helper;
    // The bounds are the protocol's own: a plane count the frame's int32 cannot carry, or a hidden
    // width outside the model geometry this transport serves, is refused here and never reaches the
    // peer's geometry check.
    if (n_planes < 1 || n_planes > 4096) return kErrGeometry;
    if (taps_hidden < 1 || taps_hidden > kMaxEmb) return kErrGeometry;
    draft_planes_ = n_planes;
    draft_hidden_ = taps_hidden;
    return kOk;
}

// L8: pack the drafter's fp32 tap rows for the wire - fp32 verbatim (today's bytes), or bf16
// (round-to-nearest-even on the 16-bit truncation) when the caller chose the halved wire.
static void pack_draft_rows(uint8_t* dst, const float* src, size_t n, bool bf16) {
    if (!bf16) {
        std::memcpy(dst, src, n * sizeof(float));
        return;
    }
    uint16_t* out = (uint16_t*) dst;
    for (size_t i = 0; i < n; ++i) {
        uint32_t bits;
        std::memcpy(&bits, src + i, 4);
        bits += 0x7fffu + ((bits >> 16) & 1u);   // round to nearest, ties to even
        out[i] = (uint16_t) (bits >> 16);
    }
}

int ExpertHelperClient::draft_context(const float* rows, int64_t n_rows, int64_t pos0, int reset,
                                      int64_t timeout_ms) {
    using namespace expert_helper;
    if (fd_ < 0) return kErrUnhealthy;
    // The negotiation gate, BEFORE any write (the kBeginP pattern): a peer that never advertised
    // the drafter lane gets a typed refusal and the frame type never reaches it.
    if (!draft_) return kErrRequest;
    if (rows == nullptr) return kErrGeometry;
    if (draft_planes_ <= 0 || draft_hidden_ <= 0) return kErrGeometry;   // draft_set_geometry first
    if (n_rows < 1 || n_rows > kMaxDraftCtxRows * 1024) return kErrGeometry;
    if (pos0 < 0) return kErrGeometry;
    if (reset != 0 && reset != 1) return kErrGeometry;
    if (reset == 1 && pos0 != 0) return kErrGeometry;   // the ring empties at position 0 only
    const int64_t row_floats = draft_planes_ * draft_hidden_;
    // L8 (default OFF): STRATA_DRAFT_TAP_BF16 together with the peer's kFeatureDraftBf16 halves
    // the tap wire - the rows leave as bf16 on the new frame kinds; unset, or a peer without the
    // bit, sends exactly today's fp32 bytes on today's frame kinds.
    static const bool tap_bf16_env = std::getenv("STRATA_DRAFT_TAP_BF16") != nullptr;
    const bool use_bf16 = tap_bf16_env && draft_bf16_;
    int64_t done = 0;
    while (done < n_rows) {
        const int64_t nr = std::min<int64_t>(kMaxDraftCtxRows, n_rows - done);
        const size_t total = sizeof(DraftCtxReq) + (size_t) nr * row_floats * (use_bf16 ? 2u : 4u);
        if (total > kMaxFramePayload) return kErrGeometry;   // a frame the reader would refuse
        std::vector<uint8_t> payload(total, 0);
        DraftCtxReq req;
        req.n_rows = (int32_t) nr;
        req.n_planes = (int32_t) draft_planes_;
        req.taps_hidden = (int32_t) draft_hidden_;
        req.reset = (done == 0) ? reset : 0;   // the request's FIRST frame carries the reset
        req.pos0 = pos0 + done;
        std::memcpy(payload.data(), &req, sizeof(req));
        pack_draft_rows(payload.data() + sizeof(req), rows + (size_t) done * row_floats,
                        (size_t) nr * row_floats, use_bf16);
        // M7 (sect12a, BUG E): ship this frame only when the shared channel holds no in-flight
        // request, and keep io_mtx_ held from the write through the ack's pairing test.  The frame's
        // identity is req_id_, and the ack echoes it; with a layer split the prompt tier's
        // begin_prompt/finish_prompt pair runs on the OTHER stage's thread, so an ungated write
        // here reads back that request's reply - the run-2 fault ("req_id 986 (want 987)").
        std::unique_lock<std::mutex> io_lk(io_mtx_);
        if (!gate_draft_frame(io_lk, timeout_ms)) {
            const int busy_rc = fd_ < 0 ? kErrUnhealthy : kErrBusy;
            std::fprintf(stderr, "expert-helper: the draft ctx frame at pos %lld found an in-flight "
                                 "window/prompt request on the shared channel (a layer's reply is "
                                 "outstanding past the gate's bound): %s; drafting is off for this "
                                 "request and the channel stays open\n", (long long) req.pos0,
                          status_name(busy_rc));
            return busy_rc;
        }
        expert_helper::Socket s;
        s.adopt(fd_);
        const int64_t deadline = rel(timeout_ms, kDeadlineDraftMs);
        int rc = s.write_frame(use_bf16 ? kDraftCtxBf16 : kDraftCtx, ++req_id_, payload.data(), total, deadline);
        if (rc != kOk) {
            std::fprintf(stderr, "expert-helper: draft ctx frame at pos %lld failed to send: %s; "
                                 "closing the channel\n", (long long) req.pos0, status_name(rc));
            s.release(); close(); return rc;
        }
        const uint32_t seq = s.seq_out();
        Header head;
        expert_helper::PayloadView body;
        rc = s.read_frame(head, rx_, body, deadline);
        if (rc == kErrTimeout) {
            ++timeouts_;
            std::fprintf(stderr, "expert-helper: the draft ctx ack at pos %lld missed its %lld ms "
                                 "deadline; closing the channel\n", (long long) req.pos0,
                         (long long) (timeout_ms > 0 ? timeout_ms : kDeadlineDraftMs));
            s.release(); close(); return rc;
        }
        if (rc != kOk) {
            std::fprintf(stderr, "expert-helper: reading the draft ctx ack failed: %s; closing the "
                                 "channel\n", status_name(rc));
            s.release(); close(); return rc;
        }
        if (head.type == kError) {
            ErrResp err{};
            if (body.size() >= sizeof(err)) std::memcpy(&err, body.data(), sizeof(err));
            // M7 (plan sect12/12a, BUG B): an application-level draft refusal is NOT channel-fatal.
            // The draft frames share the channel with the window and prompt tiers; a draft refusal
            // kills ONLY the draft lane for this request (the caller latches drafting off), never
            // the shared channel.  Transport failures (timeout, malformed frame, short read) keep
            // the established fatal rule above.  The server sends the same typed kError and keeps
            // serving, so the window tier's next begin finds the channel alive.
            std::fprintf(stderr, "expert-helper: the helper refused the draft ctx frame at pos %lld: "
                                 "%lld %s; drafting is off for this request (the channel stays open)\n",
                         (long long) req.pos0, (long long) err.status, err.text);
            s.release(); return kErrRequest;
        }
        if (head.type != kDraftAck) {
            std::fprintf(stderr, "expert-helper: unexpected draft ctx reply frame type %u; closing "
                                 "the channel\n", head.type);
            s.release(); close(); return kErrProtocol;
        }
        if (head.req_id != req_id_ || head.seq != seq) {
            std::fprintf(stderr, "expert-helper: draft ctx reply rejected: req_id %u (want %u) seq "
                                 "%u (want %u); closing the channel\n", head.req_id, req_id_,
                         head.seq, seq);
            s.release(); close(); return kErrSeq;
        }
        if (!body.empty()) {   // the protocol's own ack shape is the empty payload
            std::fprintf(stderr, "expert-helper: draft ctx ack carries %zu bytes of payload; "
                                 "closing the channel\n", body.size());
            s.release(); close(); return kErrShort;
        }
        ++draft_frames_;
        draft_rows_ += (uint64_t) nr;
        s.release();
        done += nr;
        // The harness counts the frames from this line (docs/work/MIXED_BACKEND_RPC_PLAN.md sect12
        // evidence): one line per kDraftCtx frame that flowed.
        std::fprintf(stderr, "expert-helper: draft ctx pos %lld rows %lld (frames %llu, rows %llu)\n",
                     (long long) req.pos0, (long long) nr, (unsigned long long) draft_frames_,
                     (unsigned long long) draft_rows_);
    }
    return kOk;
}

int ExpertHelperClient::draft_round(const float* rows, int64_t n_new, int64_t pos0, int32_t anchor,
                                    int32_t* out_picks, float* out_probs, int32_t out_cap,
                                    int64_t timeout_ms, int32_t& n_out) {
    using namespace expert_helper;
    n_out = 0;
    if (fd_ < 0) return kErrUnhealthy;
    if (!draft_) return kErrRequest;                 // the peer never advertised the drafter lane
    if (rows == nullptr || out_picks == nullptr) return kErrGeometry;
    if (draft_planes_ <= 0 || draft_hidden_ <= 0) return kErrGeometry;
    if (n_new < 1 || n_new > kMaxDraftRound) return kErrGeometry;   // the committed window rows
    if (pos0 < 0) return kErrGeometry;
    if (anchor < 0) return kErrGeometry;
    // out_cap rides the request (n_drafts) so the helper never proposes wider than the caller's
    // buffers, AND is checked against the REPLY's pick count below (a wider reply is still
    // refused by name, nothing written) - belt and braces after l1w2's 7-vs-5 mismatch, which
    // was fatal only because the request could not carry the cap.
    if (out_cap < 1 || out_cap > kMaxDraftPicks) return kErrGeometry;
    const int64_t row_floats = draft_planes_ * draft_hidden_;
    // L8 (default OFF): the same gate as draft_context - the env AND the peer's feature bit.
    static const bool tap_bf16_env = std::getenv("STRATA_DRAFT_TAP_BF16") != nullptr;
    const bool use_bf16 = tap_bf16_env && draft_bf16_;
    const size_t total = sizeof(DraftReq) + (size_t) n_new * row_floats * (use_bf16 ? 2u : 4u);
    if (total > kMaxFramePayload) return kErrGeometry;
    std::vector<uint8_t> payload(total, 0);
    DraftReq req;
    req.n_new = (int32_t) n_new;
    req.n_planes = (int32_t) draft_planes_;
    req.taps_hidden = (int32_t) draft_hidden_;
    req.anchor = anchor;
    req.pos0 = pos0;
    req.n_drafts = out_cap;
    std::memcpy(payload.data(), &req, sizeof(req));
    pack_draft_rows(payload.data() + sizeof(req), rows, (size_t) n_new * row_floats, use_bf16);
    // M7 (sect12a, BUG E): the same gate and the same held lock as draft_context - this frame
    // shares the channel with the pipelined window/prompt tiers, and its reply is paired by
    // req_id, so it goes on the wire only with nothing else in flight.
    std::unique_lock<std::mutex> io_lk(io_mtx_);
    if (!gate_draft_frame(io_lk, timeout_ms)) {
        const int busy_rc = fd_ < 0 ? kErrUnhealthy : kErrBusy;
        std::fprintf(stderr, "expert-helper: the draft round at pos %lld found an in-flight "
                             "window/prompt request on the shared channel (a layer's reply is "
                             "outstanding past the gate's bound): %s; drafting is off for this "
                             "request and the channel stays open\n", (long long) pos0,
                      status_name(busy_rc));
        return busy_rc;
    }
    expert_helper::Socket s;
    s.adopt(fd_);
    const int64_t deadline = rel(timeout_ms, kDeadlineDraftMs);
    int rc = s.write_frame(use_bf16 ? kDraftReqBf16 : kDraftReq, ++req_id_, payload.data(), total, deadline);
    if (rc != kOk) {
        std::fprintf(stderr, "expert-helper: draft round at pos %lld failed to send: %s; closing "
                             "the channel\n", (long long) pos0, status_name(rc));
        s.release(); close(); return rc;
    }
    const uint32_t seq = s.seq_out();
    Header head;
    expert_helper::PayloadView body;
    rc = s.read_frame(head, rx_, body, deadline);
    if (rc == kErrTimeout) {
        ++timeouts_;
        std::fprintf(stderr, "expert-helper: the draft round reply at pos %lld missed its %lld ms "
                             "deadline; closing the channel\n", (long long) pos0,
                     (long long) (timeout_ms > 0 ? timeout_ms : kDeadlineDraftMs));
        s.release(); close(); return rc;
    }
    if (rc != kOk) {
        std::fprintf(stderr, "expert-helper: reading the draft round reply failed: %s; closing the "
                             "channel\n", status_name(rc));
        s.release(); close(); return rc;
    }
    if (head.type == kError) {
        ErrResp err{};
        if (body.size() >= sizeof(err)) std::memcpy(&err, body.data(), sizeof(err));
        // M7 (plan sect12/12a, BUG B): see draft_context - a draft refusal disables ONLY the draft
        // lane for this request and leaves the shared channel open (the server sends the typed
        // kError and keeps serving); transport failures keep the fatal rule above.
        std::fprintf(stderr, "expert-helper: the helper refused the draft round at pos %lld: "
                             "%lld %s; drafting is off for this request (the channel stays open)\n",
                     (long long) pos0, (long long) err.status, err.text);
        s.release(); return kErrRequest;
    }
    if (head.type != kDraftReply) {
        std::fprintf(stderr, "expert-helper: unexpected draft round reply frame type %u; closing "
                             "the channel\n", head.type);
        s.release(); close(); return kErrProtocol;
    }
    if (head.req_id != req_id_ || head.seq != seq) {
        std::fprintf(stderr, "expert-helper: draft round reply rejected: req_id %u (want %u) seq "
                             "%u (want %u); closing the channel\n", head.req_id, req_id_,
                     head.seq, seq);
        s.release(); close(); return kErrSeq;
    }
    // Validate the WHOLE reply before writing a single pick: a violation applies NOTHING.
    if (body.size() < sizeof(DraftReply)) { s.release(); close(); return kErrShort; }
    DraftReply rep;
    std::memcpy(&rep, body.data(), sizeof(rep));
    if (rep.n_drafts < 1 || rep.n_drafts > kMaxDraftPicks || rep.reserved0 != 0 ||
        body.size() != sizeof(rep) + (size_t) rep.n_drafts * 8u ||
        rep.n_drafts > out_cap) {
        std::fprintf(stderr, "expert-helper: draft round reply malformed (n_drafts %d, %zu body "
                             "bytes, cap %d); closing the channel\n", rep.n_drafts, body.size(),
                     out_cap);
        s.release(); close(); return kErrShort;
    }
    const int32_t* picks = (const int32_t*) (body.data() + sizeof(rep));
    const float* probs = (const float*) (body.data() + sizeof(rep) + (size_t) rep.n_drafts * 4u);
    for (int32_t i = 0; i < rep.n_drafts; ++i)
        if (!std::isfinite(probs[i])) {
            std::fprintf(stderr, "expert-helper: draft round reply carries a non-finite "
                                 "probability; closing the channel\n");
            s.release(); close(); return kErrNonFinite;
        }
    std::memcpy(out_picks, picks, (size_t) rep.n_drafts * sizeof(int32_t));
    if (out_probs != nullptr) std::memcpy(out_probs, probs, (size_t) rep.n_drafts * sizeof(float));
    n_out = rep.n_drafts;
    ++draft_rounds_;
    // The harness counts the rounds from this line: one line per served kDraftReq.
    std::fprintf(stderr, "expert-helper: draft round pos %lld -> %d drafts (rounds %llu)\n",
                 (long long) (pos0 + n_new), rep.n_drafts, (unsigned long long) draft_rounds_);
    s.release();
    return kOk;
}

}  // namespace strata::core

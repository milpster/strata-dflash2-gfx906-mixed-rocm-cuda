// tests/core/dflash_owned_kv_test.cpp - the DFlash drafter's OWNED K/V state (docs/DFLASH.md):
// the pools must live inside the caller's arena, whole-resident, fp16, identity page table - and
// kv_append_step must land every cell at the physical row the identity layout names, exactly.
//
// Independent of the target model, the Verifier, the DFlash layers and attention: this only
// exercises qsa_state_bytes/qsa_state_init/qsa_state_zero/kv_append_step on one owned state
// (and then on five, one per draft layer).
//
//   1. ownership: k_pool/v_pool inside the arena; kv_elastic -1; kv_mode 0; fp16; no host copy;
//      identity page table;
//   2. append/readback at positions crossing page boundaries (page_size 4): exact fp16 bits
//      against the host's f16_from_f32 of the deterministic inputs;
//   3. elastic non-membership: enabling the process elastic K/V and growing/shrinking the global
//      pools leaves the owned state's bytes untouched;
//   4. five states, one per draft layer, each independently owned and independent.
//   5. bounded SWA ring: wrap-safe appends, ring-mapped attention at absolute positions,
//      absolute rope positions, and the host horizon detecting stale deep-rewind windows.
#include "strata/core/layer.hpp"
#include "strata/core/dflash.hpp"
#include "strata/core/dflash_ring.hpp"
#include "strata/core/session.hpp"
#include "strata/kernels/f16_bits.hpp"
#include "strata/kernels/dflash_inco.hpp"
#include "strata/kernels/qsa.hpp"
#include "strata/kernels/qsa_decode_attn.hpp"
#include "strata/spec/dflash_policy.hpp"
#include "strata/kernels/native_rope.hpp"
#include "strata/core/vmm.hpp"
#include "strata/core/conversation_snapshot.hpp"

#include <cuda_runtime.h>

#include <cmath>
#include <cstdio>
#include <vector>

using namespace strata::core;

namespace {
int g_fail = 0;
void check(bool ok, const char* what) {
    if (!ok) { std::fprintf(stderr, "FAIL: %s\n", what); ++g_fail; }
}

// deterministic cell content: k(cell, kvh, d) — fp16-exact values (small halves)
float kval(int64_t cell, int kvh, int d) {
    return (float) ((int) ((cell * 7 + kvh * 131 + d) % 61) - 30) / 4.0f;
}
float vval(int64_t cell, int kvh, int d) {
    return (float) ((int) ((cell * 13 + kvh * 57 + d) % 47) - 23) / 8.0f;
}

struct Owned {
    void* arena = nullptr;
    uint64_t bytes = 0;
    QsaState st = {};
    bool init(const ModelGeometry& g, int64_t max_cells, std::vector<int32_t>* table_out = nullptr, int64_t elastic_init = 0) {
        QsaStateInitOptions o;
        o.force_owned_kv = true;    // the pools are carved from OUR arena, whole-resident
        o.force_f16_kv = true;      // FP16 regardless of the process KV format
        o.disable_elastic = elastic_init == 0;
        o.elastic_init_cells = elastic_init;
        o.no_indexer = elastic_init > 0;
        o.disable_streaming = true;
        // with_rope=true: this state owns its rope tables (share_rope stays null) - bytes() and
        // init() must agree on that, or the tables' upload writes past the arena
        bytes = qsa_state_bytes(g, max_cells, /*with_rope=*/true, /*ring_cells=*/0, o);
        if (cudaMalloc(&arena, bytes) != cudaSuccess) return false;
        if (qsa_state_init(g, max_cells, arena, st, nullptr, /*ring_cells=*/0, o) == 0) return false;
        qsa_state_zero(st, g, nullptr);
        cudaDeviceSynchronize();
        if (table_out) {
            std::vector<int32_t> t((size_t) st.n_pages);
            if (cudaMemcpy(t.data(), st.page_table, t.size() * 4, cudaMemcpyDeviceToHost) != cudaSuccess)
                return false;
            *table_out = t;
        }
        return true;
    }
};

void check_ownership(const Owned& o, const char* tag) {
    const uintptr_t a0 = reinterpret_cast<uintptr_t>(o.arena), a1 = a0 + o.bytes;
    const auto inside = [&](const void* p) { return reinterpret_cast<uintptr_t>(p) >= a0 && reinterpret_cast<uintptr_t>(p) < a1; };
    char what[160];
    std::snprintf(what, sizeof what, "%s: k_pool/v_pool inside the arena", tag);
    check(inside(o.st.k_pool) && inside(o.st.v_pool), what);
    std::snprintf(what, sizeof what, "%s: kv_elastic -1, kv_mode 0, fp16, no host copy, no map", tag);
    check(o.st.kv_elastic == -1 && o.st.kv_mode == 0 && !o.st.kv_int8 && !o.st.kv_q4 && !o.st.kv_hybrid &&
              o.st.host.k_pool == nullptr && o.st.map.slot_block == nullptr,
          what);
}

void append_and_readback(const Owned& o, const ModelGeometry& g, const std::vector<int32_t>& table) {
    const strata::kernels::QsaShapes s = strata::kernels::qsa_real_shapes();
    const int page_size = (int) s.page_size, nkv = (int) s.n_head_kv, hd = (int) s.head_dim;
    cudaStream_t cs = nullptr;
    cudaStreamCreate(&cs);
    const int64_t max_cells = o.st.max_cells;
    std::vector<float> kcur((size_t) nkv * hd), vcur((size_t) nkv * hd);
    int32_t* step_dev = nullptr;
    cudaMalloc(&step_dev, 16);
    for (int64_t cell : {int64_t{0}, int64_t{1}, int64_t{3}, int64_t{4}, int64_t{15}, int64_t{16}, int64_t{17},
                     int64_t{31}, int64_t{32}, int64_t{47}, int64_t{63}, int64_t{64}, int64_t{65},
                     (int64_t) max_cells - 1}) {
        if (cell >= max_cells) continue;
        for (int h = 0; h < nkv * hd; ++h) {
            kcur[(size_t) h] = kval(cell, h / hd, h % hd);
            vcur[(size_t) h] = vval(cell, h / hd, h % hd);
        }
        float *kd = nullptr, *vd = nullptr;
        cudaMalloc(&kd, kcur.size() * 4);
        cudaMalloc(&vd, vcur.size() * 4);
        cudaMemcpyAsync(kd, kcur.data(), kcur.size() * 4, cudaMemcpyHostToDevice, cs);
        cudaMemcpyAsync(vd, vcur.data(), vcur.size() * 4, cudaMemcpyHostToDevice, cs);
        const int32_t step[4] = {(int32_t) cell, (int32_t)(cell + 1), (int32_t)((cell + 1) / page_size),
                                 (int32_t)(cell + 1)};
        cudaMemcpyAsync(step_dev, step, sizeof step, cudaMemcpyHostToDevice, cs);
        (void) cudaGetLastError();
        kv_append_step(o.st.k_pool, o.st.v_pool, o.st.page_table, step_dev, kd, vd, s, nullptr, nullptr);
        const cudaError_t append_err = cudaGetLastError();
        if (append_err != cudaSuccess)
            std::fprintf(stderr, "dbg: append launch failed: %s (cell %lld, k_pool %p, table %p, step %p)\n",
                         cudaGetErrorString(append_err), (long long) cell, (void*) o.st.k_pool,
                         (void*) o.st.page_table, (void*) step_dev);
        cudaStreamSynchronize(cs);
        cudaFree(kd);
        cudaFree(vd);
        // read back every kv head and every dimension, exactly
        for (int h = 0; h < nkv; ++h) {
            const int64_t page = table[(size_t)(cell / page_size)];
            const int64_t r = (page * nkv + h) * page_size + cell % page_size;
            std::vector<uint16_t> got((size_t) hd);
            cudaMemcpyAsync(got.data(), o.st.k_pool + (size_t) r * hd, (size_t) hd * 2, cudaMemcpyDeviceToHost, cs);
            cudaStreamSynchronize(cs);
            for (int d = 0; d < hd; ++d) {
                const uint16_t want = strata::kernels::f16_from_f32(kval(cell, h, d));
                if (got[(size_t) d] != want) {
                    std::fprintf(stderr, "FAIL: k cell %lld h %d d %d: got %04x want %04x\n", (long long) cell, h, d,
                                 got[(size_t) d], want);
                    ++g_fail;
                    h = nkv;
                    break;
                }
            }
            cudaMemcpyAsync(got.data(), o.st.v_pool + (size_t) r * hd, (size_t) hd * 2, cudaMemcpyDeviceToHost, cs);
            cudaStreamSynchronize(cs);
            for (int d = 0; d < hd; ++d) {
                const uint16_t want = strata::kernels::f16_from_f32(vval(cell, h, d));
                if (got[(size_t) d] != want) {
                    std::fprintf(stderr, "FAIL: v cell %lld h %d d %d: got %04x want %04x\n", (long long) cell, h, d,
                                 got[(size_t) d], want);
                    ++g_fail;
                    h = nkv;
                    break;
                }
            }
        }
    }
    cudaFree(step_dev);
    cudaStreamDestroy(cs);
}

// Compare batched appends and active attention with the existing selected-cell
// implementation, across page/chunk boundaries and every supported draft width.
void batch_and_attention_regression(const ModelGeometry& g) {
    using namespace strata::kernels;
    const QsaShapes s = qsa_real_shapes();
    const int cap = 8256, qw = s.n_head * s.head_dim, kvw = s.n_head_kv * s.head_dim;
    Owned o;
    check(o.init(g, cap), "batch regression pool init");
    cudaStream_t cs;
    cudaStreamCreateWithFlags(&cs, cudaStreamNonBlocking);
    float *kd, *vd, *qd, *old_out, *new_out, *scratch;
    int32_t *steps, *positions, *ids;
    cudaMalloc(&kd, cap * kvw * sizeof(float));
    cudaMalloc(&vd, cap * kvw * sizeof(float));
    cudaMalloc(&qd, 7 * qw * sizeof(float));
    cudaMalloc(&old_out, 7 * qw * sizeof(float));
    cudaMalloc(&new_out, 7 * qw * sizeof(float));
    cudaMalloc(&scratch, 7 * qsa_decode_attn_scratch_floats(cap, s) * sizeof(float));
    cudaMalloc(&steps, cap * 4 * sizeof(int32_t));
    cudaMalloc(&positions, 7 * s.n_head * sizeof(int32_t));
    cudaMalloc(&ids, 7 * cap * sizeof(int32_t));
    std::vector<float> kh(cap * kvw), vh(kh.size()), qh(7 * qw);
    for (int c = 0; c < cap; ++c)
        for (int i = 0; i < kvw; ++i) {
            kh[c * kvw + i] = kval(c, i / s.head_dim, i % s.head_dim);
            vh[c * kvw + i] = vval(c, i / s.head_dim, i % s.head_dim);
        }
    for (size_t i = 0; i < qh.size(); ++i) qh[i] = (int(i % 29) - 14) * 0.03125f;
    std::vector<int32_t> ih(7 * cap);
    for (size_t i = 0; i < ih.size(); ++i) ih[i] = i % cap;
    cudaMemcpyAsync(kd, kh.data(), kh.size() * 4, cudaMemcpyHostToDevice, cs);
    cudaMemcpyAsync(vd, vh.data(), vh.size() * 4, cudaMemcpyHostToDevice, cs);
    cudaMemcpyAsync(qd, qh.data(), qh.size() * 4, cudaMemcpyHostToDevice, cs);
    cudaMemcpyAsync(ids, ih.data(), ih.size() * 4, cudaMemcpyHostToDevice, cs);
    dflash_build_steps(steps, cap, 0, s.page_size, cs);
    kv_append_f16_steps(o.st.k_pool, o.st.v_pool, o.st.page_table, steps, 4, kd, vd, kvw, cap, s, cs);
    cudaStreamSynchronize(cs);
    std::vector<uint16_t> pool(cap * kvw);
    for (bool value : {false, true}) {
        cudaMemcpy(pool.data(), value ? o.st.v_pool : o.st.k_pool, pool.size() * 2, cudaMemcpyDeviceToHost);
        for (int c = 0; c < cap; ++c)
            for (int h = 0; h < s.n_head_kv; ++h)
                for (int d = 0; d < s.head_dim; ++d) {
                    const int row = (c / s.page_size * s.n_head_kv + h) * s.page_size + c % s.page_size;
                    if (pool[row * s.head_dim + d] != f16_from_f32(value ? vval(c, h, d) : kval(c, h, d))) {
                        check(false, "batched append FP16 bits and page layout");
                        goto checked;
                    }
                }
        checked:;
    }
    QsaAttnPools pools;
    pools.k_pool = o.st.k_pool; pools.v_pool = o.st.v_pool; pools.page_table = o.st.page_table;
    for (int k : {2, 3, 4, 5, 6, 7}) {
        dflash_build_positions(positions, k, s.n_head, 65, cs);
        std::vector<int32_t> ph(k * s.n_head);
        cudaMemcpyAsync(ph.data(), positions, ph.size() * 4, cudaMemcpyDeviceToHost, cs);
        cudaStreamSynchronize(cs);
        for (size_t i = 0; i < ph.size(); ++i) check(ph[i] == 65 + int(i) / s.n_head, "device head positions");
        for (int active : {1, 63, 64, 65, 120, 512, 2048, 8192}) {
            dflash_build_attn_steps(steps, k, active, s.page_size, cs);
            qsa_decode_attn_batch(qd, pools, ids, steps, cap, s, scratch, old_out, k, cs);
            dflash_attn_batch(qd, pools, steps, active, cap, s, scratch, new_out, k, cs);
            std::vector<float> a(k * qw), b(k * qw);
            cudaMemcpyAsync(a.data(), old_out, a.size() * 4, cudaMemcpyDeviceToHost, cs);
            cudaMemcpyAsync(b.data(), new_out, b.size() * 4, cudaMemcpyDeviceToHost, cs);
            cudaStreamSynchronize(cs);
            double maxerr = 0;
            for (size_t i = 0; i < a.size(); ++i) {
                check(std::isfinite(b[i]), "active attention finite");
                maxerr = std::max(maxerr, double(std::abs(a[i] - b[i])));
            }
            if (maxerr > 1e-5) {
                std::fprintf(stderr, "active attention K=%d cells=%d maxerr=%.9g\n", k, active, maxerr);
                check(false, "active vs selected attention tolerance 1e-5");
            }
            const int padded = (int) strata::spec::dflash_attention_bucket(active, cap);
            dflash_attn_batch(qd, pools, steps, padded, cap, s, scratch, new_out, k, cs);
            cudaMemcpyAsync(a.data(), new_out, a.size() * 4, cudaMemcpyDeviceToHost, cs);
            cudaStreamSynchronize(cs);
            check(a == b, "padded attention preserves every output bit");
        }
    }
    for (void* p : {static_cast<void*>(kd), static_cast<void*>(vd), static_cast<void*>(qd),
                   static_cast<void*>(old_out), static_cast<void*>(new_out), static_cast<void*>(scratch),
                   static_cast<void*>(steps), static_cast<void*>(positions), static_cast<void*>(ids)}) cudaFree(p);
    cudaStreamDestroy(cs);
    cudaFree(o.arena);
}
void owned_elastic_regression(const ModelGeometry& g) {
    if (!vmm_available()) return;
    qsa_set_kv_elastic(false, 0);   // target is streamed; only this state opts into VMM
    qsa_set_kv_resident(32768);
    qsa_set_kv_q4(true);            // the draft remains FP16, including byte sizing
    const int64_t cap = 32768;
    Owned o;
    std::vector<int32_t> table;
    check(o.init(g, cap, &table, 512), "explicit owned elastic init");
    check(!qsa_kv_elastic() && o.st.kv_elastic >= 0 && o.st.kv_mode == 0 && !o.st.kv_q4 && !o.st.kv_int8,
          "draft VMM independent of target streaming and KV format");
    check(o.st.idx_pooled_rows == 0, "no unused sparse-indexer history");
    const auto *k = o.st.k_pool, *v = o.st.v_pool;
    const uint64_t initial = qsa_state_elastic_bytes(o.st);
    check(initial > 0 && initial < qsa_kv_elastic_full_bytes(), "physical allocation follows live cells");
    check(qsa_kv_elastic_need(cap) > 0, "growth requires more chunks");
    check(qsa_kv_elastic_grow(cap, [] { return VmmChunk{}; }), "owned elastic growth");
    check(k == o.st.k_pool && v == o.st.v_pool, "growth preserves pool addresses");
    append_and_readback(o, g, table);
    std::vector<uint16_t> before(256), after(256);
    check(cudaMemcpy(before.data(), k, 512, cudaMemcpyDeviceToHost) == cudaSuccess, "read committed prefix");
    std::vector<VmmChunk> returned;
    check(qsa_kv_elastic_shrink(4096, [&](VmmChunk h) { returned.push_back(h); }) > 0, "shrink returns physical chunks");
    check(cudaMemcpy(after.data(), k, 512, cudaMemcpyDeviceToHost) == cudaSuccess && before == after,
          "shrink preserves committed prefix");
    check(qsa_kv_elastic_grow(cap, [&] {
        if (returned.empty()) return VmmChunk{};
        const auto h = returned.back(); returned.pop_back(); return h;
    }), "regrowth reuses returned chunks");
    append_and_readback(o, g, table);
    check(cudaDeviceSynchronize() == cudaSuccess, "owned elastic operations completed");
    qsa_state_release_elastic(o.st);
    check(qsa_kv_elastic_mapped_bytes() == 0 && o.st.kv_elastic == -1, "owned elastic release");
    for (auto h : returned) vmm_chunk_free(h);
    cudaFreeHost(o.st.host_step); cudaFreeHost(o.st.host_pos);
    cudaFree(o.arena);
    qsa_set_kv_resident(0); qsa_set_kv_q4(false);
}
void conversation_regression(const ModelGeometry& g) {
    // Five distinct FP16 pools, a partial last page, then another conversation.
    // A rewind must recover the saved prefix in every layer without an indexer.
    std::vector<Owned> layers(5);
    std::vector<ConversationKv> saved(5);
    std::string err;
    for (size_t i = 0; i < layers.size(); ++i) {
        auto& o = layers[i];
        check(o.init(g, 128), "conversation pool init");
        const size_t bytes = 68 * g.n_head_kv * g.head_dim * sizeof(uint16_t);
        std::vector<uint16_t> data(bytes / 2, (uint16_t) (0x3000 + i));
        cudaMemcpy(o.st.k_pool, data.data(), bytes, cudaMemcpyHostToDevice);
        cudaMemcpy(o.st.v_pool, data.data(), bytes, cudaMemcpyHostToDevice);
        size_t estimate = 0;
        check(conversation_kv_capture_bytes({}, o.st, g, 65, false, estimate, err), "draft cache estimate");
        check(conversation_kv_save(saved[i], o.st, g, 65, false, err), "draft cache save");
        check(saved[i].bytes() <= estimate, "draft cache accounting");
        check(saved[i].pooled.bytes() == 0, "draft cache has no indexer");
        cudaMemset(o.st.k_pool, 0, bytes);
        cudaMemset(o.st.v_pool, 0, bytes);
    }
    for (size_t i = 0; i < layers.size(); ++i) {
        auto& o = layers[i];
        check(conversation_kv_validate(saved[i], o.st, g, 65, false, err), "draft cache validate");
        auto invalid = g;
        invalid.head_dim /= 2;
        check(!conversation_kv_validate(saved[i], o.st, invalid, 65, false, err), "foreign draft cache rejected");
        check(conversation_kv_restore(saved[i], o.st, g, 65, false, err), "draft cache restore");
        uint64_t hash = 0;
        check(conversation_kv_verify(saved[i], o.st, g, 65, false, hash, err), "draft cache exact readback");
        std::vector<uint16_t> data(64 * g.n_head_kv * g.head_dim);
        cudaMemcpy(data.data(), o.st.k_pool, data.size() * 2, cudaMemcpyDeviceToHost);
        bool same = true;
        for (auto x : data) same = same && x == (uint16_t) (0x3000 + i);
        check(same, "draft cache layers remain distinct on rewind");
        cudaFreeHost(o.st.host_step); cudaFreeHost(o.st.host_pos);
        cudaFree(o.arena);
    }
}
// The bounded SWA ring (include/strata/core/dflash_ring.hpp): append steps map their cells
// through `cell % ring`, attention reads through the same modulo, and the RoPE positions stay
// absolute. Small-scale mirror of the runtime on the test geometry: window 24, 6 rows, page 4,
// ring 32 - appends wrap three times, attention spans the wraps, stale windows are detected.
// Gated with the DFlash2 runtime (the attention entry point it builds).
#ifdef STRATA_ENABLE_DFLASH2
void ring_regression(const ModelGeometry& g) {
    using namespace strata::kernels;
    // L12 fixture fix (window-2 GPU FAIL "ring attention reads the wrapped rows"): this
    // section used qsa_real_shapes() - the TARGET shapes (24Q/2KV x head_dim 256) - to
    // allocate pool/query and build the reference, while dflash2_attention IS the drafter's
    // attention128_kernel, whose head_dim is fixed at 128 ("Non-causal local attention for
    // the official 32Q/8KV x 128 drafter", include/strata/kernels/dflash_inco.hpp:31-38;
    // src/kernels/cuda/dflash_inco.cu:257-335 reads q[...*128+d], k[poolrow*128+d]). A
    // head_dim-256 layout can never match a 128-wide kernel: a guaranteed fixture
    // mismatch, not kernel evidence. The runtime's drafter pool uses shapes_of(pool_g_) =
    // qsa_shapes(drafter geometry) - 32Q/8KV/128, page 4 (src/core/dflash_runtime.cpp:
    // 405-454; include/strata/core/layout.hpp:29), exactly the geometry dflash2_parity
    // validates (hd=128, heads 32/kv 8). This section now uses the same geometry.
    ModelGeometry dg = g;
    dg.n_head = 32; dg.n_head_kv = 8; dg.head_dim = 128;
    const QsaShapes s = qsa_shapes(dg);   // the shapes the runtime hands the kernel
    const int hd = (int) s.head_dim, nq = (int) s.n_head, nk = (int) s.n_head_kv, page = (int) s.page_size;
    // Fixture assertions BEFORE the kernel call (CPU-visible): the drafter geometry the
    // kernel hardcodes, plus the group/page facts its entry checks enforce.
    check(nq == 32 && nk == 8 && hd == 128, "ring fixture pins the drafter geometry 32Q/8KV x 128 before the kernel call");
    check(page == 4 && nq % nk == 0, "ring fixture page size and Q/KV group match the kernel entry checks");
    const int64_t ring = dflash_ring_capacity(24, 6, page);   // 29 -> 32 cells
    const int rows = 6, window = 24;
    check(ring % page == 0 && ring >= window + rows - 1,
          "ring is whole pages and covers the widest readable span (kernel-enforced, asserted here too)");
    const int kvw = nk * hd, qw = nq * hd;
    Owned o;
    std::vector<int32_t> table;
    check(o.init(dg, ring, &table), "ring pool init");   // capture the REAL page table for the reference
    check(o.st.kv_mode == 0 && o.st.n_slots == ring / page, "ring is a mode-0 pool of ring pages");
    check((int64_t) table.size() == ring / page, "captured page table covers every ring page");
    cudaStream_t cs;
    cudaStreamCreateWithFlags(&cs, cudaStreamNonBlocking);
    float *kd, *vd, *qd, *out, *scratch;
    int32_t *steps, *attn, *positions;
    cudaMalloc(&kd, ring * kvw * sizeof(float));
    cudaMalloc(&vd, ring * kvw * sizeof(float));
    cudaMalloc(&qd, rows * qw * sizeof(float));
    cudaMalloc(&out, rows * qw * sizeof(float));
    cudaMalloc(&scratch, (size_t) rows * nq * ((window + rows + 63) / 64) * 130 * sizeof(float));
    cudaMalloc(&steps, rows * 4 * sizeof(int32_t));
    cudaMalloc(&attn, rows * 4 * sizeof(int32_t));
    cudaMalloc(&positions, (size_t) rows * nq * sizeof(int32_t));
    std::vector<float> qh((size_t) qw * rows);
    for (size_t i = 0; i < qh.size(); ++i) qh[i] = (int(i % 29) - 14) * 0.03125f;
    cudaMemcpyAsync(qd, qh.data(), qh.size() * 4, cudaMemcpyHostToDevice, cs);
    // Three full wraps: each round appends `ring` cells at [base, base+ring) and the pool must
    // hold the NEWEST round's content at every mapped row (wrap-safe, duplicate-free writes).
    for (int64_t base : {int64_t{0}, ring, 2 * ring}) {
        std::vector<float> kh((size_t) ring * kvw), vh((size_t) ring * kvw);
        for (int64_t c = 0; c < ring; ++c)
            for (int i = 0; i < kvw; ++i) {
                kh[(size_t) c * kvw + i] = kval(base + c, (int) (i / hd), (int) (i % hd));
                vh[(size_t) c * kvw + i] = vval(base + c, (int) (i / hd), (int) (i % hd));
            }
        cudaMemcpyAsync(kd, kh.data(), kh.size() * 4, cudaMemcpyHostToDevice, cs);
        cudaMemcpyAsync(vd, vh.data(), vh.size() * 4, cudaMemcpyHostToDevice, cs);
        dflash_build_steps(steps, (int) ring, base, page, cs, ring);
        kv_append_f16_steps(o.st.k_pool, o.st.v_pool, o.st.page_table, steps, 4, kd, vd, kvw,
                            (int) ring, s, cs);
        cudaStreamSynchronize(cs);
        std::vector<uint16_t> pool((size_t) ring * kvw);
        for (bool value : {false, true}) {
            cudaMemcpy(pool.data(), value ? o.st.v_pool : o.st.k_pool, pool.size() * 2, cudaMemcpyDeviceToHost);
            for (int64_t c = 0; c < ring; ++c) {
                const int64_t pc = dflash_ring_cell(base + c, ring);
                const int row = (int) (pc / page * nk) * page + (int) pc % page;
                for (int h = 0; h < nk; ++h)
                    for (int d = 0; d < hd; ++d)
                        if (pool[((size_t) row + h * page) * hd + d] !=
                            f16_from_f32(value ? vval(base + c, h, d) : kval(base + c, h, d))) {
                            check(false, "ring wrap append lands at the mapped row");
                            return;
                        }
            }
        }
    }
    // Absolute RoPE positions are untouched by the ring mapping.
    dflash_build_positions(positions, rows, nq, 70, cs);
    std::vector<int32_t> ph((size_t) rows * nq);
    cudaMemcpyAsync(ph.data(), positions, ph.size() * 4, cudaMemcpyDeviceToHost, cs);
    cudaStreamSynchronize(cs);
    for (size_t i = 0; i < ph.size(); ++i)
        check(ph[i] == 70 + (int) (i / nq), "ring keeps absolute rope positions");

    const int64_t through = 3 * ring;   // newest append end after the three wraps
    check(!dflash_ring_retained(70, rows, window, ring, through),
          "host horizon flags the stale deep-rewind window");
    check(dflash_ring_retained(through - rows, rows, window, ring, through),
          "host horizon keeps an in-place propose");

    // L12 reference rewrite: capture the FP16 pool bytes ONCE after sync (bulk D2H) and
    // compute the host softmax from those bytes at the physical rows the ring mapping +
    // CAPTURED page table name. No cudaMemcpy inside the (row,head,dim,cell) loop (the
    // window-2 version did 737k tiny PCIe transfers - unsuitable on gfx906). Two cases:
    // the deliberate stale deep-rewind window (rows hold the NEWEST wrap's bytes, the
    // mapping dflash_ring_retained refuses before proposing) and the retained in-place
    // window (rows hold the logical cells - the valid propose case). Threshold 1e-4 kept.
    const auto attention_case = [&](int64_t pos, const char* tag, const char* what) {
        const int64_t end = pos + rows;
        dflash_build_attn_steps(attn, rows, end, page, cs);
        dflash2_attention(qd, o.st.k_pool, o.st.v_pool, o.st.page_table, attn, out, scratch, rows,
                          nq, nk, page, window + rows, window, cs, (int) ring);
        cudaStreamSynchronize(cs);
        std::vector<float> got((size_t) rows * qw);
        cudaMemcpy(got.data(), out, got.size() * 4, cudaMemcpyDeviceToHost);
        std::vector<uint16_t> kp((size_t) ring * kvw), vp((size_t) ring * kvw);
        cudaMemcpy(kp.data(), o.st.k_pool, kp.size() * 2, cudaMemcpyDeviceToHost);
        cudaMemcpy(vp.data(), o.st.v_pool, vp.size() * 2, cudaMemcpyDeviceToHost);
        const int group = nq / nk;
        const auto row_of = [&](int64_t c, int h) {
            const int64_t pc = dflash_ring_cell(c, ring);
            const int64_t phys = table[pc / page];   // the REAL page table, captured at init
            return (phys * nk + h / group) * page + pc % page;
        };
        const int64_t lo = std::max<int64_t>(0, pos - window + 1);
        bool ok = true;
        double first_abs = 0, max_abs = 0, max_rel = 0, f_want = 0, f_have = 0;
        int fr = -1, fh = -1, fd = -1;
        for (int r = 0; r < rows && ok; ++r) for (int h = 0; h < nq && ok; ++h) {
            std::vector<double> scores((size_t) end, -INFINITY);
            double m = -INFINITY;
            for (int64_t c = lo; c < end; ++c) {
                if (std::abs(c - (end - rows + r)) >= window) continue;
                const uint16_t* krow = kp.data() + (size_t) row_of(c, h) * hd;
                const float* qrow = qh.data() + ((size_t) r * nq + h) * hd;
                double sc = 0;
                for (int d = 0; d < hd; ++d)
                    sc += (double) qrow[d] * __half2float(__ushort_as_half(krow[d]));
                scores[(size_t) c] = sc / std::sqrt((double) hd);
                m = std::max(m, scores[(size_t) c]);
            }
            double sum = 0;
            for (double& x : scores) { x = std::exp(x - m); sum += x; }
            for (int d = 0; d < hd; ++d) {
                double acc = 0;
                for (int64_t c = lo; c < end; ++c) {
                    if (std::abs(c - (end - rows + r)) >= window) continue;
                    const uint16_t* vrow = vp.data() + (size_t) row_of(c, h) * hd;
                    acc += scores[(size_t) c] * __half2float(__ushort_as_half(vrow[d]));
                }
                const double want = acc / sum, have = got[(size_t) r * qw + h * hd + d];
                const double abse = std::abs(have - want);
                const double rel = std::abs(want) > 1e-9 ? abse / std::abs(want) : abse;
                if (abse > max_abs) max_abs = abse;
                if (rel > max_rel) max_rel = rel;
                if (abse > 1e-4) { ok = false; first_abs = abse; fr = r; fh = h; fd = d; f_want = want; f_have = have; }
            }
        }
        if (!ok) {
            std::fprintf(stderr,
                         "ring case %s: first |delta|=%.3e at row %d head %d dim %d (want %.6f have %.6f); "
                         "max_abs=%.3e max_rel=%.3e; geometry 32Q/8KV x %d page %d ring %lld window %d rows %d\n",
                         tag, first_abs, fr, fh, fd, f_want, f_have, max_abs, max_rel,
                         hd, page, (long long) ring, window, rows);
        }
        check(ok, what);
    };
    attention_case(70, "deep-rewind/stale-mapped", "ring attention reads the wrapped rows (stale newest-writer mapping)");
    attention_case(through - rows, "in-place/retained", "retained ring attention reads the in-place rows at absolute positions");

    cudaFree(kd); cudaFree(vd); cudaFree(qd); cudaFree(out); cudaFree(scratch);
    cudaFree(steps); cudaFree(attn); cudaFree(positions);
    cudaStreamDestroy(cs);
    cudaFree(o.arena);
}
#endif  // STRATA_ENABLE_DFLASH2

}  // namespace

int main(int argc, char** argv) {
    if (cudaSetDevice(0) != cudaSuccess) {
        std::printf("dflash_owned_kv_test: no GPU\n");
        return 77;
    }
    {
        DFlashDrafter drafter;
        SessionState session;
        ModelGeometry geometry;
        std::string err;
        int32_t out[7] = {};
        check(!drafter.upload(geometry, session, 0, 128, -1, err), "unloaded upload refused");
        check(!drafter.propose(1, 0, 3, out, err), "unbound proposal refused");
        check(!drafter.add_context(nullptr, 5, 1, 0, 1, err), "unloaded BF16 context refused");
        check(!drafter.add_context_f32(nullptr, 5, 2560, 0, 1, err), "unloaded F32 context refused");
        drafter.release();
        drafter.release();
        check(drafter.vram_bytes() == 0 && drafter.capacity() == 0, "release is idempotent");
    }
    ModelGeometry g;   // the canonical head geometry: 24Q/2KV x 256, page_size 4
    const int64_t max_cells = 4096;
    batch_and_attention_regression(g);
    owned_elastic_regression(g);
    conversation_regression(g);
#ifdef STRATA_ENABLE_DFLASH2
    ring_regression(g);
#endif

    std::fprintf(stderr, "dbg: section 1 (owned+append)\n");
    {   // 1+2: one owned state, ownership + append/readback across page boundaries
        Owned o;
        std::vector<int32_t> table;
        check(o.init(g, max_cells, &table), "owned state init");
        check_ownership(o, "single state");
        check((int) table.size() == (int) o.st.n_pages && table[(size_t) 0] == 0 &&
                  table[(size_t)(table.size() - 1)] == (int32_t)(table.size() - 1),
              "identity page table");
        append_and_readback(o, g, table);
        cudaFree(o.arena);
    }
    std::fprintf(stderr, "dbg: section 4 (five layers)\n");
    {   // 4: five states, one per draft layer — each independently owned
        std::vector<Owned> layers(5);
        bool all = true;
        for (auto& o : layers) all = o.init(g, max_cells) && all;
        check(all, "five owned states init");
        for (int l = 0; l < 5; ++l) {
            char what[64];
            std::snprintf(what, sizeof what, "layer %d owned", l);
            check_ownership(layers[(size_t) l], what);
        }
        // independence: append cell 5 to layer 2 only; layers 0 and 4 must stay zero there
        const strata::kernels::QsaShapes s = strata::kernels::qsa_real_shapes();
        cudaStream_t cs = nullptr;
        cudaStreamCreate(&cs);
        std::vector<float> kcur((size_t) s.n_head_kv * s.head_dim, 0.5f), vcur = kcur;
        float *kd, *vd;
        cudaMalloc(&kd, kcur.size() * 4);
        cudaMalloc(&vd, vcur.size() * 4);
        cudaMemcpyAsync(kd, kcur.data(), kcur.size() * 4, cudaMemcpyHostToDevice, cs);
        cudaMemcpyAsync(vd, vcur.data(), vcur.size() * 4, cudaMemcpyHostToDevice, cs);
        const int32_t step[4] = {5, 6, 1, 6};
        int32_t* sd;
        cudaMalloc(&sd, sizeof step);
        cudaMemcpyAsync(sd, step, sizeof step, cudaMemcpyHostToDevice, cs);
        (void) cudaGetLastError();
        kv_append_step(layers[2].st.k_pool, layers[2].st.v_pool, layers[2].st.page_table, sd, kd, vd, s, cs, nullptr);
        const cudaError_t e4 = cudaGetLastError();
        if (e4 != cudaSuccess) std::fprintf(stderr, "dbg: s4 append err: %s\n", cudaGetErrorString(e4));
        cudaStreamSynchronize(cs);
        for (int l : {0, 4}) {
            std::vector<uint16_t> got(256);
            const int64_t r = (5 / 4) * 8 + 5 % 4;   // (page*2+kvh)*4+slot for kvh 0
            cudaMemcpyAsync(got.data(), layers[(size_t) l].st.k_pool + (size_t) r * 256, 512, cudaMemcpyDeviceToHost, cs);
            cudaStreamSynchronize(cs);
            bool zero = true;
            for (uint16_t b : got) if (b != 0) zero = false;
            char what[64];
            std::snprintf(what, sizeof what, "layer %d stays zero at layer 2's cell", l);
            check(zero, what);
        }
        cudaFree(kd);
        cudaFree(vd);
        cudaFree(sd);
        cudaStreamDestroy(cs);
        for (auto& o : layers) cudaFree(o.arena);
    }

    std::fprintf(stderr, "dbg: section 3 (elastic)\n");
    {   // 3: elastic non-membership — global grow/shrink must not touch the owned cells
        Owned o;
        std::vector<int32_t> table;
        check(o.init(g, max_cells, &table), "owned state init (elastic section)");
        append_and_readback(o, g, table);
        if (qsa_kv_elastic()) qsa_set_kv_elastic(false, 0);   // the test owns the process flag

        if (qsa_kv_elastic()) {   // VMM present: an elastic-mode state's pools must NOT be in the arena
            Owned elastic;   // a state the way the target's session would create one under --kv-grow
            elastic.bytes = qsa_state_bytes(g, max_cells, false, 0);
            check(cudaMalloc(&elastic.arena, elastic.bytes) == cudaSuccess, "elastic-mode arena");
            const uint64_t eb = qsa_state_init(g, max_cells, elastic.arena, elastic.st, nullptr, 0);
            (void) cudaGetLastError();   // a failed/reserved VMM attempt may leave a sticky error
            if (eb == 0)
                check(false, "elastic init should have succeeded");
            else {
                const uintptr_t a0 = reinterpret_cast<uintptr_t>(elastic.arena);
                const bool inside = reinterpret_cast<uintptr_t>(elastic.st.k_pool) >= a0 &&
                                    reinterpret_cast<uintptr_t>(elastic.st.k_pool) < a0 + elastic.bytes;
                check(!inside, "elastic-mode pools live outside the caller arena (the refusal is justified)");
            }
            cudaFree(elastic.arena);
        }
        // grow/shrink the registry, then verify the OWNED state's cells are untouched
        const auto before = std::invoke([&] {
            std::vector<uint16_t> r(256);
            cudaMemcpy(r.data(), o.st.k_pool, 512, cudaMemcpyDeviceToHost);
            return r;
        });
        const int64_t need = qsa_kv_elastic_need(max_cells);
        std::vector<strata::core::VmmChunk> taken;
        qsa_kv_elastic_grow(need, [&] { return strata::core::VmmChunk{}; });
        qsa_kv_elastic_shrink(need, [&](strata::core::VmmChunk c) { taken.push_back(c); });
        (void) cudaGetLastError();
        std::vector<uint16_t> after(256);
        cudaMemcpy(after.data(), o.st.k_pool, 512, cudaMemcpyDeviceToHost);
        check(before == after, "global elastic grow/shrink leaves the owned cells untouched");
        qsa_set_kv_elastic(false, 0);
        cudaFree(o.arena);
        (void) cudaGetLastError();   // the VMM experiments may leave a sticky error; the test is done
    }
    // Optional real artifact: repeat upload/release and reject mutation while
    // uploaded. No target weights or target forward are needed for this gate.
    for (int arg = 1; arg < argc; ++arg) {
        Owned target;
        if (!target.init(g, 256)) { check(false, "lifecycle target init"); continue; }
        SessionState session;
        session.qsa_states = &target.st;
        session.qsa_alloc = 1;
        DFlashDrafter drafter;
        std::string err;
        check(drafter.load(argv[arg], err), "lifecycle artifact load");
        for (int repeat = 0; repeat < 3; ++repeat) {
            const bool uploaded = drafter.upload(g, session, 0, 192, -1, err);
            if (!uploaded) std::fprintf(stderr, "lifecycle upload: %s\n", err.c_str());
            check(uploaded, "lifecycle upload");
            if (!uploaded) break;
            const auto bytes = drafter.vram_bytes();
            check(!drafter.upload(g, session, 0, 192, -1, err), "second upload refused");
            check(!drafter.load(argv[arg], err), "loaded device artifact cannot be replaced");
            check(bytes == drafter.vram_bytes(), "refused mutation preserves allocations");
            WeightTable empty;
            check(!drafter.bind(empty, nullptr, err), "missing target head refused");
            int32_t picks[7] = {};
            check(!drafter.propose(1, 0, 3, picks, err), "failed bind leaves proposal disabled");
            drafter.release();
            drafter.release();
            check(drafter.vram_bytes() == 0 && drafter.capacity() == 0, "lifecycle release");
        }
        if (target.st.host_step) cudaFreeHost(target.st.host_step);
        if (target.st.host_pos) cudaFreeHost(target.st.host_pos);
        if (target.st.owns_rope) { cudaFree(target.st.cos_tab); cudaFree(target.st.sin_tab); }
        cudaFree(target.arena);
    }
    std::printf(g_fail ? "dflash_owned_kv_test: %d FAILURES\n" : "dflash_owned_kv_test: ok\n", g_fail);
    return g_fail ? 1 : 0;
}

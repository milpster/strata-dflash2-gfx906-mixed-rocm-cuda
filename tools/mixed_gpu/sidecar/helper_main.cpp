// tools/mixed_gpu/sidecar/helper_main.cpp - the CUDA-only `strata-expert-helper` (R2).
//
// A SEPARATE PROCESS on the NVIDIA card: it owns ALL of that card's expert VRAM (the cache slots,
// the staged activations, the scratch), and it is the only program in the mixed topology that links
// a CUDA runtime.  The engine (HIP on the split32 box, CUDA anywhere else) never touches this
// device: it talks to this process over the framed AF_UNIX channel (the SAME transport file the
// engine's proxy client runs: helper_frame.cpp), and the rows come back as kRows frames.
//
// The compute is the engine's own grouped-kernel path, unchanged: the window's activations arrive
// as the q8_1 blocks the engine quantized (bit for bit `quantize_q8_1_kernel`'s), ONE grouped
// launch over the claimed entries per window (`native_expert_grouped`, the same kernels the
// in-process tier launches, the same metadata layout), and the rows return UNWEIGHTED - the engine
// weights them once in its existing moe_combine, exactly as it does for an in-process helper's rows.
//
// Startup order (docs/MIXED_GPU_HELPER.md): bind the socket (refusing an existing path, 0600),
// accept ONE connection (same uid via SO_PEERCRED), kHello/kHelloAck (geometry), kFill (ranked
// pairs -> fill from the mmap'd pack within the card's budget, the engine's claimed bitmap excluded),
// kFillReport (the authoritative resident set), then the begin/rows loop.  Absent at engine start =
// the engine keeps its CPU path; this process never needs the engine to be up first, and it exits
// cleanly (SIGTERM/SIGINT, peer EOF, --once, idle timeout) releasing the card and unlinking ONLY
// the socket it bound.  No systemd, no shmem, no adaptation, no eviction: the resident set is fixed
// at fill, and a helper restart is an engine restart (documented).
//
// TWO CHANNELS (opt-in --socket2, docs/work/MIXED_BACKEND_RPC_PLAN.md sect12b): with a second path
// given, the helper binds BOTH sockets and runs the frame loop above on TWO threads, one per
// channel, so the engine's window tier and its prompt tier stop blocking each other.  The protocol
// is unchanged (no new frame type): each channel runs its own kHello/kHelloAck and its own
// begin/rows, the engine sends kFill on ONE channel, and both channels serve rows out of the SAME
// filled mirror.  The device is shared, so one served frame holds Helper::gpu_mtx (parse, upload,
// launch, read the rows back, write the reply) and the lock is never held across a channel's idle
// read.  WITH --socket2 ABSENT: one socket, one accept, one thread, the loop exactly as it was.
#include "strata/core/expert_helper_protocol.hpp"
#include "strata/core/expert_cache.hpp"
// M7 (the drafter lane, docs/work/MIXED_BACKEND_RPC_PLAN.md sect12/12a): the serve-runtime drafter
// (include/strata/core/dflash.hpp's DFlashDrafter: add_context_f32 / propose), the target's native
// head + pinned embedding it binds, and the metadata-only weight table the bind reads.
#include "strata/core/dflash.hpp"
#include "strata/core/dflash_refusal.hpp"
#include "strata/core/geometry.hpp"
#include "strata/core/layer.hpp"
#include "strata/core/native_head.hpp"
#include "strata/core/weights.hpp"
#include "strata/artifact/gguf_split.hpp"
#include "strata/kernels/cpu/expert_layout.hpp"
#include "strata/kernels/iq_kernels.hpp"
#include "strata/kernels/s2_expert_grouped.hpp"

#include "ggml.h"   // the fp16 contract of the q8_1 activation blocks (CPU-safe, no ggml compute; M4c (b))
#include <cuda_runtime.h>

#include <atomic>
#include <cmath>
#include <csignal>
#include <chrono>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <mutex>
#include <optional>
#include <set>
#include <shared_mutex>
#include <string>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

using namespace strata::core;
using namespace strata::core::expert_helper;

namespace {

constexpr int64_t H = strata::kernels::cpu::H;
constexpr int64_t FF = strata::kernels::cpu::FF;
constexpr int64_t CAP = strata::kernels::cpu::MAXT * 10;   // 80 entries: the engine's window contract
constexpr int64_t kReserveDefaultMiB = 512;

/// The grouped-kernel metadata, the engine's RemoteMeta layout (src/core/remote_experts.cpp:25):
/// the kernels read the five arrays through pointers into ONE contiguous upload.
struct GroupMeta {
    unsigned long long ptr[CAP];
    int32_t start[CAP + 1];
    int32_t dst[CAP];
    int32_t tok[CAP];
    int32_t count;
};

/// M4c (b): the helper-side twin of the engine's quantize_q8_1_cpu (src/core/expert_helper_socket.cpp,
/// whose comment carries the full bit-identity proof against `quantize_q8_1_kernel`/`q8_1_store`).
/// The engine now ships FP32 rows (kFeaturePromptF32) so the quantize leaves ITS critical path; this
/// twin produces the SAME bytes for the SAME floats - same constants, same clamps, same order - and
/// the grouped kernel's launch is unchanged.  The engine-side golden-byte pins hold that contract.
void quantize_q8_1_twin(const float* x, int64_t n, uint8_t* y) {
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

/// R5 P1 (the selected-token prompt tier): THE SAME metadata layout, sized by the protocol's row cap
/// (8192 since M4b, 256 before) instead of the window contract's 80 entries.  `native_expert_grouped` is data-driven - the
/// groups, entries and destination rows arrive as data, the same launch serve_begin makes - so this
/// tier needs NO new kernel: it needs meta and buffers wide enough for the caps it refuses beyond.
constexpr int64_t kPromptRows = strata::core::expert_helper::kMaxRowsPerLayer;
constexpr int64_t kPromptTok = strata::core::expert_helper::kMaxSelTok;
struct GroupMetaP {
    unsigned long long ptr[kPromptRows];
    int32_t start[kPromptRows + 1];
    int32_t dst[kPromptRows];
    int32_t tok[kPromptRows];
    int32_t count;
};

std::atomic<int> g_stop{0};
// R5 P1 (the SIGTERM begin-wait fix, t0-incumbent-repro1 evidence: helper 2462170 SURVIVED the
// driver's SIGTERM while sitting in its 30-minute --begin-timeout read, leaving the restore
// RESTORE_BLOCKED and forcing a parent SIGKILL).  A flag alone cannot end that wait: the handler
// returns, poll resumes (poll is never restarted by SA_RESTART), and the loop re-polls against the
// same absolute deadline for the whole 30 minutes - g_stop was never consulted.  So the handler also
// writes ONE byte to a self-pipe, the transport's poll watches that pipe (set_poll_wake), and the
// wait returns the value an expired deadline returns - the loop's own named-timeout exit.  write()
// to a non-blocking pipe is async-signal-safe, so the handler stays safe, and the exit path is the
// existing one: release() synchronizes the stream, closes the channel, unlinks only its own socket.
// An in-flight window is not corrupted: it is aborted, the engine's client sees the closed channel
// (kErrUnhealthy/timeout) and recomputes the window's rows on the CPU, exactly the shipped fault rule.
int g_wake_pipe[2] = {-1, -1};
void on_signal(int) {
    g_stop.store(1);
    if (g_wake_pipe[1] >= 0) {
        const char byte = 1;
        const int saved = errno;
        ::write(g_wake_pipe[1], &byte, 1);   // one byte: wakes the helper's poll (async-signal-safe)
        errno = saved;
    }
}

// The handshake's facts (kHello) and the loop's counters, read by serve_fill and main.
// TWO-CHANNEL (opt-in --socket2, docs/work/MIXED_BACKEND_RPC_PLAN.md §12b): with a second bound
// socket these are touched from TWO serve threads, so every shared flag and counter here is atomic.
// With --socket2 absent exactly ONE thread ever touches them - the single-socket path's behaviour is
// unchanged, byte for byte (an uncontended atomic is the same load/store the plain scalar was).
std::vector<uint8_t> g_claimed;   // the engine's stage-held bitmap, byte per (layer, expert)
int64_t g_reserve = kReserveDefaultMiB;
int64_t g_slots = 0;              // the engine's slot request: an upper bound on the fill
int64_t g_fill_mib = 0;           // --fill-mib: an OPT-IN cap on uploaded expert bytes (0: the budget decides)
std::atomic<bool> filled{false};
std::atomic<int64_t> windows_served{0};
std::atomic<int64_t> prompt_requests_served{0};   // R5 P1: kBeginP requests served (reported, never inferred)
// M7 (the drafter lane): the draft counters, reported only when the drafter lane is ON (with no
// draft arguments the helper's exit line stays exactly as it was).
std::atomic<int64_t> draft_ctx_rows_served{0};    // kDraftCtx: tap rows added to the drafter's ring
std::atomic<int64_t> draft_rounds_served{0};      // kDraftReq: rounds proposed

struct MappedPack {
    uint8_t* addr = nullptr;
    size_t size = 0;
    bool open(const std::string& path, std::string& err) {
        int fd = ::open(path.c_str(), O_RDONLY);
        if (fd < 0) { err = "cannot open " + path; return false; }
        struct stat st;
        if (::fstat(fd, &st) != 0 || st.st_size <= 0) { ::close(fd); err = "cannot stat " + path; return false; }
        size = (size_t) st.st_size;
        addr = (uint8_t*) ::mmap(nullptr, size, PROT_READ, MAP_PRIVATE | MAP_FILE, fd, 0);
        ::close(fd);
        if (addr == MAP_FAILED) { addr = nullptr; err = "cannot mmap " + path; return false; }
        return true;
    }
    void close() { if (addr != nullptr) ::munmap(addr, size); addr = nullptr; size = 0; }
};

struct Helper {
    Socket listener;
    Socket sock;
    // TWO-CHANNEL (opt-in --socket2): the second bound socket and its connection, and the lock that
    // serializes the GPU work between the two serve threads.  The frame loops are per-channel (each
    // owns its socket, its idle wait, its exit), the DEVICE state is shared: the mirror slots, the
    // window tier's staging, the prompt tier's staging and the drafter's ring and tap staging.  The
    // lock covers ONE served frame (parse, upload, launch, read the rows back, write the reply) and is
    // NEVER held across a channel's idle read_frame - so channel A's socket I/O overlaps channel B's
    // GPU work, which is the whole point of the split, while the two tiers never touch the same
    // staging buffers at the same time.
    Socket listener2;
    Socket sock2;
    std::mutex gpu_mtx;
    // L2 (opt-in STRATA_HELPER_STREAMS, default OFF = exactly the single-mutex behaviour above):
    // ON turns kFill into the gate's only WRITER and every served frame into a reader, so
    // channel A's window/draft frames and channel B's prompt frames overlap on their OWN
    // streams (the mirror reads are read-only after fill); each frame still excludes fill, and
    // fill fences both streams because a frame releases the gate only after its sync + reply.
    std::shared_mutex gpu_gate;
    cudaStream_t stream_p = nullptr;      // ON: the prompt tier's own stream
    bool streams_split = false;           // set once in main; never mutated afterwards
    std::thread chan2;
    ExpertCache cache;
    MappedPack pack;
    GroupMeta* h_meta = nullptr;   // pinned, mapped: one upload per window
    GroupMeta* d_meta = nullptr;
    uint8_t* d_q8 = nullptr;       // the window's activations (n_tok * (H/32) * 36)
    float* d_out = nullptr;        // the compact rows (CAP * H)
    void* d_scratch = nullptr;
    // R5 P1 (the prompt tier's OWN device staging, sized by the protocol caps, never by T): the
    // grouped kernel's inputs for one kBeginP.  Pinned+mapped for the same reasons H10 records for
    // d_meta/d_out - serve_prompt reads the meta's fields on the host and copies the rows back with
    // a host memcpy, so plain cudaMalloc for those two is the SIGSEGV/r10 flicker, not a win.
    GroupMetaP* h_meta_p = nullptr;
    GroupMetaP* d_meta_p = nullptr;
    uint8_t* d_q8_p = nullptr;     // kPromptTok * (H/32) * 36
    std::vector<uint8_t> h_q8_p_scratch;   // M4c (b): the fp32 image's quantized staging (plain heap upload source)
    float* d_out_p = nullptr;      // pinned: the compact rows (kPromptRows * H)
    void* d_scratch_p = nullptr;   // the grouped kernel's scratch for kPromptRows entries
    cudaStream_t stream = nullptr;
    int64_t n_layers = 0, n_expert = 0, layer_lo = 0, layer_hi = -1;
    int64_t scratch_bytes = 0, buffers_bytes = 0, prompt_scratch_bytes = 0, prompt_buffers_bytes = 0;
    // M7 (the drafter lane, docs/work/MIXED_BACKEND_RPC_PLAN.md sect12/12a): the serve-runtime
    // drafter (include/strata/core/dflash.hpp), the target's native head + pinned embedding it
    // binds, the metadata-only weight table the bind reads, and the PINNED tap staging the draft
    // frames arrive into.  All of it exists ONLY when --dflash was given: with no draft arguments
    // every field stays empty and no code path below runs.
    strata::core::DFlashDrafter draft;
    strata::core::NativeHead draft_head;
    strata::core::NativeEmbed draft_emb;
    strata::core::WeightTable draft_wt;
    bool draft_ready = false;                 // upload + bind succeeded: kFeatureDraft is advertised
    float* h_taps = nullptr;                  // tap staging (pinned): tap-major, kMaxDraftCtxRows rows
    int64_t tap_planes = 0, tap_hidden = 0;   // the artifact's geometry: n_planes, the target's n_embd
    void* qsa_arena = nullptr;                // the ONE primary QSA state the drafter's upload reads
    strata::core::QsaState qsa_primary{};

    ~Helper() { release(); }
    void release() {
        if (stream) cudaStreamSynchronize(stream);
        if (stream_p) cudaStreamSynchronize(stream_p);   // L2: fence the prompt tier's stream too
        // M7: the drafter's own resources first (its release() synchronizes its stream), then the
        // pinned embedding the bind borrowed (clear the process global before the table dies).
        draft.release();
        if (draft_emb.bytes() != 0) strata::core::set_native_embed(nullptr);
        draft_head.unload();
        if (h_taps) cudaFreeHost(h_taps);
        if (qsa_arena) cudaFree(qsa_arena);
        h_taps = nullptr; qsa_arena = nullptr;
        draft_ready = false;
        cache.close();
        if (h_meta) cudaFreeHost(h_meta);
        if (d_meta) cudaFree(d_meta);
        if (d_q8) cudaFree(d_q8);
        if (d_out) cudaFree(d_out);
        if (d_scratch) cudaFree(d_scratch);
        if (h_meta_p) cudaFreeHost(h_meta_p);
        if (d_meta_p) cudaFree(d_meta_p);
        if (d_q8_p) cudaFree(d_q8_p);
        if (d_out_p) cudaFree(d_out_p);
        if (d_scratch_p) cudaFree(d_scratch_p);
        if (stream) cudaStreamDestroy(stream);
        if (stream_p) cudaStreamDestroy(stream_p);
        h_meta = nullptr; d_meta = nullptr; d_q8 = nullptr; d_out = nullptr; d_scratch = nullptr;
        h_meta_p = nullptr; d_meta_p = nullptr; d_q8_p = nullptr; d_out_p = nullptr; d_scratch_p = nullptr;
        stream = nullptr;
        pack.close();
        sock.close();
        sock2.close();
        listener.unbind();   // only the path THIS process bound
        listener2.unbind();  // the second channel's own path (a no-op when --socket2 was not given)
    }
};

int fail(const char* what, const std::string& why) {
    std::fprintf(stderr, "expert-helper: %s: %s\n", what, why.c_str());
    return 1;
}

bool cuda_check(cudaError_t e, const char* what, std::string& err) {
    if (e == cudaSuccess) return true;
    err = std::string(what) + ": " + cudaGetErrorString(e);
    return false;
}

// ---- kFill: fill the cache within the card's budget, in the engine's rank order ----
int serve_fill(Helper& hp, const std::vector<uint8_t>& body, uint32_t req_id, uint32_t seq, int64_t reserve_mib,
               const std::vector<uint8_t>& claimed, int64_t max_slots, std::string& err, Socket& out) {
    // The fill mutates the shared cache (and the card's free memory the budget reads), so it is
    // serialized against every served frame: the two-channel engine sends kFill on ONE channel only,
    // and the other channel's tiers must not launch while slots are being allocated.
    std::optional<std::lock_guard<std::mutex>> gpu_lk_off;
    std::optional<std::unique_lock<std::shared_mutex>> gpu_lk_fill;
    if (hp.streams_split) {
        gpu_lk_fill.emplace(hp.gpu_gate);     // L2: fill is the gate's only writer
        if (hp.stream_p) cudaStreamSynchronize(hp.stream_p);   // idle by construction (frames
                                                               // drained before the gate was taken)
    } else {
        gpu_lk_off.emplace(hp.gpu_mtx);
    }
    if (body.size() < sizeof(FillReq)) return kErrShort;
    FillReq req;
    std::memcpy(&req, body.data(), sizeof(req));
    if (req.n_pairs < 0 || req.reserved0 != 0 || body.size() != sizeof(req) + (size_t) req.n_pairs * 8)
        return kErrShort;
    const auto& lay = strata::kernels::cpu::expert_layout();
    std::vector<std::pair<int32_t, int32_t>> pairs;
    std::vector<int64_t> sizes;
    uint64_t total = 0;
    size_t free_b = 0, total_b = 0;
    if (!cuda_check(cudaMemGetInfo(&free_b, &total_b), "cudaMemGetInfo", err)) return kErrInternal;
    const int64_t budget = (int64_t) free_b - reserve_mib * 1048576ll - hp.scratch_bytes - hp.buffers_bytes
                           - (16ll << 20);   // 16 MiB: the context's own growth headroom
    std::fprintf(stderr, "expert-helper: fill preflight: device free %lld MiB of %lld MiB, budget %.1f MiB "
                         "(reserve %lld MiB, scratch %lld B, buffers %lld B), slots cap %lld, fill-mib cap %lld\n",
                 (long long) (free_b >> 20), (long long) (total_b >> 20), (double) budget / 1048576.0,
                 (long long) reserve_mib, (long long) hp.scratch_bytes, (long long) hp.buffers_bytes,
                 (long long) (max_slots == INT64_MAX ? -1 : max_slots), (long long) g_fill_mib);
    const char* stop = "ranked candidates exhausted";
    for (int32_t i = 0; i < req.n_pairs; ++i) {
        if ((int64_t) pairs.size() >= max_slots) { stop = "slots"; break; }   // the engine's slot request bounds the fill
        int32_t l = 0, e = 0;
        std::memcpy(&l, body.data() + sizeof(req) + (size_t) i * 8, 4);
        std::memcpy(&e, body.data() + sizeof(req) + (size_t) i * 8 + 4, 4);
        if (l < hp.layer_lo || l > hp.layer_hi || l >= hp.n_layers || e < 0 || e >= hp.n_expert) continue;
        const size_t ci = (size_t) l * (size_t) hp.n_expert + (size_t) e;
        if (ci < claimed.size() && claimed[ci]) continue;               // a stage owns it: never fill
        if (hp.cache.slot_of(l, e) >= 0) continue;                       // already resident
        const uint64_t bytes = ExpertCache::slot_stride((int64_t) lay.blob_bytes(l));
        if (g_fill_mib > 0 && (int64_t) total + (int64_t) bytes > g_fill_mib * 1048576ll) { stop = "fill-mib"; break; }
        if ((int64_t) total + (int64_t) bytes > budget) { stop = "budget"; break; }   // the budget decides, the report tells
        pairs.push_back({l, e});
        sizes.push_back((int64_t) lay.blob_bytes(l));
        total += bytes;
    }
    if (pairs.empty()) { err = "no expert fits the card's budget"; return kErrRequest; }
    if (!hp.cache.open_sized(sizes, hp.n_layers, hp.n_expert, err)) return kErrRequest;
    // H14 (r24): a GGUF-mapped pack keeps no packed blob region - the manifest's `offset` column
    // describes an experts.bin layout this pack does not ship, so blob_offset() lands on unrelated
    // GGUF bytes (r23 proof: garbage scales there, 191/2560 NaN rows; the engine offsets decode
    // clean, 0 NaN). Assemble each slot per role exactly like the engine's
    // FileExpertSource::copy_from_files: role r reads gguf_off[3*layer + r] + expert*role_bytes[r],
    // roles ordered [gate | up | down] as native_expert_layout expects.
    const bool per_role = lay.gguf_off.size() == 3 * (size_t) hp.n_layers && lay.gguf_file.empty();
    std::vector<uint8_t> stage, warm_src;
    for (size_t i = 0; i < pairs.size(); ++i) {
        const int32_t slot = hp.cache.admit(pairs[i].first, pairs[i].second);
        const uint64_t bytes = lay.blob_bytes(pairs[i].first);
        const uint8_t* src = nullptr;
        if (per_role) {
            const auto& pf = lay.fmt[(size_t) pairs[i].first];
            const auto pl = strata::kernels::native_expert_layout(pf.gu_type, pf.d_type, pf.n_embd, pf.n_ff);
            const uint64_t per[3] = { (uint64_t) pf.n_ff * pl.gu_row, (uint64_t) pf.n_ff * pl.gu_row,
                                      (uint64_t) pf.n_embd * pl.d_row };
            stage.resize((size_t) bytes);
            uint64_t at = 0;
            for (int r = 0; r < 3; ++r) {
                const uint64_t roff = lay.gguf_off[3 * (size_t) pairs[i].first + (size_t) r]
                                    + (uint64_t) pairs[i].second * per[r];
                if (roff + per[r] > hp.pack.size) { err = "a role region is outside the pack mapping"; return kErrInternal; }
                std::memcpy(stage.data() + at, hp.pack.addr + roff, (size_t) per[r]);
                at += per[r];
            }
            if (at != bytes) { err = "the role bytes do not sum to the blob size"; return kErrInternal; }
            src = stage.data();
        } else {
            const uint64_t off = lay.blob_offset(pairs[i].first, pairs[i].second);
            if (off + bytes > hp.pack.size) { err = "a pair's blob is outside the pack mapping"; return kErrInternal; }
            src = hp.pack.addr + off;
        }
        if (slot < 0 || !hp.cache.fill_slot_blocking(slot, src, err, (int64_t) bytes)) return kErrRequest;
        if (i == 0) {   // one slot verified byte for byte against the exact source the fill used
            if (!hp.cache.verify_slot(slot, src, err, (int64_t) bytes)) return kErrRequest;
            warm_src.assign(src, src + (size_t) bytes);   // the warmup dump compares against these bytes
        }
    }
    // One slot verified byte for byte (inside the fill loop, against the exact per-role source);
    // the warmup below launches on that first slot.
    const int32_t slot0 = hp.cache.slot_of(pairs.front().first, pairs.front().second);
    // H12 (r14/r15): the FIRST native_expert_grouped launch on a cold CUDA context pays the
    // module JIT - hundreds of milliseconds. Paid inside the engine's first timed window it
    // blows the per-window deadline; the engine's client then closes the channel and the whole
    // tier is lost for the run (r14: helper died mid-write by SIGPIPE; r15: one window then EOF).
    // Pay it HERE, on the helper's own time, on one resident row; the result is discarded.
    {
        const auto& wlay = strata::kernels::cpu::expert_layout();
        const auto& wfmt = wlay.fmt[(size_t) pairs.front().first];
        if (wfmt.had_gu || wfmt.had_d ||
            !strata::kernels::native_expert_supported(wfmt.gu_type, wfmt.d_type, wfmt.n_embd, wfmt.n_ff))
            std::fprintf(stderr, "expert-helper: warmup skipped (the first filled layer is outside the kernel contract)\n");
        else {
            const auto L = strata::kernels::native_expert_layout(wfmt.gu_type, wfmt.d_type, wfmt.n_embd, wfmt.n_ff);
            // H14: zero the activations the warmup reads (uninitialized q8 can decode to an Inf/NaN
            // fp16 scale), the output row, and the SCRATCH (r22: 191 of 2560 rows NaN with x=0 — the
            // down pass reads scratch bytes the fused pass never wrote this call; uninitialized
            // cudaMalloc junk carries NaN fp16 patterns).
            cudaMemsetAsync(hp.d_q8, 0, 65536, hp.stream);
            cudaMemsetAsync(hp.d_out, 0, (size_t) wfmt.n_embd * 4, hp.stream);
            cudaMemsetAsync(hp.d_scratch, 0, (size_t) hp.scratch_bytes, hp.stream);
            hp.h_meta->ptr[0] = (unsigned long long) hp.cache.device_slot(slot0);
            hp.h_meta->start[0] = 0; hp.h_meta->start[1] = 1;
            hp.h_meta->dst[0] = 0; hp.h_meta->tok[0] = 0;
            hp.h_meta->count = 1;
            if (!cuda_check(cudaMemcpyAsync(hp.d_meta, hp.h_meta, sizeof(GroupMeta), cudaMemcpyHostToDevice, hp.stream),
                            "warmup meta upload", err)) return kErrInternal;
            strata::kernels::native_expert_grouped(L, hp.d_meta->ptr, hp.d_meta->start, &hp.d_meta->count,
                                                    hp.d_meta->dst, hp.d_meta->tok, 1, 1,
                                                    (const uint8_t*) hp.d_q8, hp.d_scratch, hp.d_out, hp.stream);
            cudaError_t we = cudaGetLastError();
            if (we == cudaSuccess) we = cudaStreamSynchronize(hp.stream);
            if (we != cudaSuccess)
                std::fprintf(stderr, "expert-helper: warmup launch failed: %s (the first engine window pays the JIT)\n",
                             cudaGetErrorString(we));
            else {
                // H14 dump: everything the kernel saw and wrote, so a wrong slot pointer or a meta
                // defect cannot hide behind a self-consistent fill verification.
                uint8_t dev_b[16] = {0};
                cudaMemcpy(dev_b, (const void*) (uintptr_t) hp.h_meta->ptr[0], sizeof(dev_b), cudaMemcpyDeviceToHost);
                const uint8_t* src = warm_src.empty() ? hp.pack.addr + lay.blob_offset(pairs.front().first, pairs.front().second)
                                                       : warm_src.data();   // r24: the per-role assembled bytes
                std::fprintf(stderr, "expert-helper: warmup fmt L%d: gu_type=%d d_type=%d n_embd=%d n_ff=%d "
                                     "L.bytes=%zu blob_bytes=%lld\n",
                             (int) pairs.front().first, (int) wfmt.gu_type, (int) wfmt.d_type,
                             (int) wfmt.n_embd, (int) wfmt.n_ff, L.bytes,
                             (long long) lay.blob_bytes(pairs.front().first));
                std::fprintf(stderr, "expert-helper: warmup blob@ptr[0] %02x%02x%02x%02x%02x%02x%02x%02x "
                                     "vs pack %02x%02x%02x%02x%02x%02x%02x%02x (%s)\n",
                             dev_b[0], dev_b[1], dev_b[2], dev_b[3], dev_b[4], dev_b[5], dev_b[6], dev_b[7],
                             src[0], src[1], src[2], src[3], src[4], src[5], src[6], src[7],
                             std::memcmp(dev_b, src, 8) == 0 ? "ptr matches the pack" : "PTR MISMATCH");
                std::fprintf(stderr, "expert-helper: warmup meta: groups=1 entries=[0,1) dst0=%d tok0=%d | "
                                     "out[0..3]=%g %g %g %g\n",
                             hp.h_meta->dst[0], hp.h_meta->tok[0],
                             (double) hp.d_out[0], (double) hp.d_out[1], (double) hp.d_out[2], (double) hp.d_out[3]);
                int64_t first_bad = -1, bad_count = 0;
                for (int64_t c = 0; c < wfmt.n_embd; ++c)
                    if (!std::isfinite(hp.d_out[c])) { if (first_bad < 0) first_bad = c; ++bad_count; }
                std::fprintf(stderr, "expert-helper: warmup launch done on zero activations: output %s\n",
                             first_bad < 0 ? "finite - the kernel, the slot bytes and the row layout are sane"
                                           : "NON-FINITE");
                if (first_bad >= 0) {
                    // H14: name the exact output position and the RAW weight bytes of its down row,
                    // so the sm_86-vs-gfx906 dequant difference can be pinned to specific scales.
                    const uint8_t* drow = src + L.down_off + (size_t) first_bad * L.d_row;
                    std::fprintf(stderr, "expert-helper: first bad out[%lld]=%g, %lld of %d non-finite; "
                                         "gu_row=%zu d_row=%zu down_off=%zu; down row %lld first 16 bytes: "
                                         "%02x%02x%02x%02x %02x%02x%02x%02x %02x%02x%02x%02x %02x%02x%02x%02x\n",
                                 (long long) first_bad, (double) hp.d_out[first_bad], (long long) bad_count,
                                 (int) wfmt.n_embd, L.gu_row, L.d_row, L.down_off, (long long) first_bad,
                                 drow[0], drow[1], drow[2], drow[3], drow[4], drow[5], drow[6], drow[7],
                                 drow[8], drow[9], drow[10], drow[11], drow[12], drow[13], drow[14], drow[15]);
                }
            }
        }
    }
    FillReport rep;
    rep.resident = (int64_t) pairs.size();
    rep.bytes = (int64_t) total;
    rep.scratch_bytes = hp.scratch_bytes;
    rep.buffers_bytes = hp.buffers_bytes;
    rep.n_pairs = (int32_t) pairs.size();
    std::vector<uint8_t> payload(sizeof(rep) + pairs.size() * 8, 0);
    std::memcpy(payload.data(), &rep, sizeof(rep));
    for (size_t i = 0; i < pairs.size(); ++i) {
        std::memcpy(payload.data() + sizeof(rep) + i * 8, &pairs[i].first, 4);
        std::memcpy(payload.data() + sizeof(rep) + i * 8 + 4, &pairs[i].second, 4);
    }
    std::fprintf(stderr, "expert-helper: filled %lld slots (%.2f GiB), scratch %lld B, buffers %lld B\n",
                 (long long) pairs.size(), (double) total / 1073741824.0,
                 (long long) hp.scratch_bytes, (long long) hp.buffers_bytes);
    size_t free_a = 0, total_a = 0;
    if (cudaMemGetInfo(&free_a, &total_a) == cudaSuccess)
        std::fprintf(stderr, "expert-helper: fill stopped: %s; device free now %lld MiB of %lld MiB "
                             "(%lld slots, %.2f GiB uploaded)\n",
                     stop, (long long) (free_a >> 20), (long long) (total_a >> 20),
                     (long long) pairs.size(), (double) total / 1073741824.0);
    else
        std::fprintf(stderr, "expert-helper: fill stopped: %s (%lld slots, %.2f GiB uploaded; the after-fill "
                             "free-memory read failed)\n",
                     stop, (long long) pairs.size(), (double) total / 1073741824.0);
    return out.write_reply(kFillReport, req_id, seq, payload.data(), payload.size(), deadline_after(600000));
}

// ---- kBegin: one grouped launch over the claimed entries; kRows answers with the original indices ----
int serve_begin(Helper& hp, const std::vector<uint8_t>& body, uint32_t req_id, uint32_t seq, std::string& err,
                Socket& out) {
    // ONE served frame holds the device lock (see Helper::gpu_mtx): the window tier's staging
    // (h_meta/d_meta/d_q8/d_out/scratch) and the prompt tier's are shared device state, and with
    // --socket2 two threads serve frames at once.  The lock is never held across the loop's read.
    // L3 (STRATA_RPC_PROFILE=1): queue_us = the head-of-line wait for the shared device lock (a
    // window frame queued behind the other channel's in-flight frame); the lock spans parse ->
    // upload -> kernel -> sync -> gather.  Default off: no clock call, no output, same behaviour.
    static const bool prof = std::getenv("STRATA_RPC_PROFILE") != nullptr;
    const auto p_entry = prof ? std::chrono::steady_clock::now()
                              : std::chrono::steady_clock::time_point{};
    std::unique_lock<std::mutex> gpu_lk(hp.gpu_mtx, std::defer_lock);
    std::optional<std::shared_lock<std::shared_mutex>> gpu_lk_on;
    if (hp.streams_split) gpu_lk_on.emplace(hp.gpu_gate);   // L2: a served frame is a reader
    else gpu_lk.lock();
    const int64_t p_queue_us = prof ? std::chrono::duration_cast<std::chrono::microseconds>(
                                          std::chrono::steady_clock::now() - p_entry).count() : 0;
    if (body.size() < sizeof(BeginReq)) return kErrShort;
    BeginReq req;
    std::memcpy(&req, body.data(), sizeof(req));
    const int64_t n_tok = req.n_tok, k = req.k;
    if (n_tok < 1 || n_tok > kMaxTok || k != kK || req.reserved0 != 0) return kErrGeometry;
    const int64_t n = n_tok * k;
    if (req.ids_bytes != (int32_t) (n * 4)) return kErrGeometry;
    const size_t res_bytes = (size_t) ((n_tok * hp.n_expert + 7) / 8);
    if ((int64_t) (n_tok * (H / 32) * 36) != req.n_q8) return kErrGeometry;
    if (body.size() != sizeof(req) + (size_t) req.ids_bytes + res_bytes + (size_t) req.n_q8) return kErrShort;
    const int32_t* ids = (const int32_t*) (body.data() + sizeof(req));
    const uint8_t* primary = body.data() + sizeof(req) + (size_t) req.ids_bytes;
    const uint8_t* q8 = primary + res_bytes;
    const int32_t layer = req.layer;
    if (layer < hp.layer_lo || layer > hp.layer_hi) return kErrGeometry;
    const auto& lay = strata::kernels::cpu::expert_layout();
    const auto& fmt = lay.fmt[(size_t) layer];
    if (fmt.had_gu || fmt.had_d) return kErrGeometry;   // the APR rotation path is not this helper's scope
    if (!strata::kernels::native_expert_supported(fmt.gu_type, fmt.d_type, fmt.n_embd, fmt.n_ff))
        return kErrGeometry;

    // The claims, in routing order: a VALID id, not primary-resident, resident HERE.  The grouped
    // kernel's `tok` indexes the window's q8 image, so one activation set per token serves its rows.
    std::vector<int32_t> claims;
    std::vector<int32_t> group_of((size_t) n, -1);
    std::vector<unsigned long long> grp_ptr;
    std::vector<int32_t> grp_expert, start, dst, tok, compact_claim;
    for (int64_t i = 0; i < n; ++i) {
        const int32_t e = ids[i];
        if (e < 0 || e >= hp.n_expert) continue;
        if (primary[(size_t) (e >> 3)] & (uint8_t) (1u << (e & 7))) continue;
        const int32_t slot = hp.cache.slot_of(layer, e);
        if (slot < 0) continue;
        claims.push_back((int32_t) i);
        int32_t g = -1;
        for (size_t q = 0; q < grp_expert.size(); ++q) if (grp_expert[q] == e) { g = (int32_t) q; break; }
        if (g < 0) {
            g = (int32_t) grp_expert.size();
            grp_expert.push_back(e);
            grp_ptr.push_back((unsigned long long) hp.cache.device_slot(slot));
        }
        group_of[(size_t) i] = g;
    }
    if (claims.size() > (size_t) CAP) return kErrGeometry;
    if (claims.empty()) {   // nothing resident in this window: an empty rows reply, the CPU serves it all
        RowsResp rep;
        rep.n_rows = 0;
        std::vector<uint8_t> payload(sizeof(rep), 0);
        std::memcpy(payload.data(), &rep, sizeof(rep));
        return out.write_reply(kRows, req_id, seq, payload.data(), payload.size(), deadline_after(1000));
    }
    const int32_t groups = (int32_t) grp_expert.size();
    for (int32_t g = 0; g < groups; ++g) {
        start.push_back((int32_t) dst.size());
        for (int64_t i = 0; i < n; ++i)
            if (group_of[(size_t) i] == g) {
                dst.push_back((int32_t) dst.size());
                tok.push_back((int32_t) (i / k));
                compact_claim.push_back((int32_t) i);
            }
    }
    start.push_back((int32_t) dst.size());

    // The engine quantized the activations into canonical q8_1 blocks (bit for bit its own
    // quantize_q8_1_kernel's): upload them verbatim, no requantization, and launch the grouped
    // kernel exactly the in-process tier launches it.
    if (!cuda_check(cudaMemcpyAsync(hp.d_q8, q8, (size_t) req.n_q8, cudaMemcpyHostToDevice, hp.stream),
                    "q8 upload", err)) return kErrInternal;
    std::memcpy(hp.h_meta->ptr, grp_ptr.data(), grp_ptr.size() * sizeof(grp_ptr[0]));
    std::memcpy(hp.h_meta->start, start.data(), start.size() * sizeof(start[0]));
    std::memcpy(hp.h_meta->dst, dst.data(), dst.size() * sizeof(dst[0]));
    std::memcpy(hp.h_meta->tok, tok.data(), tok.size() * sizeof(tok[0]));
    hp.h_meta->count = groups;
    if (!cuda_check(cudaMemcpyAsync(hp.d_meta, hp.h_meta, sizeof(GroupMeta), cudaMemcpyHostToDevice, hp.stream),
                    "meta upload", err)) return kErrInternal;
    const auto L = strata::kernels::native_expert_layout(fmt.gu_type, fmt.d_type, fmt.n_embd, fmt.n_ff);
    // H14: clear the output rows first - a stale value (the warmup's, a prior window's) must never
    // survive into a reply if the kernel accumulates rather than stores (r17: "helper row non-finite").
    if (!cuda_check(cudaMemsetAsync(hp.d_out, 0, (size_t) dst.size() * (size_t) H * 4, hp.stream),
                    "output clear", err)) return kErrInternal;
    // H14/r22: the scratch too - 191/2560 NaN rows came from cudaMalloc junk the fused pass
    // never wrote; a stale scratch byte must never reach a reply row.
    if (!cuda_check(cudaMemsetAsync(hp.d_scratch, 0, (size_t) hp.scratch_bytes, hp.stream),
                    "scratch clear", err)) return kErrInternal;
    strata::kernels::native_expert_grouped(L, hp.d_meta->ptr, hp.d_meta->start, &hp.d_meta->count,
                                            hp.d_meta->dst, hp.d_meta->tok, groups, (int64_t) dst.size(),
                                            (const uint8_t*) hp.d_q8, hp.d_scratch, hp.d_out, hp.stream);
    if (!cuda_check(cudaGetLastError(), "native_expert_grouped launch", err)) return kErrInternal;
    if (!cuda_check(cudaStreamSynchronize(hp.stream), "window sync", err)) return kErrInternal;

    // H14: prove the rows are finite before the engine refuses them (r17 died to exactly that).
    for (int32_t i = 0; i < (int32_t) compact_claim.size(); ++i) {
        const float* row = hp.d_out + (size_t) i * H;
        bool bad = false;
        for (int64_t c = 0; c < H; ++c) if (!std::isfinite(row[c])) { bad = true; break; }
        if (bad) {
            std::fprintf(stderr, "expert-helper: window row %d of %d (claim %d, expert %d) is non-finite "
                                 "(first floats %g %g %g %g)\n", i, (int32_t) compact_claim.size(),
                         compact_claim[i], ids[compact_claim[i]], row[0], row[1], row[2], row[3]);
            break;
        }
    }

    // The rows, in claim order, with the ORIGINAL entry indices (the engine scatters them and
    // validates every index against its own claims - a row it did not claim is refused there).
    const int32_t n_rows = (int32_t) compact_claim.size();
    const size_t row_bytes = 4 + (size_t) H * 4;
    RowsResp rep;
    rep.n_rows = n_rows;
    std::vector<uint8_t> payload(sizeof(rep) + (size_t) n_rows * row_bytes, 0);
    std::memcpy(payload.data(), &rep, sizeof(rep));
    for (int32_t i = 0; i < n_rows; ++i) {
        std::memcpy(payload.data() + sizeof(rep) + (size_t) i * row_bytes, &compact_claim[i], 4);
        std::memcpy(payload.data() + sizeof(rep) + (size_t) i * row_bytes + 4, hp.d_out + (size_t) i * H,
                    (size_t) H * 4);
    }
    const auto p_ready = prof ? std::chrono::steady_clock::now()
                              : std::chrono::steady_clock::time_point{};
    const int rc_w = out.write_reply(kRows, req_id, seq, payload.data(), payload.size(), deadline_after(10000));
    if (prof)   // L3: one line per served window frame; refusals/empty replies stay untimed
        std::fprintf(stderr, "rpcprofile: serve tier=w layer=%d rows=%d queue_us=%lld lock_us=%lld "
                             "reply_us=%lld rows_kb=%lld\n", (int) layer, n_rows,
                     (long long) p_queue_us,
                     (long long) (std::chrono::duration_cast<std::chrono::microseconds>(
                                      p_ready - p_entry).count() - p_queue_us),
                     (long long) std::chrono::duration_cast<std::chrono::microseconds>(
                                      std::chrono::steady_clock::now() - p_ready).count(),
                     (long long) ((payload.size() - sizeof(rep)) / 1024));
    return rc_w;
}

// ---- kBeginP: the selected-token PROMPT tier (R5 P1). ONE layer of the helper's window, one bounded
// row set: the SAME grouped launch serve_begin makes (native_expert_grouped, the same metadata layout,
// the same q8_1 activation contract), the tier's OWN staging buffers sized by the protocol caps, the
// H14 pre-zero clears and the finite-row scan, and the rows back with the ORIGINAL pair indices.
// Every cap is checked BEFORE anything is allocated; every refusal is this transport's typed status.
// L2: the prompt tier rides its OWN stream only when the split is on; the window tier, the
// fill epoch and the default OFF path keep the one historical stream, byte for byte.
static inline cudaStream_t prompt_stream(const Helper& hp) {
    return hp.streams_split ? hp.stream_p : hp.stream;
}

int serve_prompt(Helper& hp, const std::vector<uint8_t>& body, uint32_t req_id, uint32_t seq, std::string& err,
                 Socket& out) {
    // The prompt tier's OWN staging (h_meta_p/d_meta_p/d_q8_p/d_out_p/scratch_p) and the grouped
    // kernel it launches are shared device state: with --socket2 this frame and a window frame can
    // arrive at the same moment, on different channels.  Same lock as serve_begin, never across a
    // channel's idle read.
    // L3 (STRATA_RPC_PROFILE=1): queue_us = the head-of-line wait for the shared device lock (the
    // M7 gate's single-socket failure mechanism, now measurable per frame); the lock spans parse ->
    // (optional fp32->q8 twin quantize) -> upload -> kernel -> sync -> gather.  Default off.
    static const bool prof = std::getenv("STRATA_RPC_PROFILE") != nullptr;
    const auto p_entry = prof ? std::chrono::steady_clock::now()
                              : std::chrono::steady_clock::time_point{};
    std::unique_lock<std::mutex> gpu_lk(hp.gpu_mtx, std::defer_lock);
    std::optional<std::shared_lock<std::shared_mutex>> gpu_lk_on;
    if (hp.streams_split) gpu_lk_on.emplace(hp.gpu_gate);   // L2: a served frame is a reader
    else gpu_lk.lock();
    const int64_t p_queue_us = prof ? std::chrono::duration_cast<std::chrono::microseconds>(
                                          std::chrono::steady_clock::now() - p_entry).count() : 0;
    if (body.size() < sizeof(BeginPReq)) return kErrShort;
    BeginPReq req;
    std::memcpy(&req, body.data(), sizeof(req));
    const int64_t n_sel = req.n_sel_tok, n_pairs = req.n_pairs;
    if (n_sel < 1 || n_sel > kPromptTok || req.k != (int32_t) kK || n_pairs < 1 ||
        n_pairs > kPromptRows || (req.image_format != 0 && req.image_format != 1)) return kErrGeometry;
    const size_t res_bytes = (size_t) ((n_sel * hp.n_expert + 7) / 8);
    // M4c (b): image_format 1 = fp32 rows (n_sel * H * 4); 0 = the canonical q8_1 blocks.
    const int64_t img_expect = req.image_format == 1 ? n_sel * H * (int64_t) sizeof(float)
                                                     : n_sel * (H / 32) * 36;
    if (img_expect != req.n_q8) return kErrGeometry;
    if (body.size() != sizeof(req) + (size_t) n_pairs * 8 + res_bytes + (size_t) req.n_q8) return kErrShort;
    const int32_t* pairs = (const int32_t*) (body.data() + sizeof(req));   // (token, expert) pairs
    const uint8_t* primary = body.data() + sizeof(req) + (size_t) n_pairs * 8;
    const uint8_t* q8 = primary + res_bytes;
    const int32_t layer = req.layer;
    if (layer < hp.layer_lo || layer > hp.layer_hi) return kErrGeometry;
    const auto& lay = strata::kernels::cpu::expert_layout();
    const auto& fmt = lay.fmt[(size_t) layer];
    if (fmt.had_gu || fmt.had_d) return kErrGeometry;   // the APR rotation path is not this helper's scope
    if (!strata::kernels::native_expert_supported(fmt.gu_type, fmt.d_type, fmt.n_embd, fmt.n_ff))
        return kErrGeometry;

    // The claims: the pairs whose expert the helper holds and the primary does not own.  A pair whose
    // expert is primary-resident or not resident HERE is ABSENT from the reply - the engine's
    // finish_prompt sees the missing row and recomputes it on the CPU for this request, exactly the
    // decode contract's rule (never a silent zero, never a row the engine did not claim).
    std::vector<int32_t> claim_of;                              // compact row index -> pair index
    std::vector<int32_t> group_of((size_t) n_pairs, -1);
    std::vector<unsigned long long> grp_ptr;
    std::vector<int32_t> grp_expert, start, dst, tok, compact_claim;
    std::map<int64_t, int64_t> per_expert;
    for (int64_t i = 0; i < n_pairs; ++i) {
        const int32_t t = pairs[2 * i], e = pairs[2 * i + 1];
        if (t < 0 || t >= (int32_t) n_sel) return kErrGeometry;   // a pair with no activation row
        if (e < 0 || e >= hp.n_expert) return kErrGeometry;
        if (primary[(size_t) (e >> 3)] & (uint8_t) (1u << (e & 7))) continue;   // the primary's row
        const int32_t slot = hp.cache.slot_of(layer, e);
        if (slot < 0) continue;                                               // not resident here
        if (++per_expert[e] > (int64_t) kMaxRowsPerExpert) return kErrGeometry;
        claim_of.push_back((int32_t) i);
        int32_t g = -1;
        for (size_t q = 0; q < grp_expert.size(); ++q) if (grp_expert[q] == e) { g = (int32_t) q; break; }
        if (g < 0) {
            g = (int32_t) grp_expert.size();
            grp_expert.push_back(e);
            grp_ptr.push_back((unsigned long long) hp.cache.device_slot(slot));
        }
        group_of[(size_t) i] = g;
    }
    if (claim_of.size() > (size_t) kPromptRows) return kErrGeometry;
    if (claim_of.empty()) {   // nothing resident: an empty reply, the local path serves every row
        RowsPResp rep;
        rep.n_rows = 0;
        std::vector<uint8_t> payload(sizeof(rep), 0);
        std::memcpy(payload.data(), &rep, sizeof(rep));
        return out.write_reply(kRowsP, req_id, seq, payload.data(), payload.size(), deadline_after(1000));
    }
    const int32_t groups = (int32_t) grp_expert.size();
    for (int32_t g = 0; g < groups; ++g) {
        start.push_back((int32_t) dst.size());
        for (int64_t i = 0; i < n_pairs; ++i)
            if (group_of[(size_t) i] == g) {
                dst.push_back((int32_t) dst.size());
                tok.push_back(pairs[2 * i]);      // the q8 image row of this pair's token
                compact_claim.push_back((int32_t) i);
            }
    }
    start.push_back((int32_t) dst.size());
    if (dst.size() != claim_of.size()) return kErrInternal;   // the entry map covers every claim, once

    // The activations: image_format 0 arrives exactly as the engine quantized them (quantize_q8_1_cpu,
    // the same quantizer the decode contract pins) - upload verbatim, no requantization.  image_format
    // 1 (M4c (b)) arrives as fp32 rows: quantize HERE with the pinned twin - the same bytes for the
    // same floats - then the same upload.  The engine's critical path skipped this work entirely.
    const uint8_t* img = q8;
    const size_t up_bytes = (size_t) n_sel * (size_t) (H / 32) * 36;
    if (req.image_format == 1) {
        if (hp.h_q8_p_scratch.size() < up_bytes) hp.h_q8_p_scratch.resize(up_bytes);
        const float* fx = (const float*) q8;
        for (int64_t r = 0; r < n_sel; ++r)
            quantize_q8_1_twin(fx + (size_t) r * (size_t) H, H,
                               hp.h_q8_p_scratch.data() + (size_t) r * (size_t) (H / 32) * 36);
        img = hp.h_q8_p_scratch.data();
    }
    if (!cuda_check(cudaMemcpyAsync(hp.d_q8_p, img, up_bytes, cudaMemcpyHostToDevice, prompt_stream(hp)),
                    "prompt q8 upload", err)) return kErrInternal;
    std::memcpy(hp.h_meta_p->ptr, grp_ptr.data(), grp_ptr.size() * sizeof(grp_ptr[0]));
    std::memcpy(hp.h_meta_p->start, start.data(), start.size() * sizeof(start[0]));
    std::memcpy(hp.h_meta_p->dst, dst.data(), dst.size() * sizeof(dst[0]));
    std::memcpy(hp.h_meta_p->tok, tok.data(), tok.size() * sizeof(tok[0]));
    hp.h_meta_p->count = groups;
    if (!cuda_check(cudaMemcpyAsync(hp.d_meta_p, hp.h_meta_p, sizeof(GroupMetaP), cudaMemcpyHostToDevice, prompt_stream(hp)),
                    "prompt meta upload", err)) return kErrInternal;
    const auto L = strata::kernels::native_expert_layout(fmt.gu_type, fmt.d_type, fmt.n_embd, fmt.n_ff);
    // H14, mirrored: clear the output rows and the scratch first - a stale value from a window, from
    // the warmup, or cudaMalloc junk carrying NaN fp16 patterns must never reach a reply row.
    if (!cuda_check(cudaMemsetAsync(hp.d_out_p, 0, (size_t) dst.size() * (size_t) H * 4, prompt_stream(hp)),
                    "prompt output clear", err)) return kErrInternal;
    if (!cuda_check(cudaMemsetAsync(hp.d_scratch_p, 0, (size_t) hp.prompt_scratch_bytes, prompt_stream(hp)),
                    "prompt scratch clear", err)) return kErrInternal;
    strata::kernels::native_expert_grouped(L, hp.d_meta_p->ptr, hp.d_meta_p->start, &hp.d_meta_p->count,
                                           hp.d_meta_p->dst, hp.d_meta_p->tok, groups, (int64_t) dst.size(),
                                           (const uint8_t*) hp.d_q8_p, hp.d_scratch_p, hp.d_out_p, prompt_stream(hp));
    if (!cuda_check(cudaGetLastError(), "native_expert_grouped prompt launch", err)) return kErrInternal;
    if (!cuda_check(cudaStreamSynchronize(prompt_stream(hp)), "prompt row sync", err)) return kErrInternal;

    // H14's finite-row scan, mirrored: prove the rows before the engine refuses them.
    for (int32_t i = 0; i < (int32_t) compact_claim.size(); ++i) {
        const float* row = hp.d_out_p + (size_t) i * H;
        bool bad = false;
        for (int64_t c = 0; c < H; ++c) if (!std::isfinite(row[c])) { bad = true; break; }
        if (bad) {
            std::fprintf(stderr, "expert-helper: prompt row %d of %d (pair %d, expert %d) is non-finite "
                                 "(first floats %g %g %g %g)\n", i, (int32_t) compact_claim.size(),
                         compact_claim[i], pairs[2 * compact_claim[i] + 1], row[0], row[1], row[2], row[3]);
            break;
        }
    }

    // The rows in claim order, with the ORIGINAL pair indices (the engine validates every index
    // against its own claims and refuses a row it did not claim).
    const int32_t n_rows = (int32_t) compact_claim.size();
    const size_t row_bytes = 4 + (size_t) H * 4;
    RowsPResp rep;
    rep.n_rows = n_rows;
    std::vector<uint8_t> payload(sizeof(rep) + (size_t) n_rows * row_bytes, 0);
    std::memcpy(payload.data(), &rep, sizeof(rep));
    for (int32_t i = 0; i < n_rows; ++i) {
        std::memcpy(payload.data() + sizeof(rep) + (size_t) i * row_bytes, &compact_claim[i], 4);
        std::memcpy(payload.data() + sizeof(rep) + (size_t) i * row_bytes + 4,
                    hp.d_out_p + (size_t) i * H, (size_t) H * 4);
    }
    const auto p_ready = prof ? std::chrono::steady_clock::now()
                              : std::chrono::steady_clock::time_point{};
    const int rc_w = out.write_reply(kRowsP, req_id, seq, payload.data(), payload.size(), deadline_after(10000));
    if (prof)   // L3: one line per served prompt frame; refusals/empty replies stay untimed
        std::fprintf(stderr, "rpcprofile: serve tier=p layer=%d rows=%d queue_us=%lld lock_us=%lld "
                             "reply_us=%lld rows_kb=%lld\n", (int) layer, n_rows,
                     (long long) p_queue_us,
                     (long long) (std::chrono::duration_cast<std::chrono::microseconds>(
                                      p_ready - p_entry).count() - p_queue_us),
                     (long long) std::chrono::duration_cast<std::chrono::microseconds>(
                                      std::chrono::steady_clock::now() - p_ready).count(),
                     (long long) ((payload.size() - sizeof(rep)) / 1024));
    return rc_w;
}

// ---- M7 (the drafter lane, docs/work/MIXED_BACKEND_RPC_PLAN.md sect12/12a): kDraftCtx / kDraftReq. ----
//
// The wire packs a tap row PLANE-MAJOR (row r = n_planes * taps_hidden fp32, plane p at r*n_planes + p);
// DFlashDrafter::add_context_f32 takes them TAP-MAJOR - tap p's row r at taps[p*stride_floats + r*hidden],
// stride_floats >= rows*hidden, at most the forward's width (8) rows per call.  The staging below
// transposes while copying into ONE pinned buffer (kMaxDraftCtxRows rows), and the drafter's gather
// kernel reads that pinned buffer over PCIe directly - the same zero-copy form native_embed()'s
// embedding gather uses - so no device copy of the rows exists.
//
// Every size is checked BEFORE anything is written, like every other tier here; every refusal is this
// transport's typed status, written with the echoed sequence.  The tap COLLECTOR stays engine-side
// (generate.cpp attaches it to the verifier): the helper only consumes the rows that arrive on the wire.
void stage_draft_rows(Helper& hp, const uint8_t* wire, int64_t rows) {
    const int64_t N = hp.tap_hidden, P = hp.tap_planes;
    const float* src = (const float*) wire;
    for (int64_t r = 0; r < rows; ++r)
        for (int64_t p = 0; p < P; ++p)
            std::memcpy(hp.h_taps + p * rows * N + r * N, src + (r * P + p) * N, (size_t) N * 4);
}

int serve_draft_ctx(Helper& hp, const std::vector<uint8_t>& body, uint32_t req_id, uint32_t seq, std::string& err,
                    Socket& out) {
    // The drafter's ring, its tap staging (h_taps) and its forward are shared device state too: a
    // draft frame on channel A and a prompt frame on channel B can reach the device together.
    std::optional<std::lock_guard<std::mutex>> gpu_lk_off;
    std::optional<std::shared_lock<std::shared_mutex>> gpu_lk_on;
    if (hp.streams_split) gpu_lk_on.emplace(hp.gpu_gate);   // L2: draft frames are readers too
    else gpu_lk_off.emplace(hp.gpu_mtx);
    if (body.size() < sizeof(DraftCtxReq)) return kErrShort;
    DraftCtxReq req;
    std::memcpy(&req, body.data(), sizeof(req));
    if (req.n_rows < 1 || req.n_rows > (int32_t) kMaxDraftCtxRows) return kErrGeometry;
    if ((int64_t) req.n_planes != hp.tap_planes || (int64_t) req.taps_hidden != hp.tap_hidden) return kErrGeometry;
    if (req.reset != 0 && req.reset != 1) return kErrGeometry;
    // The ring empties logically at position 0 (add_context_f32 resets the horizon on pos0 == 0);
    // a reset naming any other position is not this drafter's contract.
    if (req.reset == 1 && req.pos0 != 0) return kErrGeometry;
    if (req.pos0 < 0 || req.n_rows > hp.draft.capacity() - req.pos0) return kErrGeometry;
    const int64_t row_floats = (int64_t) req.n_planes * hp.tap_hidden;
    if (body.size() != sizeof(req) + (size_t) req.n_rows * row_floats * 4u) return kErrShort;
    stage_draft_rows(hp, body.data() + sizeof(req), req.n_rows);
    for (int64_t r0 = 0; r0 < req.n_rows; r0 += 8) {   // the forward's width: 8 rows per call
        const int64_t nr = std::min<int64_t>(8, req.n_rows - r0);
        if (!hp.draft.add_context_f32(hp.h_taps + r0 * hp.tap_hidden, (int) req.n_planes,
                                      req.n_rows * hp.tap_hidden, req.pos0 + r0, nr, err))
            return kErrRequest;
    }
    draft_ctx_rows_served += req.n_rows;
    // the empty ack of kDraftCtx (the protocol's own payload shape)
    return out.write_reply(kDraftAck, req_id, seq, nullptr, 0, deadline_after(10000));
}

int serve_draft_round(Helper& hp, const std::vector<uint8_t>& body, uint32_t req_id, uint32_t seq,
                      std::string& err, Socket& out) {
    std::optional<std::lock_guard<std::mutex>> gpu_lk_off;
    std::optional<std::shared_lock<std::shared_mutex>> gpu_lk_on;
    if (hp.streams_split) gpu_lk_on.emplace(hp.gpu_gate);   // L2: draft frames are readers too
    else gpu_lk_off.emplace(hp.gpu_mtx);
    if (body.size() < kDraftReqLegacyBytes) return kErrShort;
    DraftReq req;
    std::memcpy(&req, body.data(), kDraftReqLegacyBytes);   // the legacy prefix; n_drafts stays 0
    if (req.n_new < 1 || req.n_new > (int32_t) kMaxDraftRound) return kErrGeometry;
    if ((int64_t) req.n_planes != hp.tap_planes || (int64_t) req.taps_hidden != hp.tap_hidden) return kErrGeometry;
    if (req.pos0 < 0 || req.n_new > hp.draft.capacity() - req.pos0) return kErrGeometry;
    if (req.anchor < 0) return kErrGeometry;
    const int64_t row_floats = (int64_t) req.n_planes * hp.tap_hidden;
    const size_t rows_bytes = (size_t) req.n_new * row_floats * 4u;
    // L1 (l1w2's 7-vs-5 mismatch): the current wire form appends int32 n_drafts after pos0 -
    // the engine's reply cap; the helper never proposes wider than it.  The 24-byte form is the
    // legacy request (no n_drafts): the helper chooses the block, exactly as before.
    int block = (int) std::min<int64_t>(hp.draft.max_block(), kMaxDraftPicks);
    size_t header_bytes = kDraftReqLegacyBytes;
    if (body.size() == sizeof(DraftReq) + rows_bytes) {
        header_bytes = sizeof(req);
        std::memcpy(&req.n_drafts, body.data() + offsetof(DraftReq, n_drafts), sizeof(req.n_drafts));
        if (req.n_drafts < 0 || req.n_drafts > (int32_t) kMaxDraftPicks) return kErrGeometry;
        if (req.n_drafts > 0) block = std::min(block, (int) req.n_drafts);
    } else if (body.size() != kDraftReqLegacyBytes + rows_bytes) {
        return kErrShort;
    }
    stage_draft_rows(hp, body.data() + header_bytes, req.n_new);
    if (!hp.draft.add_context_f32(hp.h_taps, (int) req.n_planes, req.n_new * hp.tap_hidden,
                                  req.pos0, (int64_t) req.n_new, err))
        return kErrRequest;
    // The block drafts AFTER the committed window: the anchor sits at pos0 + n_new - 1 and the query
    // rows' rope positions start at pos0 + n_new - exactly the engine's own call site's pairing
    // (add_context_f32(p - (a+1), a+1) then propose(x, p, k), src/program/generate.cpp:17802).
    const int64_t pos = req.pos0 + req.n_new;
    int32_t picks[kMaxDraftPicks];
    float probs[kMaxDraftPicks];
    if (!hp.draft.propose(req.anchor, pos, block, picks, err, probs)) {
        // A refused round is a typed refusal, never a crash and never a hang.  The recoverable
        // numeric class (the candidates left the vocabulary) says so in the name; the drafter stays
        // usable for the next round.  The loop's kError reply ends the request (plan sect12's
        // failure rule: a draft failure disables drafting for the rest of the request).
        std::fprintf(stderr, "expert-helper: draft round at pos %lld refused: %s%s\n", (long long) pos, err.c_str(),
                     strata::core::dflash_refusal_is_fatal(hp.draft.last_refusal()) ? "" : " (recoverable class)");
        return kErrRequest;
    }
    ++draft_rounds_served;
    DraftReply rep;
    rep.n_drafts = block;
    std::vector<uint8_t> payload(sizeof(rep) + (size_t) block * 8u, 0);
    std::memcpy(payload.data(), &rep, sizeof(rep));
    std::memcpy(payload.data() + sizeof(rep), picks, (size_t) block * 4u);
    std::memcpy(payload.data() + sizeof(rep) + (size_t) block * 4u, probs, (size_t) block * 4u);
    return out.write_reply(kDraftReply, req_id, seq, payload.data(), payload.size(), deadline_after(10000));
}


struct Options {
    std::string pack, socket;
    // TWO-CHANNEL (opt-in): a SECOND bound socket, served on its own thread.  The engine's window
    // tier (decode/verify rows) and its prompt tier (bulk prompt rows) are logically independent and
    // share no state; on one socket each tier's frames queue behind the other's in-flight request
    // (measured: a draft lane on this socket cost -19.1% decode throughput, and a bounded draft
    // wait timed out twice during a 78k-token prefill).  EMPTY = today's single-socket behaviour.
    std::string socket2;
    std::string experts_file = "experts.bin";
    int64_t slots = 0;                 // 0: the budget decides (the fill report stays authoritative)
    int64_t reserve_mib = kReserveDefaultMiB;
    int64_t fill_mib = 0;              // 0: the card's budget decides (today's behaviour, byte for byte)
    int64_t layers_cap = -1;           // -1: every layer the layout has
    int64_t device = 1;
    int64_t idle_timeout_ms = 120000;  // no engine connected yet: exit (the engine starts first)
    int64_t begin_timeout_ms = 60000;  // the engine is mid-window computation: a generous, bounded read
    bool once = false;
    // M7 (opt-in, plan sect12): the drafter lane.  EMPTY --dflash = no drafter, no draft frames, no
    // feature bit: every line of the draft tier stays dormant.  When set, --experts must name the
    // target's GGUF: the drafter binds the target's native head and pinned embedding from it.
    std::string dflash;
    int64_t dflash_window = 8192;      // the drafter's context capacity (the engine's --dflash-window shape)
};

bool parse(int argc, char** argv, Options& o, std::string& err) {
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        auto need = [&](const char* what) -> const char* {
            if (i + 1 >= argc) { err = std::string(what) + " needs a value"; return nullptr; }
            return argv[++i];
        };
        const char* v = nullptr;
        if (a == "--pack") { if (!(v = need("--pack"))) return false; o.pack = v; }
        else if (a == "--socket") { if (!(v = need("--socket"))) return false; o.socket = v; }
        else if (a == "--socket2") { if (!(v = need("--socket2"))) return false; o.socket2 = v; }
        else if (a == "--experts") { if (!(v = need("--experts"))) return false; o.experts_file = v; }
        else if (a == "--slots") { if (!(v = need("--slots"))) return false; o.slots = std::atoll(v); }
        else if (a == "--reserve-mib") { if (!(v = need("--reserve-mib"))) return false; o.reserve_mib = std::atoll(v); }
        else if (a == "--fill-mib") { if (!(v = need("--fill-mib"))) return false; o.fill_mib = std::atoll(v); }
        else if (a == "--layers") { if (!(v = need("--layers"))) return false; o.layers_cap = std::atoll(v); }
        else if (a == "--device") { if (!(v = need("--device"))) return false; o.device = std::atoll(v); }
        else if (a == "--timeout") { if (!(v = need("--timeout"))) return false; o.idle_timeout_ms = std::atoll(v); }
        else if (a == "--begin-timeout") { if (!(v = need("--begin-timeout"))) return false; o.begin_timeout_ms = std::atoll(v); }
        else if (a == "--dflash") { if (!(v = need("--dflash"))) return false; o.dflash = v; }
        else if (a == "--dflash-window") { if (!(v = need("--dflash-window"))) return false; o.dflash_window = std::atoll(v); }
        else if (a == "--once") o.once = true;
        else { err = "unknown argument '" + a + "' (--help for the list)"; return false; }
    }
    if (o.pack.empty() || o.socket.empty()) { err = "--pack and --socket are required"; return false; }
    if (o.slots < 0 || o.reserve_mib < 0 || o.fill_mib < 0 || o.device < 0 || o.idle_timeout_ms < 100 || o.begin_timeout_ms < 100) {
        err = "--slots/--reserve-mib/--fill-mib/--device must be >= 0 and the timeouts >= 100 ms"; return false;
    }
    if (!o.dflash.empty()) {
        // The drafter binds the target's native head and pinned embedding, which live in the model's
        // GGUF shards: --experts names them (the pack's neighbour, docs/MIXED_GPU_HELPER.md).
        if (o.experts_file.size() < 5 || o.experts_file.compare(o.experts_file.size() - 5, 5, ".gguf") != 0) {
            err = "--dflash needs --experts <model.gguf>: the drafter binds the target's head and embedding from it";
            return false;
        }
        if (o.dflash_window < 64) { err = "--dflash-window must be >= 64"; return false; }
    }
    return true;
}

void usage() {
    std::printf(
        "strata-expert-helper --pack DIR --socket PATH [--socket2 PATH2] [--experts experts.bin] [--slots N] [--layers L]\n"
        "                    [--reserve-mib 512] [--fill-mib 0] [--device 1] [--timeout 120000] [--begin-timeout 60000]\n"
        "                    [--dflash DFLASH.gguf --dflash-window 8192] [--once]\n"
        "  The CUDA-only out-of-process expert tier (docs/MIXED_GPU_HELPER.md): start it BEFORE the\n"
        "  engine, on a device the engine does not use.  It binds PATH (refusing an existing file),\n"
        "  fills the engine's ranked pairs within the card's budget, and serves decode windows.\n"
        "  --socket2 (opt-in): bind a SECOND socket and serve it on its own thread, so the engine's\n"
        "  prompt tier stops queueing behind its window tier (and the other way round).  Same frames,\n"
        "  same handshake on both sockets; the mirrored experts are shared, one kFill fills them.\n"
        "  Absent = one socket, one thread, exactly the behaviour before this option.\n"
        "  --fill-mib (opt-in): cap the fill's UPLOADED EXPERT BYTES at N MiB (0 = the card's budget\n"
        "  decides, exactly the behaviour before this option).  Every fill report names WHY it stopped\n"
        "  (slots / fill-mib / budget / ranked candidates exhausted) and the device's raw free MiB\n"
        "  before and after, so occupancy is measured, not guessed.\n"
        "  --dflash (opt-in, the M7 drafter lane): load this DFlash artifact onto this card and serve\n"
        "  the kDraftCtx/kDraftReq frames; needs --experts <model.gguf> for the target's head.\n");
}

/// Serve ONE accepted connection: accept on `listener`, run the frame loop on `out`, answer every
/// reply on `out`.  Extracted verbatim from main for the two-channel split (opt-in --socket2,
/// docs/work/MIXED_BACKEND_RPC_PLAN.md §12b): the primary channel runs it on the main thread, the
/// second channel runs the SAME code on its own thread, and each owns its accept, its idle wait, its
/// named timeouts and its exit.  The frame set, the dispatch handlers, the refusals and the
/// counters are unchanged - the protocol is identical on both sockets (kHello/kHelloAck per channel;
/// the engine sends kFill on ONE channel only and both channels serve rows from that filled state).
/// Returns 0 for a normal end (bye / idle / clean signal), the frame's status for a request failure,
/// kConnAcceptFailed when no peer arrived, kConnStop for a signal during the idle accept.
constexpr int kConnAcceptFailed = -1;
constexpr int kConnStop = -2;
int serve_channel(Helper& hp, Socket& out, Socket& listener, const Options& o, bool secondary, std::string& err) {
    bool hello_seen = false;
accept_peer:
    int fd = listener.accept(deadline_after(o.idle_timeout_ms));
    if (fd < 0) {
        if (g_stop.load()) {   // SIGTERM/SIGINT during the idle accept: exit cleanly, release the card
            std::fprintf(stderr, "expert-helper: SIGTERM/SIGINT while waiting for the engine; exiting\n");
            return kConnStop;
        }
        err = secondary ? "no engine connected on the second socket within the idle timeout"
                        : "no engine connected within the idle timeout";
        return kConnAcceptFailed;
    }
    out.close();   // a retried peer's dead descriptor must not leak (adopt does not close)
    out.adopt(fd);
    std::fprintf(stderr, "expert-helper: %s connected (peer uid %d)\n",
                 secondary ? "second-channel engine" : "engine", Socket::peer_uid(fd));

    int rc_loop = 0;
    for (;;) {
        if (g_stop.load()) {   // a signal recorded during the last window: honor it before the next read
            std::fprintf(stderr, "expert-helper: SIGTERM/SIGINT received; exiting cleanly\n");
            break;
        }
        Header head;
        std::vector<uint8_t> body;
        // NO lock is held across this read: an idle channel must never block the other channel's
        // work (that is the split's whole point, and the shared device lock is taken per frame only).
        int rc = out.read_frame(head, body, deadline_after(o.begin_timeout_ms));
        if (rc == kErrTimeout) {
            if (g_stop.load()) {   // R5 P1: the wake byte, not an idle engine (t0-incumbent-repro1)
                std::fprintf(stderr, "expert-helper: SIGTERM/SIGINT during the begin-wait; exiting cleanly\n");
                break;
            }
            std::fprintf(stderr, "expert-helper: the engine idled past the timeout; exiting\n");
            break;
        }
        if (rc != kOk) { std::fprintf(stderr, "expert-helper: channel: %s; exiting\n", status_name(rc)); break; }
        if (head.type == kBye) { std::fprintf(stderr, "expert-helper: the engine said bye; exiting\n"); break; }
        if (head.type == kHello) {
            if (body.size() < sizeof(HelloReq)) { rc = kErrShort; }
            else {
                HelloReq req;
                std::memcpy(&req, body.data(), sizeof(req));
                const int64_t claimed_bytes = (int64_t) req.claimed_bytes;
                if (req.reserved0 != 0 || body.size() != sizeof(req) + (size_t) claimed_bytes) rc = kErrShort;
                else if (req.n_embd != H || req.n_ff != FF || req.n_layers != hp.n_layers ||
                         req.n_expert != hp.n_expert) rc = kErrGeometry;
                else if (req.layer_lo < 0 || req.layer_hi < req.layer_lo || req.layer_hi >= hp.n_layers)
                    rc = kErrGeometry;
                else if (req.slots <= 0) rc = kErrGeometry;
                else {
                    hp.layer_lo = req.layer_lo;
                    hp.layer_hi = req.layer_hi;
                    HelloAck ack;
                    ack.max_tok = (int32_t) kMaxTok;
                    ack.k = (int32_t) kK;
                    ack.max_entries = (int32_t) kMaxEntries;
                    ack.status = 0;
                    // R5 P1: advertise the selected-token prompt tier (the kBeginP/kRowsP family this
                    // helper serves, bounded by kMaxSelTok / kMaxRowsPerLayer).  Appended field: an R2
                    // client reads its 24-byte prefix and ignores this; a new client that sees the bit
                    // may use the tier, one that does not (an R2 helper) never sends kBeginP.
                    ack.features = kFeaturePromptRows | kFeaturePromptF32;   // M4c (b): fp32 images accepted
                    // M7: the drafter lane is advertised ONLY when the drafter loaded, uploaded and
                    // bound - a peer that never advertised kFeatureDraft never sends kDraftCtx/kDraftReq,
                    // and one that did gets this transport's typed refusal if it sends them to a helper
                    // without a drafter (the frame types exist, the service does not).
                    if (hp.draft_ready) ack.features |= kFeatureDraft;
                    size_t free_b = 0, total_b = 0;
                    cudaMemGetInfo(&free_b, &total_b);
                    ack.free_mib = (int64_t) free_b / 1048576ll;
                    rc = out.write_reply(kHelloAck, head.req_id, head.seq, (const uint8_t*) &ack, sizeof(ack),
                                              deadline_after(5000));
                    // Remember the claimed bitmap for the fill (the engine sends it with kHello).  The wire
                    // packs bit-per-pair (protocol: flat/8, bit 1 = a stage holds it); serve_fill indexes
                    // byte-per-pair, so unpack here.  H8: it used to be stored raw, and every pair of the
                    // helper's range (flat >= 16384 > 3072 packed bytes) read past it as unclaimed.  The
                    // unpack is the shared transport's own function (helper_frame.cpp) so the host CPU test
                    // pins these exact semantics with no GPU in sight.
                    // Two channels: BOTH hellos carry the engine's identical claimed bitmap (the engine
                    // sends the same geometry on both), so this is the same write of the same bytes.
                    unpack_claimed_bitmap(body.data() + sizeof(req), (size_t) claimed_bytes,
                                          (size_t) hp.n_layers * (size_t) hp.n_expert, g_claimed);
                    g_reserve = req.reserve_mib;
                    g_slots = req.slots;
                    hello_seen = true;   // from here on the connection is the engine's, one-shot again
                }
            }
            if (rc != kOk) { rc_loop = rc; break; }
            continue;
        }
        if (head.type == kFill) {
            rc = serve_fill(hp, body, head.req_id, head.seq, g_reserve, g_claimed,
                            g_slots > 0 ? g_slots : INT64_MAX, err, out);
            if (rc != kOk) {
                ErrResp e;
                e.status = rc;
                std::strncpy(e.text, err.c_str(), sizeof(e.text) - 1);
                out.write_reply(kError, head.req_id, head.seq, (const uint8_t*) &e, sizeof(e), deadline_after(5000));
                rc_loop = rc;
                break;
            }
            filled = true;
            continue;
        }
        if (head.type == kBegin) {
            if (!filled) { rc = kErrRequest; }
            else rc = serve_begin(hp, body, head.req_id, head.seq, err, out);
            if (rc != kOk) {
                ErrResp e;
                e.status = rc;
                std::strncpy(e.text, err.c_str(), sizeof(e.text) - 1);
                out.write_reply(kError, head.req_id, head.seq, (const uint8_t*) &e, sizeof(e), deadline_after(5000));
                rc_loop = rc;
                break;
            }
            ++windows_served;
            if (o.once) { std::fprintf(stderr, "expert-helper: --once: one window served, exiting\n"); break; }
            continue;
        }
        if (head.type == kBeginP) {
            // R5 P1 (the prompt tier).  Served ONLY after the engine's own gate: this helper
            // advertised kFeaturePromptRows in its kHelloAck, and a client that saw no feature bit
            // refuses kBeginP locally and never puts it on the wire.  Two channels: the filled state
            // is the SAME on both (the fill travels on one channel), so a kBeginP on the second
            // channel is served from the slots the first channel's fill created.
            if (!filled) { rc = kErrRequest; }
            else rc = serve_prompt(hp, body, head.req_id, head.seq, err, out);
            if (rc != kOk) {
                ErrResp e;
                e.status = rc;
                std::strncpy(e.text, err.c_str(), sizeof(e.text) - 1);
                out.write_reply(kError, head.req_id, head.seq, (const uint8_t*) &e, sizeof(e), deadline_after(5000));
                rc_loop = rc;
                break;
            }
            ++prompt_requests_served;
            if (o.once) { std::fprintf(stderr, "expert-helper: --once: one prompt row request served, exiting\n"); break; }
            continue;
        }
        if (head.type == kDraftCtx || head.type == kDraftReq) {
            // M7 (the drafter lane): served ONLY after the engine's own gate - this helper advertised
            // kFeatureDraft in its kHelloAck, and a client that saw no bit never sends these frames.
            // A refusal answers with the transport's typed kError (the engine disables drafting for
            // the rest of the request, plan sect12); the loop ends through the existing error path,
            // which releases the card and unlinks only this helper's own socket - never a crash,
            // never a hang.
            if (!filled || !hp.draft_ready) { rc = kErrRequest; err = "no drafter is loaded"; }
            else if (head.type == kDraftCtx) rc = serve_draft_ctx(hp, body, head.req_id, head.seq, err, out);
            else rc = serve_draft_round(hp, body, head.req_id, head.seq, err, out);
            if (rc != kOk) {
                ErrResp e;
                e.status = rc;
                std::strncpy(e.text, err.c_str(), sizeof(e.text) - 1);
                out.write_reply(kError, head.req_id, head.seq, (const uint8_t*) &e, sizeof(e), deadline_after(5000));
                // M7 (plan sect12a, BUG B): a draft-frame refusal is an APPLICATION-level refusal
                // of the draft lane, NOT a transport failure.  The shared channel carries the window
                // and prompt tiers too; ending the connection here would kill them (the live A/B
                // died as "channel closed after 38 requests / 355 rows").  Write the typed kError
                // and KEEP SERVING - the engine's client disables drafting for the request, and the
                // window tier's next begin finds the channel alive.  A malformed frame (kErrShort)
                // is transport-level and keeps the fatal rule (end the loop).
                if (rc == kErrShort) { rc_loop = rc; break; }
                std::fprintf(stderr, "expert-helper: draft frame refused (%s); the draft lane is off, "
                                     "the channel stays open\n", status_name(rc));
                continue;
            }
            if (o.once) { std::fprintf(stderr, "expert-helper: --once: one draft request served, exiting\n"); break; }
            continue;
        }
        // An unexpected frame type is REFUSED BY NAME on the channel before the loop ends: the peer
        // gets this transport's typed status, never a silent close to interpret.  (An R2 helper that
        // predates kBeginP has no reply to write: it closes the channel with kErrProtocol, which the
        // engine's client reports as a named kErrUnhealthy - also a typed refusal, also no hang, and
        // the client never sends an unadvertised frame type in the first place.)
        ErrResp e;
        e.status = kErrProtocol;
        std::snprintf(e.text, sizeof(e.text), "frame type %u is not part of this protocol", head.type);
        out.write_reply(kError, head.req_id, head.seq, (const uint8_t*) &e, sizeof(e), deadline_after(5000));
        rc_loop = kErrProtocol;   // an unexpected frame type: never interpret it
        break;
    }
    if (rc_loop != 0) std::fprintf(stderr, "expert-helper: %srequest failed: %s\n",
                                   secondary ? "second channel: " : "", status_name(rc_loop));
    // R5 P1 live test (2026-10-29): the latched stop WINS over this edge.  The helper logged its
    // "exiting cleanly" line ~10 times in a row and survived 60 s of SIGTERM to a SIGKILL, because the
    // pre-hello re-listen below re-bound the socket every time the latch ended the loop.  The predicate
    // is the shared one (helper_frame.cpp), pinned by tests/core/expert_helper_socket_test.cpp.
    // The SECOND channel never re-listens: the engine connects it once at startup and its absence is
    // the single-channel case, not a peer to wait for.
    if (!secondary && helper_relisten_allowed(g_stop.load() != 0, hello_seen, o.once)) {
        // A pre-hello peer (a socket prober, or an engine startup that dies before opening the
        // helper) must not consume the helper's single accept: drop it and listen again.
        std::fprintf(stderr, "expert-helper: the peer left before hello; NOT exiting, listening again\n");
        out.close();
        g_claimed.clear();
        g_reserve = kReserveDefaultMiB;
        g_slots = 0;
        filled = false;
        rc_loop = 0;
        goto accept_peer;
    }
    return rc_loop;
}

}  // namespace

int main(int argc, char** argv) {
    // The wake pipe FIRST: a signal that arrives before the handlers exist kills the process with
    // its default disposition (the correct behaviour of an un-setup helper); once the handlers exist
    // the pipe is already there, so no signal is ever recorded without a wake source.
    if (::pipe2(g_wake_pipe, O_CLOEXEC | O_NONBLOCK) != 0) {
        std::perror("expert-helper: pipe2 (the SIGTERM wake)");
        return 1;
    }
    set_poll_wake(g_wake_pipe[0]);
    std::signal(SIGTERM, on_signal);
    std::signal(SIGINT, on_signal);
    // H11: the engine's client closes the channel when a window reply misses its deadline; a
    // reply written into that close must surface as an EPIPE write error (clean exit through the
    // loop's error path), never kill the helper by SIGPIPE.
    std::signal(SIGPIPE, SIG_IGN);
    if (argc > 1 && std::strcmp(argv[1], "--help") == 0) { usage(); return 0; }
    Options o;
    std::string err;
    if (!parse(argc, argv, o, err)) return fail("arguments", err);
    g_fill_mib = o.fill_mib;      // process-wide: the operator's occupancy cap, unlike the hello-bound g_slots

    int n_dev = 0;
    // CUDA 13 dropped the runtime device-management calls (cudaDeviceGetCount is gone from
    // cuda_runtime_api.h); cudaGetDeviceCount is the runtime name this toolkit carries.
    if (cudaGetDeviceCount(&n_dev) != cudaSuccess || o.device >= n_dev) {
        cudaGetLastError();
        return fail("device", "no such CUDA device (see nvidia-smi; the engine must not use it)");
    }
    if (!cuda_check(cudaSetDevice((int) o.device), "cudaSetDevice", err)) return fail("device", err);
    if (!cuda_check(cudaFree(nullptr), "context init", err)) return fail("device", err);

    if (!strata::kernels::cpu::expert_layout_load(o.pack, 48, 512, err)) return fail("layout", err);
    const auto& lay = strata::kernels::cpu::expert_layout();
    if (!lay.native) { err = "the pack is not a native (IQ) expert pack (no native_experts.txt)"; return fail("layout", err); }
    Helper hp;
    hp.n_layers = lay.n_layers;
    hp.n_expert = lay.n_expert;
    if (hp.n_layers < 1 || hp.n_expert < 1) return fail("layout", "the layout names no layers/experts");
    for (int64_t l = 0; l < lay.n_layers; ++l) {
        const auto& f = lay.fmt[(size_t) l];
        if (f.n_embd != H || f.n_ff != FF) {
            err = "layer " + std::to_string(l) + " is not the 2560x640 geometry this build compiles for";
            return fail("layout", err);
        }
        if (f.had_gu || f.had_d) {
            err = "layer " + std::to_string(l) + " is Hadamard-folded: out of the helper's scope (APR is the in-process tier's path)";
            return fail("layout", err);
        }
        if (!strata::kernels::native_expert_supported(f.gu_type, f.d_type, f.n_embd, f.n_ff)) {
            err = "layer " + std::to_string(l) + " has no grouped kernel for its gate/up + down types";
            return fail("layout", err);
        }
    }
    {
        std::string p = o.pack;
        if (!p.empty() && p.back() != '/') p += "/";
        p += o.experts_file;
        if (!hp.pack.open(p, err)) return fail("pack", err);
    }    hp.scratch_bytes = (int64_t) std::max<size_t>(
        (size_t) strata::kernels::moe_hit_grouped_scratch_bytes(CAP, H, FF),
        strata::kernels::native_expert_scratch_bytes(CAP, FF));
    hp.buffers_bytes = (int64_t) (CAP * (H / 32) * 36 + CAP * H * 4 + sizeof(GroupMeta) + (2u << 20));
    if (!cuda_check(cudaStreamCreateWithFlags(&hp.stream, cudaStreamNonBlocking), "stream", err)) return fail("buffers", err);
    if (std::getenv("STRATA_HELPER_STREAMS")) {             // L2: opt-in, default OFF
        if (!cuda_check(cudaStreamCreateWithFlags(&hp.stream_p, cudaStreamNonBlocking), "stream_p", err))
            return fail("buffers", err);
        hp.streams_split = true;
        std::fprintf(stderr, "expert-helper: L2 stream split on (window/draft on the primary "
                             "stream, prompt on its own; fill fences both)\n");
    }
    if (!cuda_check(cudaHostAlloc((void**) &hp.h_meta, sizeof(GroupMeta), cudaHostAllocPortable | cudaHostAllocMapped),
                    "meta staging", err)) return fail("buffers", err);
    // H10: the launch below reads d_meta's FIELDS on the host (->ptr/->start/...), so d_meta must be
    // host-accessable: plain cudaMalloc is device memory, and a host deref of it only works while the
    // driver happens to keep the page mapped into the CPU (it flickered under fill churn -> SIGSEGV).
    if (!cuda_check(cudaHostAlloc((void**) &hp.d_meta, sizeof(GroupMeta), cudaHostAllocPortable | cudaHostAllocMapped),
                    "meta (pinned+mapped: serve_begin reads its fields on the host)", err)) return fail("buffers", err);
    if (!cuda_check(cudaMalloc((void**) &hp.d_q8, (size_t) CAP * (H / 32) * 36), "activation", err)) return fail("buffers", err);
    // H10: serve_begin's row loop copies the rows out with a HOST memcpy - d_out must be pinned
    // host memory (mapped, so the grouped kernel writes it over PCIe like the in-process tier's
    // pinned rows), never plain cudaMalloc.
    if (!cuda_check(cudaHostAlloc((void**) &hp.d_out, (size_t) CAP * H * 4, cudaHostAllocPortable | cudaHostAllocMapped),
                    "rows (pinned+mapped: the host reads the rows back)", err)) return fail("buffers", err);
    if (!cuda_check(cudaMalloc(&hp.d_scratch, (size_t) hp.scratch_bytes), "scratch", err)) return fail("buffers", err);
    // R5 P1: the prompt tier's staging, sized by the PROTOCOL caps (256 rows / 256 selected tokens),
    // never by T - the request's size is a constant, so these bytes are the tier's whole footprint.
    // Counted into buffers_bytes BEFORE the fill: the card's fill budget is what is left after the
    // helper's own buffers, so a tier the budget cannot afford must be seen at fill time, not at the
    // first kBeginP.
    hp.prompt_scratch_bytes = (int64_t) std::max<size_t>(
        (size_t) strata::kernels::moe_hit_grouped_scratch_bytes(kPromptRows, H, FF),
        strata::kernels::native_expert_scratch_bytes(kPromptRows, FF));
    hp.prompt_buffers_bytes = (int64_t) ((int64_t) kPromptTok * (H / 32) * 36 + (int64_t) kPromptRows * H * 4
                                          + 2 * (int64_t) sizeof(GroupMetaP) + hp.prompt_scratch_bytes);
    hp.buffers_bytes += hp.prompt_buffers_bytes;
    if (!cuda_check(cudaHostAlloc((void**) &hp.h_meta_p, sizeof(GroupMetaP), cudaHostAllocPortable | cudaHostAllocMapped),
                    "prompt meta staging", err)) return fail("buffers", err);
    if (!cuda_check(cudaHostAlloc((void**) &hp.d_meta_p, sizeof(GroupMetaP), cudaHostAllocPortable | cudaHostAllocMapped),
                    "prompt meta (pinned+mapped: serve_prompt reads its fields on the host)", err)) return fail("buffers", err);
    if (!cuda_check(cudaMalloc((void**) &hp.d_q8_p, (size_t) kPromptTok * (H / 32) * 36), "prompt activation", err))
        return fail("buffers", err);
    if (!cuda_check(cudaHostAlloc((void**) &hp.d_out_p, (size_t) kPromptRows * H * 4,
                                  cudaHostAllocPortable | cudaHostAllocMapped),
                    "prompt rows (pinned+mapped: the host reads the rows back)", err)) return fail("buffers", err);
    if (!cuda_check(cudaMalloc(&hp.d_scratch_p, (size_t) hp.prompt_scratch_bytes), "prompt scratch", err))
        return fail("buffers", err);

    // ---- M7 (the drafter lane, opt-in; docs/work/MIXED_BACKEND_RPC_PLAN.md sect12/12a) ----
    // Load, upload and bind the drafter HERE, before the socket is bound: kFill computes its budget
    // from the card's live free memory, so a drafter that is already resident shrinks the mirrored
    // expert slots by exactly its footprint - the engine's own rule, paid once at startup
    // (generate.cpp:5764 sizes the cache around the drafter the same way).  The tap COLLECTOR stays
    // engine-side; the helper only consumes the tap rows the wire brings.  With no --dflash nothing
    // in this block runs and every byte of the helper's behaviour is what it was.
    if (!o.dflash.empty()) {
        strata::core::ModelGeometry g = strata::core::kCompiledGeometry;
        g.n_layers = hp.n_layers;
        g.n_expert = hp.n_expert;
        if (!hp.draft.load(o.dflash, err)) return fail("dflash", err);
        const strata::core::DFlashGeometry& dg = hp.draft.artifact().geom();
        if (!strata::core::DFlashArtifact::validate_supported(dg, err)) return fail("dflash", err);
        if (dg.hidden != g.n_embd || dg.vocab != g.n_vocab) {
            err = "the artifact is hidden " + std::to_string(dg.hidden) + " / vocab " + std::to_string(dg.vocab) +
                  ", this build's target geometry is " + std::to_string(g.n_embd) + " / " + std::to_string(g.n_vocab);
            return fail("dflash", err);
        }
        if (dg.mask_token_id < 0) { err = "the artifact declares no dflash.mask_token_id"; return fail("dflash", err); }
        // The drafter's upload reads ONE thing of the target session: the primary QSA state's pool
        // shapes and max_cells.  The helper runs no target layer, so this minimal primary state -
        // direct attention, owned fp16 K/V, its own rope table - is the whole session here.
        const int64_t cells = (o.dflash_window + 63) / 64 * 64;
        strata::core::QsaStateInitOptions qopts;
        qopts.no_indexer = true;
        qopts.disable_streaming = true;
        qopts.disable_elastic = true;
        qopts.force_owned_kv = true;
        qopts.force_f16_kv = true;
        const uint64_t qb = strata::core::qsa_state_bytes(g, cells, true, 0, qopts);
        if (!cuda_check(cudaMalloc(&hp.qsa_arena, (size_t) qb), "draft session arena", err)) return fail("dflash", err);
        if (strata::core::qsa_state_init(g, cells, hp.qsa_arena, hp.qsa_primary, nullptr, 0, qopts) == 0) {
            cudaGetLastError();
            err = "the draft session's primary QSA state init failed";
            return fail("dflash", err);
        }
        strata::core::SessionState ss{};
        ss.max_cells = cells;
        ss.qsa_states = &hp.qsa_primary;
        ss.qsa_alloc = 1;
        ss.qsa_ord0 = 0;
        // window -1: the artifact's own geometry decides (the official bounded ring), exactly the
        // CLI's unset --dflash-window; mask -1: the artifact's metadata decides.
        if (!hp.draft.upload(g, ss, (int) o.device, -1, -1, err)) return fail("dflash", err);
        // The bind's two target modules, from the model's GGUF shards (--experts names them):
        // the full-vocabulary native head (device) and the pinned-host embedding gather (no VRAM).
        std::string shard_path = o.pack;
        if (!shard_path.empty() && shard_path.back() != '/') shard_path += "/";
        shard_path += o.experts_file;
        const std::vector<std::string> shards = strata::gguf_split_paths(shard_path);
        if (!hp.draft_head.load(shards, g.n_embd, g.n_vocab, err)) return fail("dflash", err);
        if (!hp.draft_emb.load(shards, g.n_embd, g.n_vocab, err)) return fail("dflash", err);
        strata::core::set_native_embed(&hp.draft_emb);
        // The bind reads `token_embd.weight`'s METADATA (ne0/ne1) and, with native_embed() set,
        // never touches its bytes: skip every tensor, so the table costs no VRAM at all.
        std::set<std::string> skip;
        {
            const std::string idx = o.pack + "/index.txt";
            std::FILE* f = std::fopen(idx.c_str(), "rb");
            if (!f) { err = "cannot open " + idx + " (the drafter needs the pack's index)"; return fail("dflash", err); }
            char line[1024];
            while (std::fgets(line, sizeof line, f)) {
                if (line[0] == '#') continue;
                char name[256] = {0};
                if (std::sscanf(line, "%255s", name) == 1) skip.insert(name);
            }
            std::fclose(f);
        }
        if (skip.empty()) { err = o.pack + "/index.txt names no tensors"; return fail("dflash", err); }
        if (!hp.draft_wt.load(o.pack, nullptr, 0, err, &skip)) return fail("dflash", err);
        if (!hp.draft.bind(hp.draft_wt, &hp.draft_head, err)) return fail("dflash", err);
        // The pinned tap staging: the protocol's largest context frame, tap-major (see
        // stage_draft_rows).  Pinned+mapped: the drafter's gather kernel reads it over PCIe directly.
        hp.tap_planes = (int64_t) dg.target_layers.size();
        hp.tap_hidden = dg.hidden;
        const size_t tap_bytes = (size_t) kMaxDraftCtxRows * (size_t) hp.tap_planes * (size_t) hp.tap_hidden * 4u;
        if (!cuda_check(cudaHostAlloc((void**) &hp.h_taps, tap_bytes, cudaHostAllocPortable | cudaHostAllocMapped),
                        "draft tap staging", err)) return fail("dflash", err);
        hp.draft_ready = true;
        std::fprintf(stderr, "expert-helper: dflash %s: %lld planes x %lld hidden, %.1f MiB of VRAM held "
                             "(weights, pools, scratch), %.1f MiB pinned tap staging, capacity %lld cells, "
                             "block %d\n",
                     o.dflash.c_str(), (long long) hp.tap_planes, (long long) hp.tap_hidden,
                     (double) hp.draft.vram_bytes() / 1048576.0, (double) tap_bytes / 1048576.0,
                     (long long) hp.draft.capacity(), hp.draft.max_block());
    }

    std::string why;
    if (!socket_path_ok(o.socket, why)) return fail("socket", why);
    if (hp.listener.listen_bind(o.socket) != kOk) { err = "cannot bind " + o.socket; return fail("socket", err); }
    std::fprintf(stderr, "expert-helper: listening on %s (device %lld, pack %s, %lld layers x %lld experts)\n",
                 o.socket.c_str(), (long long) o.device, o.pack.c_str(), (long long) hp.n_layers,
                 (long long) hp.n_expert);

    // ---- Two channels (opt-in --socket2, docs/work/MIXED_BACKEND_RPC_PLAN.md sect12b) ----
    // With --socket2 the helper binds a SECOND socket and serves it on its own thread: the engine's
    // WINDOW tier (kBegin/kRows, plus the drafter lane's frames) rides --socket, its PROMPT tier
    // (kBeginP/kRowsP) rides --socket2, and a frame on one channel stops queuing behind the other
    // channel's in-flight request.  The device state is shared and serialized by hp.gpu_mtx for the
    // duration of ONE served frame (serve_begin/serve_prompt/serve_fill/serve_draft_*); no lock is
    // ever held across a channel's idle read_frame, which is what lets one channel's socket I/O
    // overlap the other's GPU work.
    // WITHOUT --socket2 nothing here exists: one bound socket, one accept, one thread - byte for
    // byte the behaviour of every build before this change.
    if (!o.socket2.empty()) {
        std::string w2;
        if (!socket_path_ok(o.socket2, w2)) return fail("socket2", w2);
        if (o.socket2 == o.socket)
            return fail("socket2", "--socket2 must name a different path than --socket");
        if (hp.listener2.listen_bind(o.socket2) != kOk) {
            err = "cannot bind " + o.socket2;
            return fail("socket2", err);
        }
        std::fprintf(stderr, "expert-helper: second channel listening on %s (the engine's prompt tier "
                             "rides it; the window tier stays on %s)\n",
                     o.socket2.c_str(), o.socket.c_str());
        hp.chan2 = std::thread([&hp, &o]() {
            std::string e2;
            const int rc2 = serve_channel(hp, hp.sock2, hp.listener2, o, true, e2);
            if (rc2 == kConnAcceptFailed)
                std::fprintf(stderr, "expert-helper: %s (the engine keeps the prompt tier on its "
                                     "primary channel; the window tier is unaffected)\n", e2.c_str());
            else if (rc2 != 0 && rc2 != kConnStop)
                std::fprintf(stderr, "expert-helper: second channel ended: %s\n", status_name(rc2));
        });
    }

    const int conn_rc = serve_channel(hp, hp.sock, hp.listener, o, false, err);
    // END: wake the second channel (the wake byte makes its poll answer the value an expired
    // deadline answers, and its loop takes that named exit), JOIN it, and only THEN release the
    // card - the join is what keeps the device free of a serve thread that is still launching.
    g_stop.store(1);
    if (g_wake_pipe[1] >= 0) {
        const char byte = 1;
        const int saved = errno;
        const ssize_t woke = ::write(g_wake_pipe[1], &byte, 1);   // one byte: wakes the channel polls
        (void) woke;                                              // best-effort, as the signal handler's is
        errno = saved;
    }
    if (hp.chan2.joinable()) hp.chan2.join();

    if (conn_rc == kConnStop) { hp.release(); return 0; }
    if (conn_rc == kConnAcceptFailed) return fail("accept", err);
    std::fprintf(stderr, "expert-helper: %lld windows served, %lld prompt row requests%s; releasing the card\n",
                 (long long) windows_served.load(), (long long) prompt_requests_served.load(),
                 o.dflash.empty() ? ""
                                  : (std::string(", ") + std::to_string((long long) draft_ctx_rows_served.load()) +
                                     " draft context rows, " + std::to_string((long long) draft_rounds_served.load()) +
                                     " draft rounds").c_str());
    hp.release();
    return conn_rc == 0 ? 0 : 1;
}

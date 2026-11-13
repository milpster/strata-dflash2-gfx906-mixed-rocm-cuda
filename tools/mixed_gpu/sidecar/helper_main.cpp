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
//
// W2 (the reply ring; default OFF - STRATA_HELPER_REPLY_RING, and only for hellos that ask):
// rows replies on a negotiated channel travel as the tiny kRowsRing doorbell while the rows
// themselves ride a memfd-shared pinned ring (see the protocol header's ring contract).  W4 rides
// the same lane: serve_begin releases the device lock AT LAUNCH and waits its own frame's event -
// correct BECAUSE the ring gives each channel its own output slot (the shared d_out may already be
// rewritten by the next frame when the scan runs; the records in the slot cannot be).
#ifndef _GNU_SOURCE
#define _GNU_SOURCE   // memfd_create (the reply ring's shared backing file; glibc guards it)
#endif
#include "strata/core/expert_helper_protocol.hpp"
#include "strata/core/expert_helper_reserve.hpp"   // v0232: --reserve-mib's honest clamp (this file owns the fill budget's reserve)
#include "strata/core/expert_cache.hpp"
#include "strata/core/finite_rows.hpp"   // helper-ipc-diet: the AVX2 twin of the row finiteness scan

#include <cuda_fp16.h>   // W1: __float2half_rn - the GPU quant twin's fp16 scales (round-to-nearest-even, ggml's CPU contract)
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
#include <cerrno>
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

// 2026-11-06: the Nov 6 17:47 prod chain - the engine's prompt lane froze ~90 s in a reclaim-driven
// HIP queue suspension (XDMA no-retry faults in the kernel log), its socket stopped draining, and
// the helper's 10 s kRowsP write deadline closed the second channel ("helper timeout"); the engine
// fell back to the CPU for the request and the supervisor rebuilt the whole pair (3 generations,
// all KV lost).  The reply-write deadlines are the helper's patience with a SLOW-BUT-ALIVE engine;
// a dead one is POLLHUP, detected immediately at any deadline.  STRATA_HELPER_REPLY_TIMEOUT_MS
// (>= 100, the codebase's own floor) overrides every data-reply deadline at once; unset keeps each
// call's own default, byte for byte.
int64_t reply_override_ms() {
    static const int64_t v = [] {
        const char* e = std::getenv("STRATA_HELPER_REPLY_TIMEOUT_MS");
        const int64_t parsed = e == nullptr ? 0 : std::atoll(e);
        return parsed >= 100 ? parsed : 0;
    }();
    return v;
}
int64_t reply_deadline_ms(int64_t dflt) {
    const int64_t ov = reply_override_ms();
    return ov > 0 ? ov : dflt;
}
constexpr int64_t kReserveDefaultMiB = 512;

// W3.1/W3.3 (STRATA_HELPER_WIN_FAST, default OFF - exactly "1"): the helper's half of the bundle
// is the d_out pre-clear drop below; the ENGINE's half (the window payload's per-frame rebuild and
// the q8/q8k image's zero-fill) reads the same knob in its own process.  Read once at first use:
// the helper's environment is fixed for the process's lifetime (reply_override_ms's own pattern).
bool win_fast() {
    static const bool v = [] {
        const char* e = std::getenv("STRATA_HELPER_WIN_FAST");
        return e != nullptr && e[0] == '1' && e[1] == '\0';
    }();
    return v;
}

// W3.2 (STRATA_HELPER_PINNED_RX, default OFF - exactly "1"): the window image's H2D runs from a
// PINNED staging slot instead of the channel's pageable RxBuffer (the driver stages pageable
// copies through a bounce buffer; pinned memory is a direct DMA).  Gated, not free: the pinned
// allocation is observable under host memory pressure (cudaHostAlloc can fail where malloc would
// not), so default-off keeps today's allocation behavior exactly.  Read once, as win_fast.
bool pinned_rx() {
    static const bool v = [] {
        const char* e = std::getenv("STRATA_HELPER_PINNED_RX");
        return e != nullptr && e[0] == '1' && e[1] == '\0';
    }();
    return v;
}

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

// W1 (kBeginF32): the GPU twin of quantize_q8_1_cpu for the WINDOW wire - one thread per 32-value
// block, every statement the CPU quantizer's own, in its exact order:
//   - amax: fmaxf(amax, fabsf(xb[i])) accumulated i = 0..31 sequentially - max is associative, so
//     ANY reduction order gives it; this one also matches the CPU loop verbatim.
//   - sum: `sum += xb[i]` in the SAME i = 0..31 order as the CPU loop (NOT a butterfly): IEEE-754
//     fp32 addition with round-to-nearest, one add at a time, so the result is bit-identical to
//     the CPU's sum by construction - stronger than the prompt tier's twin, which is ULP-close
//     because the DEVICE kernel there reduces with a shuffle tree.
//   - d: amax/127.0f with the #606 clamp (|v| > 65504 -> +-65504) - q8_1_finite.hpp's own bits.
//   - ds: __float2half_rn (round-to-nearest-EVEN) of the clamped d and sum - ggml's
//     ggml_fp32_to_fp16 CPU contract for the normal range, restated on the device.
//   - q: roundf(xb[i]/d) clamped to +-127; the all-zero block (amax == 0) writes zeros.
// The helper is compiled WITHOUT -use_fast_math/-ftz (the CMake target sets no CUDA math flags),
// so nvcc's IEEE defaults hold: --ftz=false, --prec-div=true; a bare `a += b` is a single IEEE
// add (nvcc's default FMA contraction applies to a*b+c patterns, of which this body has none).
// The golden pin is byte-identity against quantize_q8_1_cpu: STRATA_HELPER_F32_QUANT_DUMP=<path>
// (below) dumps the first frame's input and d_q8 bytes, and
// tests/core/expert_helper_f32_window_test.cpp compares them byte for byte - a single failed
// golden compare reverts STRATA_HELPER_F32_WINDOW, and no output can move (the grouped kernel
// then reads exactly the bytes this kernel wrote in place of the engine's).
// The #606 clamp constant, the same bits as quantize_q8_1_cpu's kHalfMax (that function is host
// C++; this TU's device half needs its own).
constexpr float kF32HalfMax = 65504.0f;

__global__ void __launch_bounds__(256) quantize_q8_1_f32_window_kernel(const float* __restrict__ x,
                                                                       uint8_t* __restrict__ y,
                                                                       long long n_blocks) {
    const long long ib = (long long) blockIdx.x * blockDim.x + threadIdx.x;
    if (ib >= n_blocks) return;
    const float* xb = x + ib * 32;
    float amax = 0.0f, sum = 0.0f;
    for (int i = 0; i < 32; ++i) {
        amax = fmaxf(amax, fabsf(xb[i]));
        sum += xb[i];
    }
    float d = amax / 127.0f;
    if (fabsf(d) > kF32HalfMax) d = copysignf(kF32HalfMax, d);
    float dv = d, sv = sum;   // ds[0] re-clamps d, ds[1] clamps sum (the CPU's finite() calls)
    if (fabsf(dv) > kF32HalfMax) dv = copysignf(kF32HalfMax, dv);
    if (fabsf(sv) > kF32HalfMax) sv = copysignf(kF32HalfMax, sv);
    const __half d0 = __float2half_rn(dv);
    const __half s0 = __float2half_rn(sv);
    std::memcpy(y + ib * 36, &d0, 2);
    std::memcpy(y + ib * 36 + 2, &s0, 2);
    int8_t* qs = (int8_t*) (y + ib * 36 + 4);
    for (int i = 0; i < 32; ++i) {
        int8_t q;
        if (amax == 0.0f) q = 0;
        else {
            const float qf = roundf(xb[i] / d);
            q = (int8_t) (qf > 127.0f ? 127.0f : qf < -127.0f ? -127.0f : qf);
        }
        qs[i] = q;
    }
}

// W1's golden-pin dump: with STRATA_HELPER_F32_QUANT_DUMP=<path> set, the FIRST kBeginF32 frame
// writes <path>.in (a 16-byte header - the fp32 image's byte count and block count - then the rows
// as uploaded) and <path>.q8 (the d_q8 bytes the micro-kernel produced) after the frame's sync.
// The host test then byte-compares <path>.q8 against quantize_q8_1_cpu of <path>.in - the
// bit-identity pin, executable once a GPU run exists (the build window).  Observability only,
// default absent: no file is touched on the default path.
void dump_f32_quant_golden(const uint8_t* img_in_host, const void* q8_dev, int64_t img_bytes,
                           int64_t n_blocks, int64_t n_q8_bytes) {
    static bool dumped = false;   // first frame only; serve threads share the process
    if (dumped) return;
    dumped = true;
    const char* p = std::getenv("STRATA_HELPER_F32_QUANT_DUMP");
    if (p == nullptr || *p == '\0') return;
    const std::string in_path = std::string(p) + ".in";
    const std::string q8_path = std::string(p) + ".q8";
    const int64_t header[2] = {img_bytes, n_blocks};
    if (std::FILE* f = std::fopen(in_path.c_str(), "wb")) {
        std::fwrite(header, sizeof(header), 1, f);
        std::fwrite(img_in_host, 1, (size_t) img_bytes, f);
        std::fclose(f);
    }
    std::vector<uint8_t> q8_host((size_t) n_q8_bytes, 0);
    if (cudaMemcpy(q8_host.data(), q8_dev, (size_t) n_q8_bytes, cudaMemcpyDeviceToHost) == cudaSuccess) {
        if (std::FILE* f = std::fopen(q8_path.c_str(), "wb")) {
            std::fwrite(q8_host.data(), 1, (size_t) n_q8_bytes, f);
            std::fclose(f);
        }
    }
    std::fprintf(stderr, "expert-helper: f32 window quant golden dump written (%s.in/%s.q8)\n", p, p);
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
// One signal = one fact: the --persist session loop exits on this count, never on the g_stop latch,
// which main itself sets while ending a session (the latch is shared, the signal count is not).
std::atomic<int> g_sig_count{0};
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
    g_sig_count.fetch_add(1);
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
    // staging buffers at the same time.  (helper-window-diet: the WINDOW tier releases the lock
    // before its kRows reply write - the frame's stream work ended at its sync and the reply
    // reads only channel-local bytes, so a stalled peer's EAGAIN wait must not freeze the other
    // channel; the other served frames keep the full-span rule below.)
    Socket listener2;
    Socket sock2;
    std::mutex gpu_mtx;
    // L2 (opt-in STRATA_HELPER_STREAMS, default OFF = exactly the single-mutex behaviour above):
    // ON turns kFill into the gate's only WRITER and every served frame into a reader, so
    // channel A's window/draft frames and channel B's prompt frames overlap on their OWN
    // streams (the mirror reads are read-only after fill); each frame still excludes fill, and
    // fill fences both streams because a frame releases the gate only after its sync + reply
    // (the window tier's kRows write included: its stream already drained at the sync).
    std::shared_mutex gpu_gate;
    cudaStream_t stream_p = nullptr;      // ON: the prompt tier's own stream
    bool streams_split = false;           // set once in main; never mutated afterwards
    std::thread chan2;
    // W2 (the reply ring, default OFF): created once in main when STRATA_HELPER_REPLY_RING is set,
    // offered ONLY to hellos that carry kHelloWantReplyRing.  Slot k is channel k's OWN for the
    // process lifetime (0 = the primary socket, 1 = --socket2): the one-outstanding-per-channel
    // rule makes each slot's generations strictly sequential, so a slot is free exactly when the
    // next request on ITS channel arrives.  Thread ownership: ring_seq/ev[slot] belong to that
    // channel's serve thread (and serve_fill, which event-fences both - W4).
    int ring_fd = -1;                        // the memfd; every ack's SCM_RIGHTS is a fresh dup of it
    uint8_t* ring_map = nullptr;             // RW mapping of the memfd, registered pinned+mapped
    uint64_t ring_stride = 0;
    unsigned long long ring_seq[2] = {0, 0}; // the last generation PUBLISHED per slot (its thread only)
    cudaEvent_t ring_ev[2] = {};             // W4: the slot's frame-completion event (recorded at birth)
    // PER-SLOT, like everything the epilogue reads: a host memcpy into MAPPED memory is not a
    // stream op, so a single shared staging buffer would let channel B's parse overwrite the
    // claims channel A's queued-but-not-yet-executed epilogue still reads.  One buffer per slot,
    // written only by its channel's serve thread, closes the hole.
    int32_t* h_ring_claims[2] = {nullptr, nullptr};
    ExpertCache cache;
    MappedPack pack;
    // A v4 pack (the 2026-11-06 prod model): native_experts.txt's per-role shard column places a
    // blob's roles in DIFFERENT split files at SHARD-RELATIVE offsets, so a fill reads from up to
    // four maps: `pack` is the primary --experts file, every other shard is mapped ONCE here.
    std::string pack_path;   // the file `pack` maps (o.pack dir + o.experts_file)
    std::vector<std::pair<std::string, MappedPack>> shard_maps;
    // The map for a role's file: the primary pack, or a shard mapped on first use (the split is
    // four files; mmap stays lazy, only the touched blob pages fault in, and the fill is the only
    // caller - kFill rides ONE channel, so the map never grows concurrently).
    MappedPack* shard_map(const std::string& path, std::string& err) {
        if (path == pack_path) return &pack;
        for (auto& e : shard_maps)
            if (e.first == path) return &e.second;
        shard_maps.emplace_back(path, MappedPack{});
        if (!shard_maps.back().second.open(path, err)) {
            shard_maps.pop_back();
            return nullptr;
        }
        return &shard_maps.back().second;
    }
    // expert_gguf_file (src/core/expert_source.cpp) mirrored for the sidecar: the role's shard
    // name (an empty column = the primary file), relative to the --experts file's directory.
    std::string expert_role_path(const std::vector<std::string>& gguf_file, size_t i) const {
        if (gguf_file.size() <= i || gguf_file[i].empty()) return pack_path;
        const size_t cut = pack_path.find_last_of("/\\");
        return (cut == std::string::npos ? std::string() : pack_path.substr(0, cut + 1)) + gguf_file[i];
    }
    GroupMeta* h_meta = nullptr;   // pinned, mapped: one upload per window
    GroupMeta* d_meta = nullptr;
    uint8_t* d_q8 = nullptr;       // the window's activations (n_tok * (H/32) * 36)
    float* d_out = nullptr;        // the compact rows (CAP * H)
    void* d_scratch = nullptr;
    // W1 (kBeginF32, lazy - allocated on the first frame that needs it, so the default path's
    // buffers_bytes and footprint stay byte-identical): the fp32 window image's staging.  h_img is
    // the PINNED host slot every window image upload can copy through (the f32 rows, or the q8/q8k
    // blocks under STRATA_HELPER_PINNED_RX); d_img_f32 is the device landing of the f32 rows,
    // read by the quantize micro-kernel that writes d_q8.  kMaxTok * H * 4 = 80 KiB covers the
    // largest window image of all three wires (f32 80 KiB > q8k 23.4 KiB > q8 23 KiB).
    uint8_t* h_img = nullptr;      // pinned host, kMaxTok * H * 4
    float* d_img_f32 = nullptr;    // device, kMaxTok * H * 4 (the f32 rows before quantize)
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
        if (h_img) cudaFreeHost(h_img);       // W1: the lazy window-image staging
        if (d_img_f32) cudaFree(d_img_f32);
        if (h_meta_p) cudaFreeHost(h_meta_p);
        if (d_meta_p) cudaFree(d_meta_p);
        if (d_q8_p) cudaFree(d_q8_p);
        if (d_out_p) cudaFree(d_out_p);
        if (d_scratch_p) cudaFree(d_scratch_p);
        if (stream) cudaStreamDestroy(stream);
        if (stream_p) cudaStreamDestroy(stream_p);
        // W2: the ring's teardown mirrors its creation - unregister before unmapping, the events
        // last (a recorded event must not outlive its destruction with a waiter, and none exists
        // here: both channels are joined before the Helper dies).
        for (int s = 0; s < 2; ++s) {
            if (h_ring_claims[s]) cudaFreeHost(h_ring_claims[s]);
            if (ring_ev[s]) cudaEventDestroy(ring_ev[s]);
            h_ring_claims[s] = nullptr;
        }
        if (ring_map) {
            cudaHostUnregister(ring_map);
            ::munmap(ring_map, (size_t) strata::core::expert_helper::ring_bytes(H));
        }
        if (ring_fd >= 0) ::close(ring_fd);
        ring_map = nullptr;
        ring_fd = -1;
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

// ---- W2 (helper-quant-sync-diet.md §W2): the reply ring's helpers and the epilogue kernel. ----

/// The W2 epilogue: pack the grouped kernel's compact rows into one ring slot's records area and
/// publish the generation seal.  The records are BYTE-FOR-BYTE a kRows payload's body (RowsResp
/// then `[claim_idx | H floats]` per row), written straight into the memfd's pinned pages the
/// engine maps - serve_begin's host pack loop and the reply's row bytes both disappear.  ONE
/// block (the seal's __syncthreads is block-scoped); grid-stride over the floats keeps the
/// writes coalesced.  The seal is written LAST: every thread fences its record writes system-wide
/// (mapped host memory), the block syncs, then thread 0 publishes - a doorbell-ordered reader
/// sees either the old generation or all of this one, never a mix.
__global__ void ring_pack_records(const float* __restrict__ rows, const int32_t* __restrict__ claims,
                                  int32_t n_rows, int32_t h,
                                  uint8_t* __restrict__ records, unsigned long long* __restrict__ seq_word,
                                  unsigned long long seq) {
    const int64_t row_bytes = 4 + (int64_t) h * 4;
    const int64_t total = (int64_t) n_rows * h;
    for (int64_t j = (int64_t) blockIdx.x * blockDim.x + threadIdx.x; j < total;
         j += (int64_t) gridDim.x * blockDim.x) {
        const int32_t i = (int32_t) (j / h);
        const int32_t c = (int32_t) (j % h);
        float* dst = (float*) (void*) (records + 8 + (int64_t) i * row_bytes + 4);
        dst[c] = rows[(size_t) i * h + c];
    }
    if (threadIdx.x == 0 && blockIdx.x == 0) {
        RowsResp* rep = (RowsResp*) (void*) records;
        rep->n_rows = n_rows;
        rep->reserved0 = 0;
    }
    __threadfence_system();
    __syncthreads();
    if (threadIdx.x == 0 && blockIdx.x == 0) *(volatile unsigned long long*) seq_word = seq;
}

/// This channel's slot base in the ring (the seq word lives at [0], the records at [8..)).
uint8_t* ring_slot_of(const Helper& hp, int slot) {
    return expert_helper::ring_slot(hp.ring_map, (uint32_t) slot, hp.ring_stride);
}

/// Publish a generation whose records are ALREADY in the slot (host-written: the empty reply, the
/// prompt tier's core-packed payload) and answer with the doorbell.  Release-fence then seal -
/// the doorbell's socket write is the acquire side the engine's read rides on.
int ring_reply_sealed(Helper& hp, int slot, int32_t n_rows, uint32_t req_id, uint32_t seq, Socket& out) {
    RowsRingResp rr;
    rr.n_rows = n_rows;
    rr.slot = (int32_t) slot;
    rr.seq = ++hp.ring_seq[slot];
    std::atomic_thread_fence(std::memory_order_release);
    *(volatile unsigned long long*) (void*) ring_slot_of(hp, slot) = rr.seq;
    return out.write_reply(kRowsRing, req_id, seq, (const uint8_t*) &rr, sizeof(rr),
                           deadline_after(reply_deadline_ms(1000)));
}

/// W4's event wait: a short spin on cudaEventQuery (decode frames complete in tens of
/// microseconds), then the sync API's own wait.  Unlike the old cudaStreamSynchronize this drains
/// ONLY this frame - a later frame's work enqueued on the same stream is not waited on.
bool ring_event_wait(const Helper& hp, int slot, std::string& err) {
    cudaError_t q = cudaErrorNotReady;
    for (int spin = 0; spin < 512; ++spin) {
        q = cudaEventQuery(hp.ring_ev[slot]);
        if (q == cudaSuccess || q != cudaErrorNotReady) break;
    }
    if (q != cudaSuccess) {
        cudaGetLastError();   // clear a sticky query error; the sync below reports the real one
        return cuda_check(cudaEventSynchronize(hp.ring_ev[slot]), "ring event wait", err);
    }
    return true;
}

/// Create the ring (main, before any accept): memfd + RW mapping + header + pinned/mapped
/// registration + the per-slot events.  A failure here only disables the lane - the helper keeps
/// its socket replies, and no hello is ever offered the bit.
bool ring_create(Helper& hp, std::string& err) {
    const uint64_t bytes = expert_helper::ring_bytes(H);
    hp.ring_fd = ::memfd_create("strata-expert-helper-ring", 0);
    if (hp.ring_fd < 0) { err = std::string("memfd_create: ") + std::strerror(errno); return false; }
    if (::ftruncate(hp.ring_fd, (off_t) bytes) != 0) {
        err = std::string("ftruncate: ") + std::strerror(errno);
        return false;
    }
    hp.ring_map = (uint8_t*) ::mmap(nullptr, (size_t) bytes, PROT_READ | PROT_WRITE, MAP_SHARED,
                                    hp.ring_fd, 0);
    if (hp.ring_map == MAP_FAILED) {
        hp.ring_map = nullptr;
        err = std::string("mmap: ") + std::strerror(errno);
        return false;
    }
    std::memset(hp.ring_map, 0, (size_t) bytes);   // fault + zero every page before pinning
    expert_helper::RingHeader* rh = (expert_helper::RingHeader*) (void*) hp.ring_map;
    rh->magic = kRingMagic;
    rh->version = kRingVersion;
    rh->slot_count = kRingSlotCount;
    rh->n_embd = (uint32_t) H;
    rh->max_records = kRingMaxRecords;
    rh->slot_stride = expert_helper::ring_slot_stride(H);
    hp.ring_stride = rh->slot_stride;
    if (!cuda_check(cudaHostRegister(hp.ring_map, (size_t) bytes,
                                     cudaHostRegisterPortable | cudaHostRegisterMapped),
                    "ring pin+map", err))
        return false;
    for (int s = 0; s < (int) kRingSlotCount; ++s)
        if (!cuda_check(cudaHostAlloc((void**) &hp.h_ring_claims[s], kRingMaxRecords * sizeof(int32_t),
                                      cudaHostAllocPortable | cudaHostAllocMapped),
                        "ring claims staging", err))
            return false;
    for (int s = 0; s < (int) kRingSlotCount; ++s) {
        if (!cuda_check(cudaEventCreateWithFlags(&hp.ring_ev[s], cudaEventDisableTiming),
                        "ring event", err))
            return false;
        // recorded at birth: an always-recorded event is always legal to query/synchronize, so
        // serve_fill's fence and a first frame's wait need no "was it used yet" state
        if (!cuda_check(cudaEventRecord(hp.ring_ev[s], 0), "ring event birth", err)) return false;
    }
    std::fprintf(stderr, "expert-helper: reply ring ready (%u slots x %llu B, %u records/slot; "
                         "offered to hellos that ask)\n",
                 (unsigned) kRingSlotCount, (unsigned long long) hp.ring_stride,
                 (unsigned) kRingMaxRecords);
    return true;
}

// ---- kFill: fill the cache within the card's budget, in the engine's rank order ----
int serve_fill(Helper& hp, PayloadView body, uint32_t req_id, uint32_t seq, int64_t reserve_mib,
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
    // W4 (the doc's fill-fencing rule): on the ring lane a served frame releases the device lock
    // AT LAUNCH, so winning the lock no longer proves the last frame's kernel finished - fence
    // BOTH slots' events, not just their replies.  Cheap when idle (a completed event returns at
    // once; the events are recorded at birth, so a channel that never served is covered too).
    // Without the ring (the default) the lock still spans completion and this is skipped whole.
    if (hp.ring_fd >= 0)
        for (int s = 0; s < (int) kRingSlotCount; ++s)
            if (!cuda_check(cudaEventSynchronize(hp.ring_ev[s]), "fill ring fence", err))
                return kErrInternal;
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
    // H14 (r24) + the v4 multi-shard pack (2026-11-06): a GGUF-mapped pack keeps no packed blob
    // region - the manifest's `offset` column describes an experts.bin layout this pack does not
    // ship, so blob_offset() lands on unrelated GGUF bytes (r23 proof: garbage scales there,
    // 191/2560 NaN rows; the engine offsets decode clean, 0 NaN). Assemble each slot per role
    // exactly like the engine's FileExpertSource::copy_from_files: role r reads
    // gguf_off[3*layer + r] + expert*role_bytes[r] INSIDE THE FILE the manifest names for that
    // role. A v4 manifest carries a per-role shard column and the offsets are SHARD-RELATIVE -
    // the primary --experts mapping (a split's first shard is metadata-only) cannot cover a blob,
    // which is where the 2026-11-06 prod fill died (kErrInternal on the iq3xxs-ud pack). A v3
    // pack names no shards, every role resolves to the primary file, and the reads are the r24
    // single-mapping reads byte for byte.
    const bool gguf_pack = lay.gguf_off.size() == 3 * (size_t) hp.n_layers;
    std::vector<uint8_t> stage, warm_src;
    for (size_t i = 0; i < pairs.size(); ++i) {
        const int32_t slot = hp.cache.admit(pairs[i].first, pairs[i].second);
        const uint64_t bytes = lay.blob_bytes(pairs[i].first);
        const uint8_t* src = nullptr;
        if (gguf_pack) {
            const auto& pf = lay.fmt[(size_t) pairs[i].first];
            const auto pl = strata::kernels::native_expert_layout(pf.gu_type, pf.d_type, pf.n_embd, pf.n_ff);
            const uint64_t per[3] = { (uint64_t) pf.n_ff * pl.gu_row, (uint64_t) pf.n_ff * pl.gu_row,
                                      (uint64_t) pf.n_embd * pl.d_row };
            stage.resize((size_t) bytes);
            uint64_t at = 0;
            for (int r = 0; r < 3; ++r) {
                const size_t gi = 3 * (size_t) pairs[i].first + (size_t) r;
                const std::string role_file = hp.expert_role_path(lay.gguf_file, gi);
                std::string map_err;
                MappedPack* mp = hp.shard_map(role_file, map_err);
                if (mp == nullptr) { err = map_err; return kErrInternal; }
                const uint64_t roff = lay.gguf_off[gi] + (uint64_t) pairs[i].second * per[r];
                if (roff + per[r] > mp->size) {
                    err = "a role region is outside the mapping of " + role_file;
                    return kErrInternal;
                }
                std::memcpy(stage.data() + at, mp->addr + roff, (size_t) per[r]);
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

// ---- kBegin / kBeginQ8K / kBeginF32: one grouped launch over the claimed entries; kRows answers
// with the original indices.  image_q8k: the frame's activation image is ggml's Q8_K blocks (the
// CPU reference's own gate/up image) and the launch is native_expert_grouped_q8k.  image_f32 (W1):
// the image is the window's RAW fp32 rows - staged into hp.h_img (pinned), uploaded to hp.d_img_f32
// and quantized on the stream by quantize_q8_1_f32_window_kernel into hp.d_q8, so the grouped
// launch below reads the same blocks kBegin carries and is byte-for-byte unchanged. ----

// W1/W3.2: the window image's staging, allocated LAZILY on the first frame that uses it (kBeginF32
// always; kBegin/kBeginQ8K only under STRATA_HELPER_PINNED_RX) - a default-off run never pays the
// 80 KiB pinned + 80 KiB device or reports it in buffers_bytes, so the fill budget's inputs stay
// byte-identical today.  `need_f32` also allocates the device landing of the fp32 rows.
bool ensure_window_img_staging(Helper& hp, bool need_f32, std::string& err) {
    const size_t bytes = (size_t) kMaxTok * (size_t) H * 4;   // the largest window image of the three wires
    if (hp.h_img == nullptr &&
        !cuda_check(cudaHostAlloc((void**) &hp.h_img, bytes, cudaHostAllocPortable),
                    "window image staging (pinned)", err)) return false;
    if (need_f32 && hp.d_img_f32 == nullptr &&
        !cuda_check(cudaMalloc((void**) &hp.d_img_f32, bytes), "f32 image (device)", err)) return false;
    return true;
}

int serve_begin(Helper& hp, PayloadView body, uint32_t req_id, uint32_t seq, std::string& err,
                Socket& out, bool image_q8k, bool image_f32, int ring_slot) {
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
    if (image_q8k && H % 256 != 0) return kErrGeometry;   // whole 256-value super-blocks
    if (image_f32 && H % 32 != 0) return kErrGeometry;    // whole 32-value q8_1 blocks for the quant kernel
    // the wire's pinned image sizes: q8_1 (H/32)*36, Q8_K (H/256)*292, fp32 H*4 (W1's kBeginF32)
    const int64_t img_row = image_f32 ? H * 4 : image_q8k ? (H / 256) * 292 : (H / 32) * 36;
    if (n_tok * img_row != req.n_q8) return kErrGeometry;
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
    if (image_q8k && !strata::kernels::native_expert_q8k_supported(fmt.gu_type, fmt.n_embd))
        return kErrGeometry;   // this layer's gate/up weight type has no Q8_K kernel

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
        if (ring_slot >= 0) {
            // W2: the negotiated lane answers empty too - a 0-row doorbell over a sealed, empty
            // records area (host-written: no kernel ran, there are no rows to pack).
            RowsResp* rep = (RowsResp*) (void*) (ring_slot_of(hp, ring_slot) + 8);
            rep->n_rows = 0;
            rep->reserved0 = 0;
            return ring_reply_sealed(hp, ring_slot, 0, req_id, seq, out);
        }
        RowsResp rep;
        rep.n_rows = 0;
        std::vector<uint8_t> payload(sizeof(rep), 0);
        std::memcpy(payload.data(), &rep, sizeof(rep));
        return out.write_reply(kRows, req_id, seq, payload.data(), payload.size(), deadline_after(reply_deadline_ms(1000)));
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

    // The activations reach d_q8 one of three ways, all producing the same bytes the grouped kernel
    // reads below:
    //  - kBegin/kBeginQ8K (default): the engine quantized the image - canonical q8_1 blocks (bit
    //    for bit its own quantize_q8_1_kernel's) or the CPU reference's own Q8_K image
    //    (native_quant_act) - uploaded verbatim, no requantization.  Under STRATA_HELPER_PINNED_RX
    //    (W3.2, default OFF) the copy runs from the pinned h_img staging slot instead of the
    //    channel's pageable RxBuffer: a direct DMA instead of a driver-staged bounce (the RxBuffer
    //    itself reallocs as frames grow, so IT cannot be registered - the staging slot is the
    //    stable pinned range; one ~23 KB host memcpy buys the DMA).
    //  - kBeginF32 (W1, the engine's STRATA_HELPER_F32_WINDOW + our kFeatureF32Window): the window's
    //    RAW fp32 rows, staged into the same pinned slot, uploaded to d_img_f32 and quantized on
    //    the stream by quantize_q8_1_f32_window_kernel - the CPU quantizer's own statements, one
    //    thread per block, bit-identical including the fp16 sum (see the kernel's proof) - writing
    //    d_q8 before the grouped launch reads it in the same stream.
    if (image_f32) {
        if (!ensure_window_img_staging(hp, true, err)) return kErrInternal;
        std::memcpy(hp.h_img, q8, (size_t) req.n_q8);
        if (!cuda_check(cudaMemcpyAsync(hp.d_img_f32, hp.h_img, (size_t) req.n_q8, cudaMemcpyHostToDevice,
                                        hp.stream), "f32 image upload", err)) return kErrInternal;
        const long long n_blocks = (long long) n_tok * (H / 32);
        quantize_q8_1_f32_window_kernel<<<(unsigned) ((n_blocks + 255) / 256), 256, 0, hp.stream>>>(
            hp.d_img_f32, hp.d_q8, n_blocks);
        if (!cuda_check(cudaGetLastError(), "f32 window quant launch", err)) return kErrInternal;
    } else if (pinned_rx()) {
        if (!ensure_window_img_staging(hp, false, err)) return kErrInternal;
        std::memcpy(hp.h_img, q8, (size_t) req.n_q8);
        if (!cuda_check(cudaMemcpyAsync(hp.d_q8, hp.h_img, (size_t) req.n_q8, cudaMemcpyHostToDevice,
                                        hp.stream), "q8 upload (pinned)", err)) return kErrInternal;
    } else if (!cuda_check(cudaMemcpyAsync(hp.d_q8, q8, (size_t) req.n_q8, cudaMemcpyHostToDevice, hp.stream),
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
    //
    // W3.1 (STRATA_HELPER_WIN_FAST, default OFF - the same knob as the engine's payload diet):
    // skip the clear.  The kernel does NOT accumulate - it STORES, on every row this frame's scan
    // and reply read, proven for every down-kernel variant native_expert_grouped/_q8k can launch:
    //  - native_down_kernel writes out[ent_dst[e]*L.n_embd + r] = s*sd, a plain store, for every
    //    group g < ng (grid-strided in y), every entry e in [grp_start[g], grp_start[g+1]) and every
    //    column r < L.n_embd (gridDim.x covers ceil(n_embd/8) x 8 rows/warp) - iq_kernels.cu:2207-2228;
    //  - native_down_multi_kernel: the same store shape per GRP_NC entries (:2279-2443);
    //  - native_down_ws_kernel via dws_rows: out[dst[c]*n_embd + r] = s[c]*sd, a store (:4489-4497);
    //  - expdec_down_kernel: out[ent_dst[e+c]*L.n_embd + row0 + r] = s[r][c], a store (:2800-2811);
    //  - s26_down_l_kernel (:3824/:3836) and s27_down_kernel (:4042/:4054): stores;
    //  - native_down_amd_kernel (:5706-5734) and native_down_lds_kernel (:6045-6046): stores.
    //  The only atomic on `out` in the whole TU is nib_hash_kernel's accumulator (:5306) - a
    //  different buffer, not this path.  serve_begin's entry map builds dst as the exact
    //  0..dst.size()-1 sequence with grp_start covering every entry, and the scan/pack below read
    //  only rows [0, compact_claim.size()) = [0, dst.size()) - the memset's exact range, fully
    //  overwritten in this stream BEFORE the cudaStreamSynchronize below.  The clear guards
    //  nothing; the diet branch's prompt-tier twin (serve_prompt_core's own comment) removed its
    //  clear on the same kernel-family proof.  Kept under the env for one release regardless.
    if (!win_fast() &&
        !cuda_check(cudaMemsetAsync(hp.d_out, 0, (size_t) dst.size() * (size_t) H * 4, hp.stream),
                    "output clear", err)) return kErrInternal;
    // H14/r22: the scratch too - 191/2560 NaN rows came from cudaMalloc junk the fused pass
    // never wrote; a stale scratch byte must never reach a reply row.
    if (!cuda_check(cudaMemsetAsync(hp.d_scratch, 0, (size_t) hp.scratch_bytes, hp.stream),
                    "scratch clear", err)) return kErrInternal;
    if (image_q8k)
        strata::kernels::native_expert_grouped_q8k(L, hp.d_meta->ptr, hp.d_meta->start, &hp.d_meta->count,
                                                   hp.d_meta->dst, hp.d_meta->tok, groups, (int64_t) dst.size(),
                                                   (const uint8_t*) hp.d_q8, hp.d_scratch, hp.d_out, hp.stream);
    else
        strata::kernels::native_expert_grouped(L, hp.d_meta->ptr, hp.d_meta->start, &hp.d_meta->count,
                                               hp.d_meta->dst, hp.d_meta->tok, groups, (int64_t) dst.size(),
                                               (const uint8_t*) hp.d_q8, hp.d_scratch, hp.d_out, hp.stream);
    if (!cuda_check(cudaGetLastError(), image_q8k ? "native_expert_grouped_q8k launch"
                                                  : "native_expert_grouped launch", err)) return kErrInternal;
    if (ring_slot >= 0) {
        // ==== W2 (the ring lane) + W4 (event completion, lock released at launch).  The epilogue
        // packs this frame's rows - with their claim indices - straight into this channel's slot
        // (stream-ordered after the grouped launch), then seals the generation; the reply is the
        // 16-byte doorbell and the rows never touch the socket.
        const int32_t n_rows = (int32_t) compact_claim.size();
        const size_t row_bytes = 4 + (size_t) H * 4;
        std::memcpy(hp.h_ring_claims[ring_slot], compact_claim.data(), (size_t) n_rows * sizeof(int32_t));
        uint8_t* slot = ring_slot_of(hp, ring_slot);
        const unsigned long long seal = ++hp.ring_seq[ring_slot];
        ring_pack_records<<<1, 256, 0, hp.stream>>>(hp.d_out, hp.h_ring_claims[ring_slot], n_rows,
                                                    (int32_t) H, slot + 8,
                                                    (unsigned long long*) (void*) slot, seal);
        if (!cuda_check(cudaGetLastError(), "ring pack launch", err)) return kErrInternal;
        if (!cuda_check(cudaEventRecord(hp.ring_ev[ring_slot], hp.stream), "ring event record", err))
            return kErrInternal;
        // W4's contract, spelled where it bites:
        //  - d_out lifetime: this frame's rows LEFT the shared d_out with the epilogue (they live
        //    in this channel's OWN slot now), so the next frame's kernel may overwrite d_out the
        //    moment this frame's stream work completes - the scan below reads the slot, never
        //    d_out.  d_q8/d_meta/d_scratch reuse is safe by stream order (one stream: FIFO; the
        //    split: per-tier staging), which is exactly why the mutex can go here.
        //  - the lock ends AT LAUNCH (both lock modes): the other channel may parse, upload and
        //    launch its own frame while this one still computes.
        //  - one-outstanding-per-channel is unchanged and load-bearing: it bounds live events and
        //    slot generations to one per channel, and it is what lets this thread wait ONLY its
        //    own event.  serve_fill fences both slots' events (its W4 note).
        if (gpu_lk_on.has_value()) gpu_lk_on.reset();
        else if (gpu_lk.owns_lock()) gpu_lk.unlock();
        if (!ring_event_wait(hp, ring_slot, err)) return kErrInternal;
        // H14 over the packed records (host reads of the pinned slot - no PCIe round trip); the
        // engine's validator still refuses what this logs, exactly as on the socket path.
        for (int32_t i = 0; i < n_rows; ++i) {
            const float* row = (const float*) (const void*) (slot + 8 + sizeof(RowsResp) +
                                                             (size_t) i * row_bytes + 4);
            if (all_finite(row, (size_t) H)) continue;
            std::fprintf(stderr, "expert-helper: window row %d of %d (claim %d, expert %d) is non-finite "
                                 "(first floats %g %g %g %g)\n", i, n_rows, compact_claim[i],
                         ids[compact_claim[i]], row[0], row[1], row[2], row[3]);
            break;
        }
        RowsRingResp rr;
        rr.n_rows = n_rows;
        rr.slot = (int32_t) ring_slot;
        rr.seq = seal;
        const auto p_ready = prof ? std::chrono::steady_clock::now()
                                  : std::chrono::steady_clock::time_point{};
        const int rc_w = out.write_reply(kRowsRing, req_id, seq, (const uint8_t*) &rr, sizeof(rr),
                                         deadline_after(reply_deadline_ms(10000)));
        if (prof)   // L3: the same line, rows_kb = the records the lane carried (the socket's 16 B doorbell is the delta)
            std::fprintf(stderr, "rpcprofile: serve tier=w layer=%d rows=%d queue_us=%lld lock_us=%lld "
                                 "reply_us=%lld rows_kb=%lld ring=1\n", (int) layer, n_rows,
                         (long long) p_queue_us,
                         (long long) (std::chrono::duration_cast<std::chrono::microseconds>(
                                          p_ready - p_entry).count() - p_queue_us),
                         (long long) std::chrono::duration_cast<std::chrono::microseconds>(
                                          std::chrono::steady_clock::now() - p_ready).count(),
                         (long long) ((sizeof(RowsResp) + (size_t) n_rows * row_bytes) / 1024));
        return rc_w;
    }
    if (!cuda_check(cudaStreamSynchronize(hp.stream), "window sync", err)) return kErrInternal;
    if (image_f32)   // W1: the golden-pin dump reads the pinned input, then pulls d_q8 back
        dump_f32_quant_golden(hp.h_img, hp.d_q8, (int64_t) req.n_q8, (long long) n_tok * (H / 32),
                              (long long) n_tok * (H / 32) * 36);

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
    // helper-window-diet: the frame's GPU work ended at the sync above and every shared byte it
    // produced is already copied into this reply payload - release the device lock BEFORE
    // the reply write.  write_reply re-polls against its deadline on a stalled peer (the Nov 6
    // reclaim wedge: 90 s of EAGAIN), and gpu_mtx held across that freezes the OTHER channel's
    // frames too; after the release the write blocks on channel-local bytes only.  Fill fencing
    // is unchanged: fill needs this frame's STREAM fenced, and the sync already drained it.
    if (gpu_lk_on) gpu_lk_on.reset();   // same shape as the acquisition, opposite direction
    else gpu_lk.unlock();
    const int rc_w = out.write_reply(kRows, req_id, seq, payload.data(), payload.size(), deadline_after(reply_deadline_ms(10000)));
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

// ---- L3 sub-spans of ONE prompt-tier claim's serve body (STRATA_RPC_PROFILE only) ----
// The names follow the code's own phases in serve_prompt_core, nothing is invented:
//   parse  - the BeginPReq header, the geometry caps and the layer/format gates;
//   lookup - the pair loop (the primary bitmap, cache.slot_of, the per-expert cap) and the grouped
//            entry map (start/dst/tok/compact_claim);
//   quant  - the fp32 -> q8_1 twin quantize (image_format 1 only; ~0 for formats 0 and 2);
//   upload - the three async enqueues: the q8 image H2D, the GroupMetaP H2D, the scratch clear;
//   launch - the grouped kernel launch enqueue (native_expert_grouped / _q8k) + cudaGetLastError;
//   ksync  - cudaStreamSynchronize: the device compute to completion (it drains the queued uploads,
//            the clear and the kernels - that is the GEMM time);
//   scan   - the H14 finite-row scan of the mapped-host output rows;
//   pack   - the reply serialize, i.e. the rows' PCIe read out of the mapped cudaHostAlloc d_out_p.
// Their SUM is lock_us (the existing lock span); reply_us stays the socket write span.
// `sp` is nullptr when profiling is OFF, so no clock is read on the default path.
struct ServeSpans {
    int64_t parse_us = 0, lookup_us = 0, quant_us = 0, upload_us = 0;
    int64_t launch_us = 0, ksync_us = 0, scan_us = 0, pack_us = 0;
};

inline int64_t span_us(const std::chrono::steady_clock::time_point& from) {
    return std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::steady_clock::now() - from).count();
}

inline std::chrono::steady_clock::time_point span_mark(const ServeSpans* sp) {
    return sp != nullptr ? std::chrono::steady_clock::now() : std::chrono::steady_clock::time_point{};
}

// The appended L3 fields of one serve line: the span decomposition of lock_us, formatted ONLY when
// profiling.  The existing prefix stays byte-identical and these fields are appended after it, so the
// ab9 collectors (which match the prefix and ignore what follows) keep parsing.  The caller formats
// them INTO its line and writes the line ONCE: with --socket2 two serve threads write profile lines to
// the same stderr, and a line split across writes could interleave.
inline void format_serve_spans(const ServeSpans& sp, char* buf, size_t n) {
    std::snprintf(buf, n, " parse_us=%lld lookup_us=%lld quant_us=%lld upload_us=%lld launch_us=%lld "
                          "ksync_us=%lld scan_us=%lld pack_us=%lld",
                  (long long) sp.parse_us, (long long) sp.lookup_us, (long long) sp.quant_us,
                  (long long) sp.upload_us, (long long) sp.launch_us, (long long) sp.ksync_us,
                  (long long) sp.scan_us, (long long) sp.pack_us);
}

// The batch's line reports ONE set of spans for the WHOLE batch, so the per-part spans sum here.
inline ServeSpans& operator+=(ServeSpans& a, const ServeSpans& b) {
    a.parse_us += b.parse_us;
    a.lookup_us += b.lookup_us;
    a.quant_us += b.quant_us;
    a.upload_us += b.upload_us;
    a.launch_us += b.launch_us;
    a.ksync_us += b.ksync_us;
    a.scan_us += b.scan_us;
    a.pack_us += b.pack_us;
    return a;
}

// The per-part BODY of a prompt-tier request: every cap kBeginP enforces, the claims, the grouped
// launch, the H14 finite scan, and the reply BYTES - the exact kRowsP payload (RowsPResp + rows in
// claim order with the original pair indices).  It takes no lock and writes no socket: serve_prompt
// locks and writes it, and serve_prompt_batch serves K parts under ONE lock acquisition and writes
// them as K parts of one kRowsPB.  One code path per claim, so a batched part is byte-for-byte the
// request and reply a standalone kBeginP/kRowsP pair carries.
static int serve_prompt_core(Helper& hp, PayloadView body, std::vector<uint8_t>& reply,
                             int32_t& n_rows_out, int32_t& layer_out, std::string& err,
                             ServeSpans* sp = nullptr) {
    const auto t_parse = span_mark(sp);   // L3: spans are only marked when sp is non-null (prof ON)
    if (body.size() < sizeof(BeginPReq)) return kErrShort;
    BeginPReq req;
    std::memcpy(&req, body.data(), sizeof(req));
    const int64_t n_sel = req.n_sel_tok, n_pairs = req.n_pairs;
    if (n_sel < 1 || n_sel > kPromptTok || req.k != (int32_t) kK || n_pairs < 1 ||
        n_pairs > kPromptRows || req.image_format < 0 || req.image_format > 2) return kErrGeometry;
    const size_t res_bytes = (size_t) ((n_sel * hp.n_expert + 7) / 8);
    // M4c (b): image_format 1 = fp32 rows (n_sel * H * 4); 0 = the canonical q8_1 blocks; 2 = the
    // Q8_K blocks (n_sel * (H/256) * 292), the CPU reference's own gate/up image (kFeatureQ8K).
    const int64_t img_expect = req.image_format == 1 ? n_sel * H * (int64_t) sizeof(float)
                             : req.image_format == 2 ? n_sel * (H / 256) * 292
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
    if (req.image_format == 2) {
        if (fmt.gu_act != (int) GGML_TYPE_Q8_K)
            return kErrGeometry;   // this layer's own gate/up format is not Q8_K: not this wire's layer
        if (!strata::kernels::native_expert_q8k_supported(fmt.gu_type, fmt.n_embd))
            return kErrGeometry;   // this layer's gate/up weight type has no Q8_K kernel
    }
    if (sp != nullptr) sp->parse_us = span_us(t_parse);
    const auto t_lookup = span_mark(sp);

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
        reply.assign(sizeof(rep), 0);
        std::memcpy(reply.data(), &rep, sizeof(rep));
        n_rows_out = 0;
        layer_out = layer;
        return kOk;
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
    if (sp != nullptr) sp->lookup_us = span_us(t_lookup);   // the pair resolution AND the entry map
    const auto t_quant = span_mark(sp);

    // The activations: image_format 0 arrives exactly as the engine quantized them (quantize_q8_1_cpu,
    // the same quantizer the decode contract pins) - upload verbatim, no requantization.  image_format
    // 1 (M4c (b)) arrives as fp32 rows: quantize HERE with the pinned twin - the same bytes for the
    // same floats - then the same upload.  The engine's critical path skipped this work entirely.
    // image_format 2 arrives as Q8_K blocks - the CPU reference's own gate/up image
    // (native_quant_act engine-side) - uploaded verbatim like format 0, at its own row size.
    const bool img_q8k = req.image_format == 2;
    const uint8_t* img = q8;
    const size_t up_bytes = (size_t) n_sel *
        (img_q8k ? (size_t) (H / 256) * 292 : (size_t) (H / 32) * 36);
    if (req.image_format == 1) {
        if (hp.h_q8_p_scratch.size() < up_bytes) hp.h_q8_p_scratch.resize(up_bytes);
        const float* fx = (const float*) q8;
        for (int64_t r = 0; r < n_sel; ++r)
            quantize_q8_1_twin(fx + (size_t) r * (size_t) H, H,
                               hp.h_q8_p_scratch.data() + (size_t) r * (size_t) (H / 32) * 36);
        img = hp.h_q8_p_scratch.data();
    }
    if (sp != nullptr) sp->quant_us = span_us(t_quant);   // 0 work for image_format 0/2 (uploaded verbatim)
    const auto t_upload = span_mark(sp);
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
    // H14's output clear is GONE here (helper-ipc-diet, 2026-11), and the scratch clear is narrowed
    // to the bytes this claim's kernels can dirty - a 24 MB pinned-memory PCIe memset per claim was
    // the single biggest GPU-side cost in the ab9 window.  The coverage facts, from the kernels:
    //  - out: native_down_kernel's grid covers EVERY column r < n_embd and, for every group g <
    //    n_groups, every entry e in [grp_start[g], grp_start[g+1]) writes out[ent_dst[e]*n_embd + r]
    //    (iq_kernels.cu:2207-2228).  ent_dst is the 0..dst.size()-1 sequence built above (:822-831),
    //    grp_start covers every entry, and the q8k path launches the same down kernel - so the
    //    memset's exact range [0, dst.size()*H*4) is fully overwritten in this stream BEFORE the
    //    cudaStreamSynchronize below, and the finite scan and the reply read ONLY that range.  A
    //    stale window/warmup/cudaMalloc value cannot reach a reply row: the clear guards nothing.
    //  - scratch: the gu/SwiGLU/down chain derives gate/up/h/hq from cap_entries = dst.size()
    //    (native_expert_scratch_bytes' own formula; the q8k kernel states the same layout) and
    //    writes every byte of that prefix before reading it.  The memset follows the claim's cap
    //    (max of the two layout formulas, the same max that sizes the buffer at :1477-1479) so it
    //    covers every byte the kernels touch this claim and leaves the never-read tail alone.
    //    The output rows keep NO clear: verified fully overwritten above - correctness over
    //    cleverness was applied to the scratch, where the write-before-read proof is layout-wide.
    const size_t scratch_dirty = (size_t) std::max(
        strata::kernels::native_expert_scratch_bytes((int64_t) dst.size(), L.n_ff),
        strata::kernels::moe_hit_grouped_scratch_bytes((int64_t) dst.size(), H, FF));
    if (!cuda_check(cudaMemsetAsync(hp.d_scratch_p, 0, scratch_dirty, prompt_stream(hp)),
                    "prompt scratch clear", err)) return kErrInternal;
    if (sp != nullptr) sp->upload_us = span_us(t_upload);   // the THREE async enqueues, not their drain
    const auto t_launch = span_mark(sp);
    if (img_q8k)
        strata::kernels::native_expert_grouped_q8k(L, hp.d_meta_p->ptr, hp.d_meta_p->start, &hp.d_meta_p->count,
                                                   hp.d_meta_p->dst, hp.d_meta_p->tok, groups, (int64_t) dst.size(),
                                                   (const uint8_t*) hp.d_q8_p, hp.d_scratch_p, hp.d_out_p, prompt_stream(hp));
    else
        strata::kernels::native_expert_grouped(L, hp.d_meta_p->ptr, hp.d_meta_p->start, &hp.d_meta_p->count,
                                               hp.d_meta_p->dst, hp.d_meta_p->tok, groups, (int64_t) dst.size(),
                                               (const uint8_t*) hp.d_q8_p, hp.d_scratch_p, hp.d_out_p, prompt_stream(hp));
    if (!cuda_check(cudaGetLastError(), img_q8k ? "native_expert_grouped_q8k prompt launch"
                                                : "native_expert_grouped prompt launch", err)) return kErrInternal;
    if (sp != nullptr) sp->launch_us = span_us(t_launch);   // the kernel enqueue + cudaGetLastError
    const auto t_ksync = span_mark(sp);
    if (!cuda_check(cudaStreamSynchronize(prompt_stream(hp)), "prompt row sync", err)) return kErrInternal;
    if (sp != nullptr) sp->ksync_us = span_us(t_ksync);   // THE GEMM SPAN: queued uploads, clear, kernels
    const auto t_scan = span_mark(sp);

    // H14's finite-row scan, mirrored: prove the rows before the engine refuses them.
    for (int32_t i = 0; i < (int32_t) compact_claim.size(); ++i) {
        const float* row = hp.d_out_p + (size_t) i * H;
        // helper-ipc-diet: all_finite is the scalar rule's bit-exact twin (subnormals pass, only
        // NaN/inf flag), AVX2-width where the CPU has it; the first bad row is the same row the
        // scalar loop stopped on, so the log line and the refusal trigger byte-for-byte unchanged.
        const bool bad = !all_finite(row, (size_t) H);
        if (bad) {
            std::fprintf(stderr, "expert-helper: prompt row %d of %d (pair %d, expert %d) is non-finite "
                                 "(first floats %g %g %g %g)\n", i, (int32_t) compact_claim.size(),
                         compact_claim[i], pairs[2 * compact_claim[i] + 1], row[0], row[1], row[2], row[3]);
            break;
        }
    }

    if (sp != nullptr) sp->scan_us = span_us(t_scan);   // the H14 all_finite pass over the mapped rows

    // The rows in claim order, with the ORIGINAL pair indices (the engine validates every index
    // against its own claims and refuses a row it did not claim).
    const auto t_pack = span_mark(sp);
    const int32_t n_rows = (int32_t) compact_claim.size();
    const size_t row_bytes = 4 + (size_t) H * 4;
    RowsPResp rep;
    rep.n_rows = n_rows;
    reply.assign(sizeof(rep) + (size_t) n_rows * row_bytes, 0);
    std::memcpy(reply.data(), &rep, sizeof(rep));
    for (int32_t i = 0; i < n_rows; ++i) {
        std::memcpy(reply.data() + sizeof(rep) + (size_t) i * row_bytes, &compact_claim[i], 4);
        std::memcpy(reply.data() + sizeof(rep) + (size_t) i * row_bytes + 4,
                    hp.d_out_p + (size_t) i * H, (size_t) H * 4);
    }
    if (sp != nullptr) sp->pack_us = span_us(t_pack);   // reply serialize + the rows' PCIe read from d_out_p
    n_rows_out = n_rows;
    layer_out = layer;
    return kOk;
}

int serve_prompt(Helper& hp, PayloadView body, uint32_t req_id, uint32_t seq, std::string& err,
                 Socket& out, int ring_slot) {
    // The prompt tier's OWN staging (h_meta_p/d_meta_p/d_q8_p/d_out_p/scratch_p) and the grouped
    // kernel it launches are shared device state: with --socket2 this frame and a window frame can
    // arrive at the same moment, on different channels.  Same lock as serve_begin, never across a
    // channel's idle read - and the SAME lock the batch below takes ONCE for its K parts.
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
    std::vector<uint8_t> payload;
    int32_t n_rows = 0, layer = 0;
    ServeSpans spans;   // L3: the core gets a span sink ONLY when profiling; off = no clocks read
    const int rc = serve_prompt_core(hp, body, payload, n_rows, layer, err, prof ? &spans : nullptr);
    if (rc != kOk) return rc;
    const auto p_ready = prof ? std::chrono::steady_clock::now()
                              : std::chrono::steady_clock::time_point{};
    // W2: the negotiated lane carries the core's packed reply bytes into this channel's slot (one
    // small mapped memcpy - the B-half claims are decode-sized) and answers with the doorbell,
    // taking the 72-90 KB of rows OFF the socket.  A claim bigger than a slot's record capacity
    // keeps today's kRowsP for that frame - a per-frame fallback, no renegotiation.  The prompt
    // tier keeps its WHOLE-FRAME lock either way (W4's early release is serve_begin's, where the
    // epilogue removes d_out from the critical path; this tier's pack stays host-side).
    const bool via_ring = ring_slot >= 0 && n_rows <= (int32_t) kRingMaxRecords;
    int rc_w;
    if (via_ring) {
        uint8_t* slot = ring_slot_of(hp, ring_slot);
        std::memcpy(slot + 8, payload.data(), payload.size());
        RowsRingResp rr;
        rr.n_rows = n_rows;
        rr.slot = (int32_t) ring_slot;
        rr.seq = ++hp.ring_seq[ring_slot];
        std::atomic_thread_fence(std::memory_order_release);
        *(volatile unsigned long long*) (void*) slot = rr.seq;
        rc_w = out.write_reply(kRowsRing, req_id, seq, (const uint8_t*) &rr, sizeof(rr),
                               deadline_after(reply_deadline_ms(10000)));
    } else {
        rc_w = out.write_reply(kRowsP, req_id, seq, payload.data(), payload.size(), deadline_after(reply_deadline_ms(10000)));
    }
    if (prof) {   // L3: one line per served prompt frame; refusals/empty replies stay untimed
        char spans_txt[320];
        format_serve_spans(spans, spans_txt, sizeof(spans_txt));   // appended, same write as the line
        std::fprintf(stderr, "rpcprofile: serve tier=p layer=%d rows=%d queue_us=%lld lock_us=%lld "
                             "reply_us=%lld rows_kb=%lld%s%s\n", (int) layer, n_rows,
                     (long long) p_queue_us,
                     (long long) (std::chrono::duration_cast<std::chrono::microseconds>(
                                      p_ready - p_entry).count() - p_queue_us),
                     (long long) std::chrono::duration_cast<std::chrono::microseconds>(
                                      std::chrono::steady_clock::now() - p_ready).count(),
                     (long long) ((payload.size() - sizeof(RowsPResp)) / 1024),
                     via_ring ? " ring=1" : "", spans_txt);
    }
    return rc_w;
}

// ---- The CLAIM BATCH (kBeginPB / kRowsPB, default OFF - the engine's STRATA_CLAIM_BATCH): K
// consecutive prompt-tier claims in ONE exchange, served by the helper under ONE lock acquisition.
//
// Transport-only by construction: the frame's parts are sliced by kBeginP's own byte contract
// (BeginPReq + pairs + the layer's primary bitmap + the image, length-checked per part against the
// frame's exact total), each part runs the SAME serve_prompt_core a standalone kBeginP runs - same
// claims, same caps, same launch, same reply layout - and the parts are answered in the request's
// part order.  Nothing reorders rows: the engine validates each part against ITS OWN claims, so a
// row belonging to another part is kErrBadId there and the caller's same-layer fallback owns the set.
//
// The lock is the frame's point: K claims pay the device lock and the queue handshake ONCE
// (measured on the 8k-token window: 96 prompt claims, a per-claim fixed floor of ~5.8 ms).
int serve_prompt_batch(Helper& hp, PayloadView body, uint32_t req_id, uint32_t seq, std::string& err,
                       Socket& out) {
    static const bool prof = std::getenv("STRATA_RPC_PROFILE") != nullptr;
    const auto p_entry = prof ? std::chrono::steady_clock::now()
                              : std::chrono::steady_clock::time_point{};
    std::unique_lock<std::mutex> gpu_lk(hp.gpu_mtx, std::defer_lock);
    std::optional<std::shared_lock<std::shared_mutex>> gpu_lk_on;
    if (hp.streams_split) gpu_lk_on.emplace(hp.gpu_gate);   // L2: a served frame is a reader
    else gpu_lk.lock();
    const int64_t p_queue_us = prof ? std::chrono::duration_cast<std::chrono::microseconds>(
                                          std::chrono::steady_clock::now() - p_entry).count() : 0;
    ServeSpans spans;   // L3: the batch's ONE span set - the slice pass plus every part's spans
    const ServeSpans* sp = prof ? &spans : nullptr;
    const auto t_slice = span_mark(sp);
    if (body.size() < sizeof(BeginPBReq)) return kErrShort;
    BeginPBReq req;
    std::memcpy(&req, body.data(), sizeof(req));
    if (req.n_parts < 1 || req.n_parts > (int32_t) kMaxClaimBatch || req.flags != 0 ||
        req.reserved0 != 0) return kErrGeometry;
    // Slice the parts and length-check EVERY part before any device work: the frame's byte total is
    // exact, so a truncated or padded part is refused as kErrShort with nothing computed.
    std::vector<PayloadView> parts;
    size_t off = sizeof(req);
    for (int32_t i = 0; i < req.n_parts; ++i) {
        if (off + sizeof(BeginPReq) > body.size()) return kErrShort;
        BeginPReq part;
        std::memcpy(&part, body.data() + off, sizeof(part));
        if (part.n_sel_tok < 1 || part.n_sel_tok > (int32_t) kPromptTok || part.n_pairs < 1 ||
            part.n_pairs > (int32_t) kPromptRows) return kErrGeometry;
        const int64_t img = part.image_format == 1 ? (int64_t) part.n_sel_tok * H * (int64_t) sizeof(float)
                          : part.image_format == 2 ? (int64_t) part.n_sel_tok * (H / 256) * 292
                                                   : (int64_t) part.n_sel_tok * (H / 32) * 36;
        const size_t res_bytes = (size_t) (((int64_t) part.n_sel_tok * hp.n_expert + 7) / 8);
        const size_t len = sizeof(part) + (size_t) part.n_pairs * 8 + res_bytes + (size_t) img;
        if (img <= 0 || off + len > body.size()) return kErrShort;
        parts.push_back(PayloadView(body.data() + off, len));
        off += len;
    }
    if (off != body.size()) return kErrShort;   // the parts' byte total is the frame's, exactly
    if (sp != nullptr) spans.parse_us += span_us(t_slice);   // the batch's header check + slice pass
    // ONE reply for the batch: RowsPBResp + the parts' kRowsP payloads, in the request's part order.
    std::vector<uint8_t> reply;
    int64_t rows_total = 0;
    int32_t layer0 = 0;
    for (size_t i = 0; i < parts.size(); ++i) {
        std::vector<uint8_t> part_reply;
        int32_t n_rows = 0, layer = 0;
        ServeSpans part_spans;   // L3: summed into the batch's spans; nullptr when profiling is off
        const int rc = serve_prompt_core(hp, parts[i], part_reply, n_rows, layer, err,
                                         sp != nullptr ? &part_spans : nullptr);
        if (rc != kOk) return rc;
        if (sp != nullptr) spans += part_spans;
        if (i == 0) layer0 = layer;
        rows_total += n_rows;
        reply.insert(reply.end(), part_reply.begin(), part_reply.end());
    }
    RowsPBResp rep;
    rep.n_parts = (int32_t) parts.size();
    std::vector<uint8_t> payload(sizeof(rep) + reply.size(), 0);
    std::memcpy(payload.data(), &rep, sizeof(rep));
    std::memcpy(payload.data() + sizeof(rep), reply.data(), reply.size());
    const auto p_ready = prof ? std::chrono::steady_clock::now()
                              : std::chrono::steady_clock::time_point{};
    const int rc_w = out.write_reply(kRowsPB, req_id, seq, payload.data(), payload.size(),
                                     deadline_after(reply_deadline_ms(10000)));
    if (prof) {
        // L3: the line's PREFIX is the single-claim line's exactly, so the existing parsers keep
        // reading it; `parts` is the appended field naming the batch this line covers, and the
        // sub-spans are the WHOLE batch's (slice pass + every part), summed - one write per line.
        char spans_txt[320];
        format_serve_spans(spans, spans_txt, sizeof(spans_txt));
        std::fprintf(stderr, "rpcprofile: serve tier=p layer=%d rows=%lld queue_us=%lld lock_us=%lld "
                             "reply_us=%lld rows_kb=%lld parts=%d%s\n", (int) layer0, (long long) rows_total,
                      (long long) p_queue_us,
                      (long long) (std::chrono::duration_cast<std::chrono::microseconds>(
                                       p_ready - p_entry).count() - p_queue_us),
                      (long long) std::chrono::duration_cast<std::chrono::microseconds>(
                                       std::chrono::steady_clock::now() - p_ready).count(),
                      (long long) ((payload.size() - sizeof(rep)) / 1024), (int) parts.size(), spans_txt);
    }
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
void stage_draft_rows(Helper& hp, const uint8_t* wire, int64_t rows, bool bf16) {
    const int64_t N = hp.tap_hidden, P = hp.tap_planes;
    const float* src = (const float*) wire;
    const uint16_t* src16 = (const uint16_t*) wire;
    for (int64_t r = 0; r < rows; ++r)
        for (int64_t p = 0; p < P; ++p) {
            float* dst = hp.h_taps + p * rows * N + r * N;
            const int64_t off = (r * P + p) * N;
            if (!bf16)
                std::memcpy(dst, src + off, (size_t) N * 4);
            else
                for (int64_t n = 0; n < N; ++n) {   // L8: expand bf16 -> f32 (the low 16 bits are zero)
                    const uint32_t bits = (uint32_t) src16[off + n] << 16;
                    std::memcpy(dst + n, &bits, 4);
                }
        }
}

int serve_draft_ctx(Helper& hp, PayloadView body, uint32_t req_id, uint32_t seq, bool bf16_taps,
                    std::string& err, Socket& out) {
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
    if (body.size() != sizeof(req) + (size_t) req.n_rows * row_floats * (bf16_taps ? 2u : 4u))
        return kErrShort;
    stage_draft_rows(hp, body.data() + sizeof(req), req.n_rows, bf16_taps);
    for (int64_t r0 = 0; r0 < req.n_rows; r0 += 8) {   // the forward's width: 8 rows per call
        const int64_t nr = std::min<int64_t>(8, req.n_rows - r0);
        if (!hp.draft.add_context_f32(hp.h_taps + r0 * hp.tap_hidden, (int) req.n_planes,
                                      req.n_rows * hp.tap_hidden, req.pos0 + r0, nr, err))
            return kErrRequest;
    }
    draft_ctx_rows_served += req.n_rows;
    // the empty ack of kDraftCtx (the protocol's own payload shape)
    return out.write_reply(kDraftAck, req_id, seq, nullptr, 0, deadline_after(reply_deadline_ms(10000)));
}

int serve_draft_round(Helper& hp, PayloadView body, uint32_t req_id, uint32_t seq,
                      bool bf16_taps, std::string& err, Socket& out) {
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
    const size_t rows_bytes = (size_t) req.n_new * row_floats * (bf16_taps ? 2u : 4u);
    // L1 (l1w2's 7-vs-5 mismatch): the current wire form appends int32 n_drafts after pos0 -
    // the engine's reply cap; the helper never proposes wider than it.  The 24-byte form is the
    // legacy request (no n_drafts): the helper chooses the block, exactly as before.  L8's
    // kDraftReqBf16 has NO legacy form: an engine that sends the bf16 kind always writes
    // n_drafts, so only the full-header size matches.
    int block = (int) std::min<int64_t>(hp.draft.max_block(), kMaxDraftPicks);
    size_t header_bytes = kDraftReqLegacyBytes;
    if (body.size() == sizeof(DraftReq) + rows_bytes) {
        header_bytes = sizeof(req);
        std::memcpy(&req.n_drafts, body.data() + offsetof(DraftReq, n_drafts), sizeof(req.n_drafts));
        if (req.n_drafts < 0 || req.n_drafts > (int32_t) kMaxDraftPicks) return kErrGeometry;
        if (req.n_drafts > 0) block = std::min(block, (int) req.n_drafts);
    } else if (bf16_taps || body.size() != kDraftReqLegacyBytes + rows_bytes) {
        return kErrShort;
    }
    stage_draft_rows(hp, body.data() + header_bytes, req.n_new, bf16_taps);
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
    return out.write_reply(kDraftReply, req_id, seq, payload.data(), payload.size(), deadline_after(reply_deadline_ms(10000)));
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
    int64_t reserve_mib = kReserveDefaultMiB;   // the fill budget's reserve (clamped 64..4096 in main; the engine's hello value is ignored)
    int64_t fill_mib = 0;              // 0: the card's budget decides (today's behaviour, byte for byte)
    int64_t layers_cap = -1;           // -1: every layer the layout has
    int64_t device = 1;
    int64_t idle_timeout_ms = 120000;  // no engine connected yet: exit (the engine starts first)
    int64_t begin_timeout_ms = 60000;  // the engine is mid-window computation: a generous, bounded read
    bool once = false;
    // --persist (opt-in): when an engine session ENDS (the engine died, said bye, or idled past its
    // timeout), keep the card and LISTEN AGAIN so the NEXT engine incarnation - the one server.py
    // restarts after a GPU hiccup - reconnects on the same sockets and re-fills.  The Nov 6 prod
    // chain (kernel fence reset -> engine abort -> helper "channel closed; exiting" -> every later
    // engine logged "helper connect failed" and ran the CPU expert path) is what this answers.
    // false (the default) = the original single-session helper, byte for byte.
    bool persist = false;
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
        else if (a == "--persist") o.persist = true;
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
        "  --reserve-mib N: the VRAM the fill leaves free on this card (default 512, clamped to 64..4096;\n"
        "  below 256 warns once - the fill then has little room to survive the card's other users).\n"
        "  This option is authoritative for the fill budget: the engine's hello no longer overrides it\n"
        "  (before v0232 the option was parsed but never used, and the engine's own 512 default always\n"
        "  won - a --reserve-mib 300 helper still preflighted \"reserve 512 MiB\").\n"
        "  --fill-mib (opt-in): cap the fill's UPLOADED EXPERT BYTES at N MiB (0 = the card's budget\n"
        "  decides, exactly the behaviour before this option).  Every fill report names WHY it stopped\n"
        "  (slots / fill-mib / budget / ranked candidates exhausted) and the device's raw free MiB\n"
        "  before and after, so occupancy is measured, not guessed.\n"
        "  --dflash (opt-in, the M7 drafter lane): load this DFlash artifact onto this card and serve\n"
        "  the kDraftCtx/kDraftReq frames; needs --experts <model.gguf> for the target's head.\n"
        "  --persist (opt-in): when an engine session ends (the engine died, said bye, or idled past\n"
        "  its timeout), keep the card and LISTEN AGAIN: the next engine incarnation reconnects on the\n"
        "  same sockets and re-fills (its own kFill), so an engine death costs the expert tier only for\n"
        "  that session. SIGTERM/SIGINT still exit cleanly; without this flag the helper serves exactly\n"
        "  one engine session, as in every build before it.\n"
        "  STRATA_HELPER_REPLY_TIMEOUT_MS=N (opt-in env, no flag): every DATA reply's write deadline\n"
        "  (kRows, kRowsP, kDraftAck, kDraftReply, kHelloAck) uses N ms instead of the call's own\n"
        "  default, so a slow-but-alive engine that stops draining its socket (a reclaim-suspended\n"
        "  GPU queue) never costs the channel. Unset = each call's own default, byte for byte; a\n"
        "  DEAD peer is POLLHUP and is detected at once either way.\n"
        "  STRATA_HELPER_REPLY_RING=1 (opt-in env, default OFF): allocate the pinned REPLY RING and\n"
        "  offer it to engine hellos that ask for it (the engine's same-named env) - negotiated\n"
        "  rows replies shrink to a 16-byte doorbell and the rows travel through the memfd-shared\n"
        "  ring, one dedicated slot per channel, with the socket reply kept verbatim for any peer\n"
        "  or frame that did not negotiate it.  The lane also enables the early device-lock release\n"
        "  at kernel launch (event-driven completion) on the window tier; correctness never depends\n"
        "  on STRATA_HELPER_STREAMS either way.\n");
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
    // W2: this channel's ring slot (-1 = the lane is OFF here: never asked, or no ring exists).
    // Set by this channel's OWN hello, read by every serve below; a re-accepted peer re-helloes
    // before any frame, so it is always re-negotiated before use.
    int ring_slot = -1;
accept_peer:
    ring_slot = -1;   // W2: a new peer negotiates its own hello before any frame
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
    RxBuffer rx;   // helper-ipc-diet: ONE grow-only receive buffer per channel - the per-claim 24 MB
                   // reply frame reuses its pages; the span views below are valid while consumed
    for (;;) {
        if (g_stop.load()) {   // a signal recorded during the last window: honor it before the next read
            std::fprintf(stderr, "expert-helper: SIGTERM/SIGINT received; exiting cleanly\n");
            break;
        }
        Header head;
        PayloadView body;
        // NO lock is held across this read: an idle channel must never block the other channel's
        // work (that is the split's whole point, and the shared device lock is taken per frame only).
        int rc = out.read_frame(head, rx, body, deadline_after(o.begin_timeout_ms));
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
                // W2: reserved0 is the hello's FLAG WORD.  An unknown bit is refused by name (a
                // future flag is negotiated, never misparsed); the size check keeps its own status.
                const bool ring_ask = (req.reserved0 & kHelloWantReplyRing) != 0;
                if ((req.reserved0 & ~(uint32_t) kHelloWantReplyRing) != 0) rc = kErrProtocol;
                else if (body.size() != sizeof(req) + (size_t) claimed_bytes) rc = kErrShort;
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
                    ack.features = kFeaturePromptRows | kFeaturePromptF32 | kFeatureQ8K | kFeaturePromptBatch;
                    //   M4c (b): fp32 images accepted.  The Q8_K wire (kBeginQ8K / image_format 2 -
                    //   the CPU reference's own gate/up image through native_expert_grouped_q8k) is
                    //   advertised because this build compiled it; the ENGINE still opts in with
                    //   STRATA_EXPERT_HELPER_Q8K, and a layer whose weight type the Q8_K kernel does
                    //   not serve is refused by name at serve time (native_expert_q8k_supported).
                    // W1: the fp32 WINDOW wire (kBeginF32) is advertised because this build compiled
                    //   its GPU quant micro-kernel; the ENGINE still opts in with
                    //   STRATA_HELPER_F32_WINDOW, and a peer without the bit never sees the frame.
                    ack.features |= kFeatureF32Window;
                    // M7: the drafter lane is advertised ONLY when the drafter loaded, uploaded and
                    // bound - a peer that never advertised kFeatureDraft never sends kDraftCtx/kDraftReq,
                    // and one that did gets this transport's typed refusal if it sends them to a helper
                    // without a drafter (the frame types exist, the service does not).
                    if (hp.draft_ready) ack.features |= kFeatureDraft | kFeatureDraftBf16;   // L8: bf16 taps too
                    // W2: the ring is offered ONLY when it exists AND this hello asked for it -
                    // the ack then carries the feature bit AND the memfd itself (SCM_RIGHTS on
                    // the same frame; the kernel dups the descriptor per attachment).  A hello
                    // that did not ask keeps the exact legacy ack, even with the ring allocated.
                    if (ring_ask && hp.ring_fd >= 0) {
                        ack.features |= kFeatureReplyRing;
                        ring_slot = secondary ? 1 : 0;   // the channel's OWN slot, for the process lifetime
                    }
                    size_t free_b = 0, total_b = 0;
                    cudaMemGetInfo(&free_b, &total_b);
                    ack.free_mib = (int64_t) free_b / 1048576ll;
                    rc = ring_slot >= 0
                             ? out.write_reply_fd(kHelloAck, head.req_id, head.seq, (const uint8_t*) &ack,
                                                 sizeof(ack), deadline_after(reply_deadline_ms(5000)),
                                                 hp.ring_fd)
                             : out.write_reply(kHelloAck, head.req_id, head.seq, (const uint8_t*) &ack,
                                               sizeof(ack), deadline_after(reply_deadline_ms(5000)));
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
                    // v0232: kHello's reserve_mib is NOT taken anymore.  Until v0231 the helper's
                    // own --reserve-mib was parsed but never used and this line overwrote the
                    // budget's reserve with the ENGINE's --expert-helper-reserve-mib (default 512),
                    // so every helper-side reserve request below 512 was dead.  The helper owns its
                    // card, so its own clamped option is authoritative (set once in main); the wire
                    // field stays as-is for pre-v0232 helpers, which keep reading it.
                    g_slots = req.slots;
                    hello_seen = true;   // from here on the connection is the engine's, one-shot again
                    log_socket_buffers(out.fd(), "helper");   // helper-ipc-diet: the achieved buffer pair
                }
            }
            if (rc != kOk) { rc_loop = rc; break; }
            continue;
        }
        if (head.type == kFill) {
            rc = serve_fill(hp, body, head.req_id, head.seq, g_reserve, g_claimed,
                            g_slots > 0 ? g_slots : INT64_MAX, err, out);
            if (rc != kOk) {
                // The engine gets the status code; the operator gets the reason. 2026-11-06: the
                // iq3xxs-ud fill died kErrInternal and the log said only "request failed: helper
                // internal" - the pack-path detail was swallowed with the kError reply.
                std::fprintf(stderr, "expert-helper: fill refused: %s\n", err.c_str());
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
        if (head.type == kBegin || head.type == kBeginQ8K || head.type == kBeginF32) {
            // kBeginQ8K (default OFF, the engine's STRATA_EXPERT_HELPER_Q8K): the same window
            // request with the activations as Q8_K blocks, served under kBegin's own rules - this
            // helper advertised kFeatureQ8K, so a peer that never saw the bit never sends the frame.
            // kBeginF32 (W1, default OFF, the engine's STRATA_HELPER_F32_WINDOW): the same request
            // with the activations as RAW fp32 rows, quantized here on the GPU stream - this helper
            // advertised kFeatureF32Window, so the frame type never reaches a peer without the bit.
            if (!filled) { rc = kErrRequest; }
            else rc = serve_begin(hp, body, head.req_id, head.seq, err, out, head.type == kBeginQ8K,
                                  head.type == kBeginF32, ring_slot);
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
            else rc = serve_prompt(hp, body, head.req_id, head.seq, err, out, ring_slot);
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
        if (head.type == kBeginPB) {
            // The claim batch (default OFF - the engine's STRATA_CLAIM_BATCH with our
            // kFeaturePromptBatch): K prompt-tier claims in ONE frame, served under ONE device-lock
            // acquisition.  A peer that never saw the bit never sends it, and an engine that predates
            // it keeps kBeginP byte for byte.  The same gate and the same typed-refusal path as
            // kBeginP above.
            if (!filled) { rc = kErrRequest; }
            else rc = serve_prompt_batch(hp, body, head.req_id, head.seq, err, out);
            if (rc != kOk) {
                ErrResp e;
                e.status = rc;
                std::strncpy(e.text, err.c_str(), sizeof(e.text) - 1);
                out.write_reply(kError, head.req_id, head.seq, (const uint8_t*) &e, sizeof(e), deadline_after(5000));
                rc_loop = rc;
                break;
            }
            ++prompt_requests_served;
            if (o.once) { std::fprintf(stderr, "expert-helper: --once: one prompt batch frame served, exiting\n"); break; }
            continue;
        }
        if (head.type == kDraftCtx || head.type == kDraftReq ||
            head.type == kDraftCtxBf16 || head.type == kDraftReqBf16) {
            // L8: the bf16 kinds carry the same headers with the taps as bf16 (half the bytes).
            const bool bf16_taps = head.type == kDraftCtxBf16 || head.type == kDraftReqBf16;
            // M7 (the drafter lane): served ONLY after the engine's own gate - this helper advertised
            // kFeatureDraft in its kHelloAck, and a client that saw no bit never sends these frames.
            // A refusal answers with the transport's typed kError (the engine disables drafting for
            // the rest of the request, plan sect12); the loop ends through the existing error path,
            // which releases the card and unlinks only this helper's own socket - never a crash,
            // never a hang.
            if (!filled || !hp.draft_ready) { rc = kErrRequest; err = "no drafter is loaded"; }
            else if (head.type == kDraftCtx || head.type == kDraftCtxBf16)
                rc = serve_draft_ctx(hp, body, head.req_id, head.seq, bf16_taps, err, out);
            else rc = serve_draft_round(hp, body, head.req_id, head.seq, bf16_taps, err, out);
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
        g_reserve = o.reserve_mib;   // the helper's own clamped option, unchanged by any hello
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
    // v0232: the helper's OWN --reserve-mib is the fill budget's reserve (before, the option was
    // parsed and never used - the engine's kHello value silently decided it, so a --reserve-mib 300
    // helper still preflighted "reserve 512").  Clamped here, once, with the honest-floor warning:
    // below 256 MiB a display/peer hiccup on the card can OOM the fill; 64 remains testable.
    {
        const int64_t given = o.reserve_mib;
        o.reserve_mib = strata::core::expert_helper::clamp_reserve_mib(given);
        if (o.reserve_mib != given)
            std::fprintf(stderr, "expert-helper: --reserve-mib %lld outside %lld..%lld, clamped to %lld\n",
                         (long long) given,
                         (long long) strata::core::expert_helper::kReserveFloorMiB,
                         (long long) strata::core::expert_helper::kReserveCeilMiB,
                         (long long) o.reserve_mib);
        if (o.reserve_mib < strata::core::expert_helper::kReserveWarnBelowMiB)
            std::fprintf(stderr, "expert-helper: --reserve-mib %lld MiB is below the recommended %lld: the fill "
                                 "has little room to survive the card's other users\n",
                         (long long) o.reserve_mib,
                         (long long) strata::core::expert_helper::kReserveWarnBelowMiB);
    }
    g_reserve = o.reserve_mib;   // the budget's reserve from here on (kHello no longer overwrites it)
    if (reply_override_ms() > 0)
        std::fprintf(stderr, "expert-helper: reply write deadlines overridden to %lld ms (STRATA_HELPER_REPLY_TIMEOUT_MS)\n",
                     (long long) reply_override_ms());
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
        hp.pack_path = p;   // fill resolves per-role shard paths against this file's directory
    }    hp.scratch_bytes = (int64_t) std::max<size_t>(
        (size_t) strata::kernels::moe_hit_grouped_scratch_bytes(CAP, H, FF),
        strata::kernels::native_expert_scratch_bytes(CAP, FF));
    // The Q8_K wire's image row (2920 B at this geometry) is 40 B larger than the q8_1's (2880):
    // size the activation staging for whichever is larger, so both frames upload verbatim.
    const int64_t window_img_bytes = (int64_t) std::max<size_t>((size_t) CAP * ((size_t) H / 32) * 36,
                                                                (size_t) CAP * ((size_t) H / 256) * 292);
    hp.buffers_bytes = (int64_t) (window_img_bytes + CAP * H * 4 + sizeof(GroupMeta) + (2u << 20));
    if (!cuda_check(cudaStreamCreateWithFlags(&hp.stream, cudaStreamNonBlocking), "stream", err)) return fail("buffers", err);
    if (std::getenv("STRATA_HELPER_STREAMS")) {             // L2: opt-in, default OFF
        if (!cuda_check(cudaStreamCreateWithFlags(&hp.stream_p, cudaStreamNonBlocking), "stream_p", err))
            return fail("buffers", err);
        hp.streams_split = true;
        std::fprintf(stderr, "expert-helper: L2 stream split on (window/draft on the primary "
                             "stream, prompt on its own; fill fences both)\n");
    }
    // W2: the reply ring (opt-in STRATA_HELPER_REPLY_RING, default OFF).  Created before the
    // first accept so every hello can negotiate it; a creation failure disables the lane only -
    // the helper answers hellos without kFeatureReplyRing and serves socket replies, exactly the
    // pre-ring behaviour.
    if (std::getenv("STRATA_HELPER_REPLY_RING") && !ring_create(hp, err)) {
        if (hp.ring_map != nullptr) {
            cudaHostUnregister(hp.ring_map);
            ::munmap(hp.ring_map, (size_t) expert_helper::ring_bytes(H));
        }
        if (hp.ring_fd >= 0) ::close(hp.ring_fd);
        for (int s = 0; s < (int) kRingSlotCount; ++s) {
            if (hp.h_ring_claims[s] != nullptr) cudaFreeHost(hp.h_ring_claims[s]);
            hp.h_ring_claims[s] = nullptr;
            if (hp.ring_ev[s] != nullptr) {
                cudaEventDestroy(hp.ring_ev[s]);
                hp.ring_ev[s] = nullptr;
            }
        }
        hp.ring_map = nullptr;
        hp.ring_fd = -1;
        std::fprintf(stderr, "expert-helper: reply ring unavailable (%s); serving socket replies\n",
                     err.c_str());
    }
    if (!cuda_check(cudaHostAlloc((void**) &hp.h_meta, sizeof(GroupMeta), cudaHostAllocPortable | cudaHostAllocMapped),
                    "meta staging", err)) return fail("buffers", err);
    // H10: the launch below reads d_meta's FIELDS on the host (->ptr/->start/...), so d_meta must be
    // host-accessable: plain cudaMalloc is device memory, and a host deref of it only works while the
    // driver happens to keep the page mapped into the CPU (it flickered under fill churn -> SIGSEGV).
    if (!cuda_check(cudaHostAlloc((void**) &hp.d_meta, sizeof(GroupMeta), cudaHostAllocPortable | cudaHostAllocMapped),
                    "meta (pinned+mapped: serve_begin reads its fields on the host)", err)) return fail("buffers", err);
    if (!cuda_check(cudaMalloc((void**) &hp.d_q8, (size_t) window_img_bytes), "activation", err)) return fail("buffers", err);
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
    const int64_t prompt_img_bytes = (int64_t) std::max<size_t>((size_t) kPromptTok * ((size_t) H / 32) * 36,
                                                                (size_t) kPromptTok * ((size_t) H / 256) * 292);
    hp.prompt_buffers_bytes = (int64_t) (prompt_img_bytes + (int64_t) kPromptRows * H * 4
                                          + 2 * (int64_t) sizeof(GroupMetaP) + hp.prompt_scratch_bytes);
    hp.buffers_bytes += hp.prompt_buffers_bytes;
    if (!cuda_check(cudaHostAlloc((void**) &hp.h_meta_p, sizeof(GroupMetaP), cudaHostAllocPortable | cudaHostAllocMapped),
                    "prompt meta staging", err)) return fail("buffers", err);
    if (!cuda_check(cudaHostAlloc((void**) &hp.d_meta_p, sizeof(GroupMetaP), cudaHostAllocPortable | cudaHostAllocMapped),
                    "prompt meta (pinned+mapped: serve_prompt reads its fields on the host)", err)) return fail("buffers", err);
    if (!cuda_check(cudaMalloc((void**) &hp.d_q8_p, (size_t) prompt_img_bytes), "prompt activation", err))
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
    }

    // The second channel's thread is (re)started per engine session: the engine connects it once per
    // run, and a --persist re-listen must offer it to the next incarnation.  The binds happen once.
    auto start_chan2 = [&hp, &o] {
        if (o.socket2.empty() || hp.chan2.joinable()) return;
        hp.chan2 = std::thread([&hp, &o]() {
            std::string e2;
            const int rc2 = serve_channel(hp, hp.sock2, hp.listener2, o, true, e2);
            if (rc2 == kConnAcceptFailed)
                std::fprintf(stderr, "expert-helper: %s (the engine keeps the prompt tier on its "
                                     "primary channel; the window tier is unaffected)\n", e2.c_str());
            else if (rc2 != 0 && rc2 != kConnStop)
                std::fprintf(stderr, "expert-helper: second channel ended: %s\n", status_name(rc2));
        });
    };

    for (int gen = 1;; ++gen) {
        start_chan2();
        const int conn_rc = serve_channel(hp, hp.sock, hp.listener, o, false, err);
        // A stop latched BEFORE main latches it is a real SIGTERM/SIGINT (the serve loop breaks on
        // it), and every mode honors it with the clean exit below.
        const bool signalled = g_stop.load() != 0;
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
        std::fprintf(stderr, "expert-helper: %lld windows served, %lld prompt row requests%s; %s\n",
                     (long long) windows_served.load(), (long long) prompt_requests_served.load(),
                     o.dflash.empty() ? ""
                                      : (std::string(", ") + std::to_string((long long) draft_ctx_rows_served.load()) +
                                         " draft context rows, " + std::to_string((long long) draft_rounds_served.load()) +
                                         " draft rounds").c_str(),
                     (o.persist && !o.once && !signalled) ? "the engine's session ended" : "releasing the card");
        if (o.once || !o.persist || signalled) {
            hp.release();
            return conn_rc == 0 ? 0 : 1;
        }
        // --persist and the session ended by itself (peer EOF, bye, the engine idled past its
        // timeout): keep the card and listen again.  The next engine's kHello re-binds slots and
        // layer range; its kFill re-fills (ExpertCache::open closes the old arena first, and
        // serve_fill skips pairs the cache still holds).  Reset the engine-bound handshake state
        // exactly as serve_channel's pre-hello re-listen does.
        g_claimed.clear();
        g_reserve = o.reserve_mib;   // the helper's own clamped option, unchanged by any hello
        g_slots = 0;
        hp.layer_lo = hp.layer_hi = 0;
        if (g_wake_pipe[0] >= 0) {   // drain the wake byte: a stale wake must not end the new accept
            char drained = 0;
            while (::read(g_wake_pipe[0], &drained, 1) > 0) {}
        }
        // Clear ONLY main's own END latch; a real SIGTERM/SIGINT is a separate fact (g_sig_count)
        // and wins over the re-listen at every point of this window: before the store the count
        // gate below answers it (the drain consumed its wake byte, so the exit is clean), after
        // the store the latched g_stop and its wake byte take accept's named clean exit. (R5 P1
        // lesson, kept: the latched stop must win over any re-listen.)
        g_stop.store(0);
        if (g_sig_count.load() != 0) {
            std::fprintf(stderr, "expert-helper: SIGTERM/SIGINT recorded during the session end; exiting cleanly\n");
            hp.release();
            return 0;
        }
        std::fprintf(stderr, "expert-helper: --persist: engine session %d ended; listening again for the "
                             "next engine (card held; the next kHello/kFill re-binds and re-fills)\n", gen);
    }
}

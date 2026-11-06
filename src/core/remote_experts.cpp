#include "strata/core/remote_experts.hpp"
#include "strata/core/device.hpp"   // primary_device() (STRATA_PRIMARY_DEVICE)
#include "strata/core/remote_expert_opt.hpp"
#include "strata/core/expert_helper_socket.hpp"

#include "ggml.h"   // GGML_TYPE_Q8_K (the Q8_K wire's per-layer pack-format gate)

#include "strata/kernels/cpu/expert_layout.hpp"
#include "strata/kernels/cpu/hadamard.hpp"
#include "strata/kernels/cpu/native_expert.hpp"
#include "strata/kernels/iq_kernels.hpp"
#include "strata/kernels/quantize_act.hpp"
#include "strata/kernels/s2_expert_grouped.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace strata::core {
namespace {
constexpr int64_t H = strata::kernels::cpu::H;
constexpr int64_t FF = strata::kernels::cpu::FF;
constexpr int64_t CAP = strata::kernels::cpu::MAXT * 10;

// Small enough to copy as one pinned buffer per layer. The kernels read the
// individual arrays through pointers into the same device allocation.
struct RemoteMeta {
    unsigned long long ptr[CAP];
    int32_t start[CAP + 1];
    int32_t dst[CAP];
    int32_t tok[CAP];
    int32_t count;
};

struct DeviceScope {
    int previous = -1;
    bool ok = false;
    cudaError_t status = cudaSuccess;
    const char* failed_step = nullptr;
    explicit DeviceScope(int device) {
        status = cudaGetDevice(&previous);
        if (status != cudaSuccess) {
            previous = -1;
            failed_step = "cudaGetDevice";
            return;
        }
        status = cudaSetDevice(device);
        if (status != cudaSuccess) {
            failed_step = "cudaSetDevice";
            return;
        }
        ok = true;
    }
    ~DeviceScope() { if (previous >= 0) cudaSetDevice(previous); }
    std::string error(int device) const {
        return std::string("CUDA") + std::to_string(device) + " experts: " +
               (failed_step ? failed_step : "device switch") +
               "(" + std::to_string(device) + ") failed: " + cudaGetErrorString(status) +
               " (CUDA error " + std::to_string((int) status) + ")";
    }
};

bool check(cudaError_t result, const char* what, std::string& err, int device) {
    if (result == cudaSuccess) return true;
    err = "CUDA" + std::to_string(device) + " experts: " + what + ": " + cudaGetErrorString(result);
    return false;
}
} // namespace

RemoteExperts::~RemoteExperts() { close(); }

bool RemoteExperts::preflight(int device, double& free_gib, std::string& err) {
    int count = 0;
    if (!check(cudaGetDeviceCount(&count), "cudaGetDeviceCount", err, device)) return false;
    if (device < 0 || device >= count) {
        err = "CUDA" + std::to_string(device) + " experts: CUDA device is not visible";
        return false;
    }
    if (device == primary_device()) {   // the primary may be any visible ordinal; the helper cannot be it
        err = "CUDA" + std::to_string(device) + " experts: the helper cannot be the primary device "
                                                 "(STRATA_PRIMARY_DEVICE)";
        return false;
    }
    // The layer waits for this GPU on the CPU pool's critical path: spin instead of sleeping, whose wake-up
    // costs more than a small expert batch takes (measured: ~0.3 ms per round trip on Windows).  Only possible
    // before the device's context exists, so first thing; STRATA_REMOTE_SPIN=0 keeps the driver's default.
#if !defined(STRATA_USE_HIP) && !defined(STRATA_GPU_METAL)
    const char* spin = std::getenv("STRATA_REMOTE_SPIN");
    if (!(spin && spin[0] == '0')) cudaInitDevice(device, cudaDeviceScheduleSpin | cudaDeviceMapHost, 0);
    cudaGetLastError();
#endif
    // HIP has no cudaInitDevice equivalent; retain its default scheduling policy.
    DeviceScope scope(device);
    if (!scope.ok) { err = scope.error(device); return false; }
    size_t free_bytes = 0, total_bytes = 0;
    if (!check(cudaMemGetInfo(&free_bytes, &total_bytes), "preflight free memory", err, device)) return false;
    free_gib = (double) free_bytes / 1073741824.0;
    return true;
}

void RemoteExperts::close() {
    if (proxy_) {   // the proxy tier owns no device resource: close the channel, nothing else
        if (helper_ != nullptr) {
            // keep the evidence: the DONE line's helper= counters must survive the channel's
            // close (r15 printed nothing because the stats died with the client object)
            proxy_stat_reqs_ += helper_->stat_requests();
            proxy_stat_rows_ += helper_->stat_rows();
            proxy_stat_bytes_ += helper_->stat_bytes();
            proxy_stat_timeouts_ += helper_->stat_timeouts();
            proxy_stat_fallbacks_ += helper_->stat_fallbacks();
            proxy_stat_prompt_reqs_ += helper_->stat_prompt_requests();
            proxy_stat_prompt_rows_ += helper_->stat_prompt_rows();
            std::fprintf(stderr, "strata generate: expert-helper: channel closed after %lld requests / %lld rows "
                                 "(timeouts %lld, CPU fallbacks %lld)\n",
                         (long long) proxy_stat_reqs_, (long long) proxy_stat_rows_,
                         (long long) proxy_stat_timeouts_, (long long) proxy_stat_fallbacks_);
            helper_->bye();
        }
        delete helper_;
        helper_ = nullptr;
        if (helper2_ != nullptr) {   // the prompt tier's own channel (opt-in --expert-helper-socket2)
            proxy_stat_prompt_reqs_ += helper2_->stat_prompt_requests();
            proxy_stat_prompt_rows_ += helper2_->stat_prompt_rows();
            std::fprintf(stderr, "strata generate: expert-helper: the prompt tier's channel (%s) closed "
                                 "after %lld prompt requests / %lld prompt rows\n",
                         proxy_path2_.c_str(), (long long) helper2_->stat_prompt_requests(),
                         (long long) helper2_->stat_prompt_rows());
            helper2_->bye();
            delete helper2_;
            helper2_ = nullptr;
        }
        proxy_ = false;
        healthy_ = false;
        disabled_ = false;
        healthy2_ = false;
        disabled2_ = false;
        prompt_chan2_ = false;
        proxy_path2_.clear();
        proxy_resident_bits_.clear();
        proxy_resident_ = 0;
        proxy_bytes_ = 0;
        proxy_ms_begin_ = proxy_ms_wait_ = 0;
        proxy_claims_.clear();
    }
    if (device_ < 0) return;
    DeviceScope scope(device_);
    if (scope.ok) {
        if (stream_) cudaStreamSynchronize(stream_);
        if (read_stream_) cudaStreamSynchronize(read_stream_);
        cache_.close();
        if (d_x_) cudaFree(d_x_);
        if (d_out_) cudaFree(d_out_);
        if (d_q8_) cudaFree(d_q8_);
        if (d_scales_) cudaFree(d_scales_);
        if (d_xr_) cudaFree(d_xr_);
        if (d_scratch_) cudaFree(d_scratch_);
        if (d_meta_) cudaFree(d_meta_);
        if (h_x_) cudaFreeHost(h_x_);
        if (h_out_) cudaFreeHost(h_out_);
        if (h_meta_) cudaFreeHost(h_meta_);
        if (stream_) cudaStreamDestroy(stream_);
        if (read_stream_) cudaStreamDestroy(read_stream_);
    }
    device_ = -1;
    stream_ = nullptr;
    read_stream_ = nullptr;
    h_x_ = h_out_ = d_x_ = d_out_ = nullptr;
    h_meta_ = d_meta_ = nullptr;
    d_q8_ = nullptr;
    d_scales_ = nullptr;
    d_xr_ = nullptr;
    d_sx_ = d_sh_ = nullptr;
    d_scratch_ = nullptr;
    d_start_ = d_dst_ = d_tok_ = d_count_ = nullptr;
    d_ptr_ = nullptr;
    original_row_.clear();
    layers_present_.clear();
}

bool RemoteExperts::open(int device, int slots, int64_t layers, int64_t experts,
                         const std::vector<std::pair<int32_t, int32_t>>& ranked,
                         const ExpertCache& primary, ExpertSource& source,
                         std::vector<uint8_t>& claimed, std::string& err, bool auto_size) {
    close();
    int count = 0;
    if (!check(cudaGetDeviceCount(&count), "cudaGetDeviceCount", err, device)) return false;
    if (device < 0 || device >= count || slots <= 0 || ranked.empty() ||
        layers <= 0 || experts <= 0 || claimed.size() != (size_t) layers * (size_t) experts ||
        device == primary_device()) {   // the helper cannot be the primary device (any visible ordinal)
        err = "CUDA" + std::to_string(device) + " experts: need the device, ranked experts and positive slot count";
        return false;
    }
    DeviceScope scope(device);
    if (!scope.ok) { err = scope.error(device); return false; }
    device_ = device;
    n_expert_ = experts;
    const auto& lay = strata::kernels::cpu::expert_layout();
    size_t free_bytes = 0, total_bytes = 0;
    if (!check(cudaMemGetInfo(&free_bytes, &total_bytes), "free memory", err, device)) { close(); return false; }
    uint64_t needed = 0;
    std::vector<std::pair<int32_t, int32_t>> selected;
    selected.reserve((size_t) std::min<int64_t>(slots, layers * experts));
    std::vector<uint8_t> picked(claimed.size(), 0);
    for (const auto& pair : ranked) {
        if (pair.first < 0 || pair.first >= layers || pair.second < 0 || pair.second >= experts) continue;
        const size_t index = (size_t) pair.first * (size_t) experts + (size_t) pair.second;
        if (primary.slot_of(pair.first, pair.second) < 0 && !claimed[index] && !picked[index]) {
            const uint64_t bytes = lay.native ? (lay.blob_bytes(pair.first) + 255) / 256 * 256 : lay.max_blob;
            if (auto_size && needed + bytes + (512ull << 20) > free_bytes) break;
            selected.push_back(pair);
            needed += bytes;
            picked[index] = 1;
            if ((int) selected.size() >= slots) break;
        }
    }
    if (selected.empty()) {
        err = "CUDA" + std::to_string(device) + (auto_size ? " experts: no unclaimed expert fits with 512 MiB free"
                                                          : " experts: no unclaimed experts remain");
        close(); return false;
    }
    std::vector<int64_t> sizes;
    if (lay.native) {
        sizes.reserve(selected.size());
        for (const auto& pair : selected) sizes.push_back((int64_t) lay.blob_bytes(pair.first));
    }
    // Leave room for the CUDA context, staging and later driver allocations, especially under WDDM.
    if (needed + (512ull << 20) > free_bytes) {
        err = "CUDA" + std::to_string(device) + " experts: slots leave less than 512 MiB free; reduce --expert-cache-device" + std::to_string(device);
        close(); return false;
    }
    const bool cache_ok = lay.native ? cache_.open_sized(sizes, layers, experts, err)
                                     : cache_.open((int64_t) selected.size(), layers, experts,
                                                   (int64_t) lay.max_blob, err);
    if (!cache_ok) { err = "CUDA" + std::to_string(device) + " experts: " + err; close(); return false; }
    for (const auto& pair : selected) {
        const int32_t slot = cache_.admit(pair.first, pair.second);
        const uint8_t* blob = source.blob(pair.first, pair.second);
        if (slot < 0 || !blob || !cache_.fill_slot_blocking(slot, blob, err, (int64_t) lay.blob_bytes(pair.first))) {
            err = "CUDA" + std::to_string(device) + " experts: " +
                  (err.empty() ? "cache fill failed" : err);
            close(); return false;
        }
    }
    const auto& first = selected.front();
    if (!cache_.verify_slot(cache_.slot_of(first.first, first.second), source.blob(first.first, first.second),
                            err, (int64_t) lay.blob_bytes(first.first))) {
        err = "CUDA" + std::to_string(device) + " experts: " + err;
        close(); return false;
    }

    const size_t scratch = std::max<size_t>(
        (size_t) strata::kernels::moe_hit_grouped_scratch_bytes(CAP, H, FF),
        strata::kernels::native_expert_scratch_bytes(CAP, FF));
    const size_t meta_bytes = sizeof(RemoteMeta) + (remote_opt_ ? remote_opt_->metadata_bytes() : 0);
    const bool allocated =
        check(cudaStreamCreateWithFlags(&stream_, cudaStreamNonBlocking), "stream", err, device) &&
        check(cudaHostAlloc((void**) &h_x_, (size_t) CAP * H * sizeof(float), cudaHostAllocPortable | cudaHostAllocMapped), "input staging", err, device) &&
        check(cudaHostAlloc((void**) &h_out_, (size_t) CAP * H * sizeof(float), cudaHostAllocPortable | cudaHostAllocMapped), "result staging", err, device) &&
        check(cudaHostAlloc(&h_meta_, meta_bytes, cudaHostAllocPortable), "metadata staging", err, device) &&
        check(cudaMalloc((void**) &d_x_, (size_t) CAP * H * sizeof(float)), "input", err, device) &&
        check(cudaMalloc((void**) &d_out_, (size_t) CAP * H * sizeof(float)), "result", err, device) &&
        check(cudaMalloc((void**) &d_q8_, (size_t) CAP * (H / 32) * 36), "activation", err, device) &&
        check(cudaMalloc((void**) &d_scales_, (size_t) CAP * (H / 32) * sizeof(float)), "activation scales", err, device) &&
        check(cudaMalloc(&d_scratch_, scratch), "scratch", err, device) &&
        check(cudaMalloc(&d_meta_, meta_bytes), "group metadata", err, device) &&
        (!strata::kernels::cpu::hadamard_spec().any() ||
         check(cudaMalloc((void**) &d_xr_, (size_t) CAP * H * sizeof(float)), "rotated input", err, device));
    if (!allocated) { close(); return false; }
    if (!check(cudaStreamCreateWithFlags(&read_stream_, cudaStreamNonBlocking), "cache read stream", err, device)) {
        close();
        return false;
    }
    if (strata::kernels::cpu::hadamard_spec().any()) {   // APR: this device's copies of the sign vectors
        d_sx_ = strata::kernels::hadamard_device_signs(strata::kernels::cpu::hadamard_signs((int) H), (int) H);
        d_sh_ = strata::kernels::hadamard_device_signs(strata::kernels::cpu::hadamard_signs((int) FF), (int) FF);
    }
    // Zero-copy: the helper reads its input from, and writes its compact rows into, the pinned host buffers
    // directly - two copies fewer per layer, each of which is a PCIe round trip.  STRATA_REMOTE_ZEROCOPY=0 copies.
    const char* zc = std::getenv("STRATA_REMOTE_ZEROCOPY");
    zero_copy_ = !(zc && zc[0] == '0') &&
                 cudaHostGetDevicePointer((void**) &z_x_, h_x_, 0) == cudaSuccess &&
                 cudaHostGetDevicePointer((void**) &z_out_, h_out_, 0) == cudaSuccess;
    cudaGetLastError();
    auto* meta = (RemoteMeta*) d_meta_;
    d_ptr_ = meta->ptr;
    d_start_ = meta->start;
    d_dst_ = meta->dst;
    d_tok_ = meta->tok;
    d_count_ = &meta->count;
    owned_.resize(CAP);
    layers_present_.assign((size_t) layers, 0);
    group_of_.resize(CAP);
    group_id_.reserve(CAP);
    ptr_.reserve(CAP);
    start_.reserve(CAP + 1);
    dst_.reserve(CAP);
    tok_.reserve(CAP);
    original_row_.reserve(CAP);
    computed_ = 0;
    launched_layers_ = 0;
    returned_bytes_ = full_row_bytes_ = 0;
    for (const auto& pair : selected) {
        layers_present_[(size_t) pair.first] = 1;
        claimed[(size_t) pair.first * (size_t) experts + (size_t) pair.second] = 1;
    }
    return true;
}

// ==================== R2: the out-of-process CUDA helper (PROXY), CPU-only ====================
//
// Every function below runs in the engine's own process WITHOUT a single device call: on the HIP
// engine (the split32 topology's real process) no CUDA runtime is mapped at all, and on a CUDA
// engine the proxy makes no call either - the helper owns all NVIDIA memory.  The channel is the
// framed AF_UNIX transport (include/strata/core/expert_helper_protocol.hpp); its I/O is shared
// verbatim with the helper (tools/mixed_gpu/sidecar/helper_frame.cpp).
bool RemoteExperts::open_socket(const std::string& path, int64_t slots, int64_t layers, int64_t experts,
                                int64_t n_embd, int64_t n_ff, int64_t layer_lo, int64_t layer_hi,
                                int64_t reserve_mib, int64_t timeout_ms,
                                const std::vector<std::pair<int32_t, int32_t>>& ranked,
                                const std::vector<uint8_t>& claimed, std::string& err,
                                const std::string& path2) {
    close();
    const char* tag = "expert-helper";
    if (slots <= 0 || layers <= 0 || experts <= 0 || ranked.empty() ||
        claimed.size() != (size_t) layers * (size_t) experts) {
        err = std::string(tag) + ": need positive slots/geometry, a ranked list and a full claimed bitmap";
        return false;
    }
    if (n_embd != H || n_ff != FF || n_embd % 32 != 0 || n_ff % 32 != 0 ||
        n_embd > expert_helper::kMaxEmb || n_ff > expert_helper::kMaxFf ||
        layers > expert_helper::kMaxTok * 10000 || experts > 65536 ||
        layer_lo < 0 || layer_hi < layer_lo || layer_hi >= layers) {
        err = std::string(tag) + ": geometry outside the kernel contract (H=" + std::to_string(H) +
              ", FF=" + std::to_string(FF) + ", layer range " + std::to_string(layer_lo) + ".." +
              std::to_string(layer_hi) + " of " + std::to_string(layers) + ")";
        return false;
    }
#if !defined(__linux__)
    // AF_UNIX is the transport this design accepts.  Windows has no AF_UNIX in this topology: an
    // explicit refusal at open, never a build break, exactly as the protocol header promises.
    err = std::string(tag) + ": the helper socket (AF_UNIX) is Linux-only in this build";
    return false;
#else
    std::string why;
    if (!expert_helper::socket_path_connect_ok(path, why)) { err = std::string(tag) + ": " + why; return false; }   // H7: bind-side socket_path_ok refused the helper's own bound socket
    proxy_ = true;   // from here on every accessor answers as a proxy, on success AND on failure
    proxy_layers_ = layers;
    proxy_experts_ = experts;
    proxy_n_embd_ = n_embd;
    proxy_lo_ = layer_lo;
    proxy_hi_ = layer_hi;
    proxy_slots_ = slots;
    proxy_reserve_mib_ = reserve_mib;
    proxy_timeout_ms_ = timeout_ms > 0 ? timeout_ms : 20;
    proxy_connect_ms_ = 2000;
    proxy_path_ = path;
    helper_ = new ExpertHelperClient();
    int rc = helper_->connect(path, proxy_connect_ms_);
    if (rc != expert_helper::kOk) {
        err = std::string(tag) + ": connect to " + path + " failed: " + expert_helper::status_name(rc) +
              " (start the helper before the engine; the CPU path stays in use)";
        close();
        return false;
    }
    int64_t free_mib = 0;
    rc = helper_->hello(layers, experts, n_embd, n_ff, layer_lo, layer_hi, slots, reserve_mib, claimed,
                        -1, free_mib);
    if (rc != expert_helper::kOk) {
        err = std::string(tag) + ": handshake refused: " + expert_helper::status_name(rc);
        close();
        return false;
    }
    std::vector<std::pair<int32_t, int32_t>> resident;
    int64_t bytes = 0;
    rc = helper_->fill_report(ranked, -1, resident, bytes);
    if (rc != expert_helper::kOk) {
        err = std::string(tag) + ": fill refused: " + expert_helper::status_name(rc);
        close();
        return false;
    }
    // Mirror the helper's authoritative resident set.  A pair outside the layer range, a pair a
    // stage already owns, an out-of-range expert, a duplicate, or more pairs than the ranked list
    // is a contract violation the engine refuses (the disjointness of the two tiers is the
    // design's condition; the helper cannot take a row a stage computes).
    proxy_resident_bits_.assign((size_t) layers * (size_t) experts, 0);
    int64_t mirrored = 0;
    for (const auto& pair : resident) {
        const int64_t l = pair.first, e = pair.second;
        if (l < proxy_lo_ || l > proxy_hi_ || e < 0 || e >= experts ||
            claimed[(size_t) l * (size_t) experts + (size_t) e] ||
            proxy_resident_bits_[(size_t) l * (size_t) experts + (size_t) e]) {
            err = std::string(tag) + ": fill report is not disjoint from the stages (" +
                  std::to_string(l) + "," + std::to_string(e) + ")";
            close();
            return false;
        }
        proxy_resident_bits_[(size_t) l * (size_t) experts + (size_t) e] = 1;
        ++mirrored;
    }
    if (mirrored > (int64_t) ranked.size()) { err = std::string(tag) + ": fill report exceeds the ranked list"; close(); return false; }
    proxy_resident_ = mirrored;
    proxy_bytes_ = bytes;
    healthy_ = true;
    disabled_ = false;
    n_expert_ = experts;
    if (mirrored == 0) {
        err = std::string(tag) + ": the helper filled nothing (its VRAM budget left no slot); the CPU path stays in use";
        close();
        return false;
    }
    std::fprintf(stderr, "expert-helper: %lld rows on %s (mirrored %lld/%lld slots, %.2f GiB, device free at hello %lld MiB)\n",
                 (long long) mirrored, path.c_str(), (long long) mirrored, (long long) slots,
                 (double) proxy_bytes_ / 1073741824.0, (long long) free_mib);

    // ---- The PROMPT tier's own channel (opt-in `path2`, the engine's --expert-helper-socket2) ----
    // Channel A keeps the fill, the window tier and the drafter lane; channel B carries the
    // prompt tier's kBeginP/kRowsP alone, so a prompt row stops queueing behind a window's
    // in-flight request (and the other way round).  The protocol is unchanged: the SAME hello runs
    // on B, no kFill is sent there, and both channels serve rows out of the one filled mirror.
    // Every failure below is a NOTICE and keeps the single-channel behaviour: the prompt tier stays
    // on channel A, the helper stays open, the run continues.  A peer whose caps do not match is
    // refused by the client's own hello (the ack's max_tok / k / max_entries are checked there).
    if (!path2.empty()) {
        const char* why2 = nullptr;
        std::string detail;
        if (path2 == path) {
            why2 = "names the same path as --expert-helper-socket";
        } else if (!expert_helper::socket_path_connect_ok(path2, detail)) {
            why2 = detail.c_str();
        } else {
            helper2_ = new ExpertHelperClient();
            proxy_path2_ = path2;
            const int rc2 = helper2_->connect(path2, proxy_connect_ms_);
            if (rc2 != expert_helper::kOk) {
                why2 = expert_helper::status_name(rc2);
            } else {
                int64_t free2_mib = 0;
                const int rc2h = helper2_->hello(layers, experts, n_embd, n_ff, layer_lo, layer_hi,
                                                 slots, reserve_mib, claimed, -1, free2_mib);
                if (rc2h != expert_helper::kOk) {
                    detail = std::string("handshake refused: ") + expert_helper::status_name(rc2h) +
                             " (the second channel's caps must match the first)";
                    why2 = detail.c_str();
                } else if (!helper2_->prompt_rows_supported()) {
                    why2 = "the peer on the second socket does not advertise the prompt tier";
                } else {
                    healthy2_ = true;
                    disabled2_ = false;
                    prompt_chan2_ = true;   // the first request's prompt tier rides channel B
                }
            }
            if (!healthy2_) { delete helper2_; helper2_ = nullptr; proxy_path2_.clear(); }
        }
        if (why2 != nullptr) {
            std::fprintf(stderr, "expert-helper: --expert-helper-socket2 %s: %s; the prompt tier keeps "
                                 "the primary channel (window and prompt rows share one socket, as "
                                 "they always did)\n", path2.c_str(), why2);
        } else {
            std::fprintf(stderr, "expert-helper: two channels on %s + %s: the window tier (and the "
                                 "drafter lane) on the first, the prompt tier on the second - each "
                                 "channel fails on its own, the other keeps serving\n",
                         path.c_str(), path2.c_str());
        }
    }
    return true;
#endif
}

bool RemoteExperts::note_request_start() {
    if (!proxy_) return false;
    disabled_ = false;   // re-arm the tier that disabled itself inside the previous request
    disabled2_ = false;  // the prompt tier's channel re-arms on its own
    // The prompt tier's channel is resolved ONCE, here at the request boundary, and pinned for the
    // whole request: prefill stages its rows into THAT channel's pinned buffer before it sends, so a
    // channel that dies mid-request makes the send fail by name (the caller's same-layer fallback
    // runs) and never sends frames built for the other channel.  Channel B dies -> the next request
    // pins A and the window tier never notices; A dies -> the window tier falls back and the prompt
    // tier keeps serving on B.
    prompt_chan2_ = helper2_ != nullptr && healthy2_;
    return helper_usable();
}

uint64_t RemoteExperts::helper_requests() const { return proxy_stat_reqs_ + (helper_ == nullptr ? 0 : helper_->stat_requests()); }
uint64_t RemoteExperts::helper_rows() const { return proxy_stat_rows_ + (helper_ == nullptr ? 0 : helper_->stat_rows()); }
uint64_t RemoteExperts::helper_bytes() const { return proxy_stat_bytes_ + (helper_ == nullptr ? 0 : helper_->stat_bytes()); }
uint64_t RemoteExperts::helper_timeouts() const { return proxy_stat_timeouts_ + (helper_ == nullptr ? 0 : helper_->stat_timeouts()); }
uint64_t RemoteExperts::helper_fallbacks() const { return proxy_stat_fallbacks_ + (helper_ == nullptr ? 0 : helper_->stat_fallbacks()); }
void RemoteExperts::note_fallback() { if (helper_ != nullptr) helper_->note_fallback(); }

bool RemoteExperts::proxy_begin(int64_t layer, const float* x, const int32_t* ids, int64_t n_tok, int64_t k,
                                const int32_t* primary_res, std::string& err, std::vector<int32_t>& claimed_out) {
    claimed_out.clear();
    proxy_claims_.clear();
    const int64_t n = n_tok * k;
    if (n <= 0 || n > CAP || n_tok > strata::kernels::cpu::MAXT || k != 10 || layer < 0 || layer >= proxy_layers_) {
        err = "expert-helper: invalid layer, routing width or window size";
        return false;
    }
    // An unhealthy or request-disabled tier claims ZERO rows and SUCCEEDS: the dispatch's plan
    // already classifies every entry, so the CPU computes what the helper would have taken -
    // the request keeps its exact semantics with no helper.
    if (!helper_usable()) return true;
    // The claims: entries the plan left to the helper (the dispatch classifies helper candidates as
    // kind -1 and passes the primary residency; the primary's own kind >= 0 entries never reach a
    // helper's begin at all - see src/core/expert_source.cpp:4907), routed to an expert the
    // mirrored fill report holds.  Routing order IS the claim order: the helper's grouped kernel
    // answers one dense row per claimed entry, in claim order, and the client scatters them back
    // at the original indices (validate_rows_payload).
    std::vector<int32_t>& claims = proxy_claims_;
    claims.clear();
    for (int64_t i = 0; i < n; ++i) {
        const int32_t e = ids[i];
        if (e < 0 || e >= proxy_experts_) continue;
        if (primary_res && primary_res[(size_t) layer * (size_t) proxy_experts_ + (size_t) e] >= 0) continue;
        if (!proxy_holds(layer, e)) continue;
        claims.push_back((int32_t) i);
    }
    if (claims.empty()) return true;
    // A duplicate routed id inside the window: the in-process tier computes ONE group row per
    // (expert) and writes it at every routed index.  The proxy keeps every routed index (the
    // helper's grouped kernel does the same: each entry is one dst row), so claims list each.
    // The Q8_K wire (default OFF: STRATA_EXPERT_HELPER_Q8K, a peer that advertised kFeatureQ8K, and
    // a layer whose own pack format is Q8_K gate/up on a kernel-served weight type): the image is
    // the CPU reference's own activation - native_quant_act, the same function the CPU-offloaded
    // rows of THIS window quantize with - so the helper's grouped Q8_K kernel reproduces the
    // reference arithmetic instead of the q8_1 MMQ contract (docs/work/MIXED_P2_REFERENCE.md).
    // Any miss keeps the pinned q8_1 image and today's frame, byte for byte.
    if (std::getenv("STRATA_EXPERT_HELPER_Q8K") != nullptr && helper_->q8k_supported()) {
        const auto& lay = strata::kernels::cpu::expert_layout();
        if (layer >= 0 && (size_t) layer < lay.fmt.size()) {
            const auto& f = lay.fmt[(size_t) layer];
            if (f.gu_act == (int) GGML_TYPE_Q8_K &&
                strata::kernels::native_expert_q8k_supported(f.gu_type, proxy_n_embd_)) {
                thread_local std::vector<uint8_t> q8k;
                q8k.assign((size_t) n_tok * (size_t) f.act_bytes, 0);
                for (int64_t t = 0; t < n_tok; ++t)
                    strata::kernels::cpu::native_quant_act(f, x + (size_t) t * (size_t) proxy_n_embd_,
                                                           q8k.data() + (size_t) t * (size_t) f.act_bytes);
                const int rc = helper_->begin_q8k((int32_t) layer, (int32_t) n_tok, ids, primary_res,
                                                  q8k.data(), q8k.size(), proxy_timeout_ms_);
                if (rc != expert_helper::kOk) {
                    // The begin-time fallback, verbatim: the window is fully CPU-served, the tier
                    // re-arms at the next request, and nothing failed.
                    healthy_ = false;
                    disabled_ = true;
                    claims.clear();
                    err = std::string("expert-helper: begin_q8k send failed: ") + expert_helper::status_name(rc);
                    return true;
                }
                claimed_out = claims;
                computed_ += (int64_t) claims.size();
                launched_layers_ += 1;
                proxy_ms_begin_ = helper_->ms_begin();
                return true;
            }
        }
    }
    const int64_t q8_blocks = n_tok * (proxy_n_embd_ / 32);
    thread_local std::vector<uint8_t> q8;
    q8.assign((size_t) q8_blocks * 36u, 0);
    quantize_q8_1_cpu(x, n_tok * proxy_n_embd_, q8.data());
    const int rc = helper_->begin((int32_t) layer, (int32_t) n_tok, ids, primary_res, q8.data(),
                                  q8.size(), proxy_timeout_ms_);
    if (rc != expert_helper::kOk) {
        // The channel is closed by the client; the tier is out for this request.  Claimed_out
        // stays EMPTY for the caller's begin contract: the dispatch's kind[] keeps these entries
        // CPU-served, so the CPU computes them right here in the same window (the begin-time
        // fallback), and the tier re-arms at the next request.
        healthy_ = false;
        disabled_ = true;
        claims.clear();
        err = std::string("expert-helper: begin send failed: ") + expert_helper::status_name(rc);
        return true;   // the window is fully CPU-served; nothing failed
    }
    claimed_out = claims;
    computed_ += (int64_t) claims.size();
    launched_layers_ += 1;
    proxy_ms_begin_ = helper_->ms_begin();
    return true;
}

bool RemoteExperts::proxy_finish(float* out, std::string& err) {
    if (proxy_claims_.empty()) return true;
    const int32_t rows = (int32_t) proxy_claims_.size();
    int32_t got = 0;
    const int rc = helper_->finish(out, proxy_n_embd_, 0, 10, proxy_claims_, proxy_timeout_ms_, got);
    proxy_ms_wait_ = helper_->ms_wait();
    if (rc != expert_helper::kOk) {
        healthy_ = false;
        disabled_ = true;
        proxy_claims_.clear();   // the dispatch recomputes EXACTLY these rows on the CPU
        err = std::string("expert-helper: finish failed: ") + expert_helper::status_name(rc);
        return false;
    }
    returned_bytes_ += (uint64_t) got * (uint64_t) proxy_n_embd_ * 4ull;
    full_row_bytes_ += (uint64_t) rows * (uint64_t) proxy_n_embd_ * 4ull;
    proxy_claims_.clear();
    return true;
}

// ================= M2 (worker prefill dispatch, docs/work/MIXED_BACKEND_RPC_PLAN.md): the prompt tier's
// PROXY passthroughs.  The decode wrappers above return "claims zero and SUCCEEDS" for an unusable
// tier; these do NOT: the prefill dispatch stages real rows into the tier's buffer BEFORE the send,
// so an unusable tier must refuse by NAME and run the same fallback arm a failed send does. =================
int RemoteExperts::begin_prompt(int32_t layer, const ExpertHelperPromptPlan& plan, const int32_t* host_res,
                                int64_t deadline_ms) {
    ExpertHelperClient* ch = prompt_chan();
    if (!proxy_ || ch == nullptr || !prompt_usable()) return expert_helper::kErrUnhealthy;
    const int rc = ch->begin_prompt(layer, plan, host_res, deadline_ms);
    if (rc != expert_helper::kOk) {
        // The tier's own channel latches, and ONLY its channel: with two channels a prompt death
        // disables the prompt tier for the rest of the request while the window tier keeps serving
        // (and the reverse).  Single channel: the shared latch, exactly as before.
        if (ch == helper2_) { healthy2_ = false; disabled2_ = true; }
        else { healthy_ = false; disabled_ = true; }
    }
    return rc;
}

int RemoteExperts::finish_prompt(float* out, int64_t n_embd, const std::vector<int32_t>& claims,
                                 int64_t timeout_ms, int32_t& rows) {
    rows = 0;
    ExpertHelperClient* ch = prompt_chan();
    if (!proxy_ || ch == nullptr || !prompt_usable()) return expert_helper::kErrUnhealthy;
    const int rc = ch->finish_prompt(out, n_embd, claims, timeout_ms, rows);
    if (rc != expert_helper::kOk) {
        if (ch == helper2_) { healthy2_ = false; disabled2_ = true; }
        else { healthy_ = false; disabled_ = true; }
        rows = 0;   // the client's whole-or-nothing rule: a violation applies NOTHING
    }
    return rc;
}

// ================= M7 (the drafter lane): the remote drafter's passthroughs =================
//
// The deadline.  proxy_timeout_ms_ (--expert-helper-timeout-ms, default 20 ms) is tuned for the
// WINDOW's replies, which the helper computes while the CPU pool works; a draft round is fully
// synchronous - the engine waits on the helper's 5-layer forward - and the plan's budget for it is
// the slot the local drafter occupied between verifies (docs/work/MIXED_BACKEND_RPC_PLAN.md
// sect12).  The floor is 2000 ms; STRATA_DRAFT_TIMEOUT_MS overrides.  Draft calls NEVER touch
// healthy_/disabled_: the window tier keeps serving, and the caller's own latch keeps drafting off
// for the rest of the request (plan sect12's failure rule).

namespace {
int64_t draft_deadline_ms(int64_t proxy_timeout_ms) {
    static const int64_t env_ms = [] {
        const char* v = std::getenv("STRATA_DRAFT_TIMEOUT_MS");
        return v != nullptr ? std::atoll(v) : 0;
    }();
    if (env_ms > 0) return env_ms;
    return std::max<int64_t>(proxy_timeout_ms, 2000);
}
}  // namespace

int RemoteExperts::draft_context(const float* rows, int64_t n_rows, int64_t pos0, int reset) {
    if (!draft_capable()) return expert_helper::kErrUnhealthy;   // in-process tier: named refusal
    return helper_->draft_context(rows, n_rows, pos0, reset, draft_deadline_ms(proxy_timeout_ms_));
}

int RemoteExperts::draft_round(const float* rows, int64_t n_new, int64_t pos0, int32_t anchor,
                               int32_t* out_picks, float* out_probs, int32_t out_cap, int32_t& n_out) {
    n_out = 0;
    if (!draft_capable()) return expert_helper::kErrUnhealthy;
    return helper_->draft_round(rows, n_new, pos0, anchor, out_picks, out_probs, out_cap,
                                draft_deadline_ms(proxy_timeout_ms_), n_out);
}

bool RemoteExperts::begin(int64_t layer, const float* x, const int32_t* ids, int64_t n_tok,
                          int64_t k, const int32_t* kind, const int32_t* primary_res,
                          std::string& err, std::vector<int32_t>& claimed_out) {
    if (proxy_) return proxy_begin(layer, x, ids, n_tok, k, primary_res, err, claimed_out);
    claimed_out.clear();   // an in-process tier's claims are read through owns() by the dispatch
    const bool ok = begin(layer, x, ids, n_tok, k, kind, primary_res, err);
    if (ok && device_ >= 0) {
        const int64_t n = n_tok * k;
        for (int64_t i = 0; i < n; ++i)
            if (owned_[(size_t) i]) claimed_out.push_back((int32_t) i);
    }
    return ok;
}

bool RemoteExperts::begin(int64_t layer, const float* x, const int32_t* ids, int64_t n_tok,
                          int64_t k, const int32_t* kind, const int32_t* primary_res,
                          std::string& err) {
    if (proxy_) {   // no claim list: the tier claims ZERO rows, the CPU serves the window
        proxy_claims_.clear();
        return true;
    }
    // cumulative host time in here (staging and launches), reported per request by the driver
    struct Timer { double& acc; std::chrono::steady_clock::time_point t0 = std::chrono::steady_clock::now();
        ~Timer() { acc += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count(); } } timer{ms_begin_};
    const int64_t n = n_tok * k;
    if (n <= 0 || n > CAP || n_tok > strata::kernels::cpu::MAXT || k != 10 || layer < 0 ||
        (size_t) layer >= layers_present_.size() || device_ < 0) {
        err = "CUDA" + std::to_string(device_) + " experts: invalid layer, routing width or window size";
        return false;
    }
    std::fill(owned_.begin(), owned_.begin() + n, 0);
    group_id_.clear(); ptr_.clear(); start_.clear(); dst_.clear(); tok_.clear(); original_row_.clear();
    if (!layers_present_[(size_t) layer]) return true;
    std::fill(group_of_.begin(), group_of_.begin() + n, -1);
    for (int64_t i = 0; i < n; ++i) {
        const int32_t e = ids[i];
        if ((kind && kind[i] != -1) || e < 0 || e >= n_expert_ ||
            (primary_res && primary_res[(size_t) layer * (size_t) n_expert_ + (size_t) e] >= 0)) continue;
        const int32_t slot = cache_.slot_of(layer, e);
        if (slot < 0) continue;
        owned_[(size_t) i] = 1;
        int32_t group = -1;
        for (size_t g = 0; g < group_id_.size(); ++g)
            if (group_id_[g] == e) { group = (int32_t) g; break; }
        if (group < 0) {
            group = (int32_t) group_id_.size();
            group_id_.push_back(e);
            ptr_.push_back((unsigned long long) cache_.device_slot(slot));
        }
        group_of_[(size_t) i] = group;
        ++computed_;
    }
    if (group_id_.empty()) return true;
    for (size_t g = 0; g < group_id_.size(); ++g) {
        start_.push_back((int32_t) dst_.size());
        for (int64_t i = 0; i < n; ++i) if (group_of_[(size_t) i] == (int32_t) g) {
            // The grouped kernels read the activation from `tok`, so their
            // output row can instead be packed densely for the USB4 return.
            original_row_.push_back((int32_t) i);
            dst_.push_back((int32_t) dst_.size());
            tok_.push_back((int32_t) (i / k));
        }
    }
    start_.push_back((int32_t) dst_.size());
    // Private pinned buffers survive until this GPU has consumed them. The CPU
    // pool can write other output rows without a cross-device race.
    std::memcpy(h_x_, x, (size_t) n_tok * H * sizeof(float));
    auto* meta = (RemoteMeta*) h_meta_;
    std::memcpy(meta->ptr, ptr_.data(), ptr_.size() * sizeof(ptr_[0]));
    std::memcpy(meta->start, start_.data(), start_.size() * sizeof(start_[0]));
    std::memcpy(meta->dst, dst_.data(), dst_.size() * sizeof(dst_[0]));
    std::memcpy(meta->tok, tok_.data(), tok_.size() * sizeof(tok_[0]));
    meta->count = (int32_t) group_id_.size();
    const bool reduce = remote_opt_ && remote_opt_->active();
    if (reduce) remote_opt_->prepare(*this, meta + 1);
    DeviceScope scope(device_);
    if (!scope.ok) { err = scope.error(device_); return false; }
    groups_ = (int32_t) group_id_.size();
    const cudaStream_t s = stream_;
    const bool staged =
        (zero_copy_ || check(cudaMemcpyAsync(d_x_, h_x_, (size_t) n_tok * H * sizeof(float), cudaMemcpyHostToDevice, s), "copy input", err, device_)) &&
        check(cudaMemcpyAsync(d_meta_, h_meta_, sizeof(RemoteMeta) + (reduce ? remote_opt_->metadata_bytes() : 0),
                              cudaMemcpyHostToDevice, s), "copy group metadata", err, device_);
    if (!staged) return false;
    const auto& lay = strata::kernels::cpu::expert_layout();
    if (lay.native) {
        const auto& fmt = lay.fmt[(size_t) layer];
        const float* xe = zero_copy_ ? z_x_ : d_x_;
        if (fmt.had_gu) {   // APR: Hadamard-folded gate/up take H (s * x)
            strata::kernels::hadamard_rows(xe, d_xr_, n_tok, H, fmt.had_block, d_sx_, s);
            xe = d_xr_;
        }
        strata::kernels::quantize_q8_1_rows(xe, n_tok, H, d_q8_, s);
        auto L = strata::kernels::native_expert_layout(fmt.gu_type, fmt.d_type, fmt.n_embd, fmt.n_ff);
        if (fmt.had_d) { L.had_block = fmt.had_block; L.d_signs = d_sh_; }
        strata::kernels::native_expert_grouped(L, d_ptr_, d_start_, d_count_, d_dst_, d_tok_,
                                               groups_, (int64_t) dst_.size(), d_q8_, d_scratch_, zero_copy_ && !reduce ? z_out_ : d_out_, s);
    } else {
        strata::kernels::quantize_q8_0_scaled(zero_copy_ ? z_x_ : d_x_, d_q8_, d_scales_, n_tok * H, s);
        strata::kernels::moe_grouped_s2(d_ptr_, d_start_, d_count_, d_dst_, d_tok_,
                                        groups_, (int64_t) dst_.size(), d_q8_, d_scales_, d_scratch_, zero_copy_ && !reduce ? z_out_ : d_out_, s);
    }
    const uint64_t compact_bytes = (uint64_t) (reduce ? n_tok : dst_.size()) * H * sizeof(float);
    if (reduce) {
        if (!remote_opt_->reduce(*this, (RemoteMeta*) d_meta_ + 1, err)) return false;
    } else if (!zero_copy_ && !check(cudaMemcpyAsync(h_out_, d_out_, (size_t) compact_bytes, cudaMemcpyDeviceToHost, s),
               "copy results", err, device_)) return false;
    ++launched_layers_;
    returned_bytes_ += compact_bytes;
    full_row_bytes_ += (uint64_t) n * H * sizeof(float);
    return true;
}

bool RemoteExperts::finish(float* out, std::string& err) {
    if (proxy_) return proxy_finish(out, err);
    if (group_id_.empty()) return true;
    DeviceScope scope(device_);
    if (!scope.ok) { err = scope.error(device_); return false; }
    const auto w0 = std::chrono::steady_clock::now();
    // Spin on the stream like PeerExperts::finish instead of the blocking sync: the layer waits for this helper
    // on the CPU pool's critical path, and a blocking sync's wake-up costs more than the helper's small batch
    // takes - on HIP especially, where the CUDA branch's cudaInitDevice(ScheduleSpin) does not exist and the
    // device keeps its default policy.  STRATA_REMOTE_SPIN=0 keeps the blocking sync (the same switch the CUDA
    // branch reads).
#if defined(STRATA_USE_HIP)   // HIP only: the CUDA branch's cudaInitDevice(ScheduleSpin) already makes the sync spin
    static const bool spin = [] {
        const char* v = std::getenv("STRATA_REMOTE_SPIN");
        return !(v != nullptr && v[0] == '0');
    }();
    if (spin) {
        cudaError_t e;
        while ((e = cudaStreamQuery(stream_)) == cudaErrorNotReady) {
        }
        if (!check(e, "finish", err, device_)) return false;
    } else if (!check(cudaStreamSynchronize(stream_), "finish", err, device_)) {
        return false;
    }
#else
    if (!check(cudaStreamSynchronize(stream_), "finish", err, device_)) return false;
#endif
    ms_wait_ += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - w0).count();
    if (remote_opt_ && remote_opt_->active()) { remote_opt_->accumulate(*this); return true; }
    for (size_t i = 0; i < original_row_.size(); ++i)
        std::memcpy(out + (size_t) original_row_[i] * H, h_out_ + i * H, (size_t) H * sizeof(float));
    return true;
}

bool RemoteExperts::copy_cached_blobs(const int32_t* layers, const int32_t* experts, uint8_t* const* dst,
                                      size_t n, uint8_t* hit, std::string& err) {
    std::fill(hit, hit + n, 0);
    if (device_ < 0 || read_stream_ == nullptr || n == 0) return true;
    const auto& lay = strata::kernels::cpu::expert_layout();
    DeviceScope scope(device_);
    if (!scope.ok) { err = scope.error(device_); return false; }
    std::lock_guard<std::mutex> lock(read_mu_);
    bool queued = false;
    for (size_t i = 0; i < n; ++i) {
        if (dst[i] == nullptr) continue;
        const int32_t slot = cache_.slot_of(layers[i], experts[i]);
        if (slot < 0) continue;
        const size_t bytes = (size_t) lay.blob_bytes(layers[i]);
        if (!check(cudaMemcpyAsync(dst[i], cache_.device_slot(slot), bytes, cudaMemcpyDeviceToHost, read_stream_),
                   "cache read", err, device_)) {
            // Nothing may still be writing a caller's buffer when this returns.  If the drain succeeds, the
            // hits queued before the failure are complete and the caller may keep them; if it does not,
            // forget every hit and let the caller refill all of them from its fallback.
            if (cudaStreamSynchronize(read_stream_) != cudaSuccess) std::fill(hit, hit + n, 0);
            return false;
        }
        hit[i] = 1;
        queued = true;
    }
    if (queued && !check(cudaStreamSynchronize(read_stream_), "cache read wait", err, device_)) {
        std::fill(hit, hit + n, 0);   // a failed wait means no copied buffer may be trusted
        return false;
    }
    return true;
}

bool RemotePrefillSource::copy_blob(int64_t layer, int64_t expert, uint8_t* dst) {
    if (dst != nullptr && from_helper(layer, expert)) {
        const int32_t l = (int32_t) layer, e = (int32_t) expert;
        uint8_t* d = dst;
        std::string err;
        for (RemoteExperts* h : helpers_) {
            uint8_t got = 0;
            const bool ok = h->copy_cached_blobs(&l, &e, &d, 1, &got, err);
            if (got) {
                helper_blobs_.fetch_add(1, std::memory_order_relaxed);
                helper_bytes_.fetch_add(strata::kernels::cpu::expert_layout().blob_bytes(layer),
                                        std::memory_order_relaxed);
                return true;
            }
            if (!ok) break;
        }
        warn(err);
    }
    return fallback_.copy_blob(layer, expert, dst);
}

bool RemotePrefillSource::copy_blobs(const int32_t* layers, const int32_t* experts, uint8_t* const* dst, size_t n) {
    if (helpers_.empty() || n == 0) return fallback_.copy_blobs(layers, experts, dst, n);
    thread_local std::vector<int32_t> hl, he;
    thread_local std::vector<uint8_t*> hd;
    thread_local std::vector<size_t> idx;
    thread_local std::vector<uint8_t> got;
    thread_local std::vector<uint8_t> served;
    thread_local std::vector<int32_t> ml, me;
    thread_local std::vector<uint8_t*> md;
    thread_local std::vector<int> which;
    which.assign(n, -1);
    served.assign(n, 0);
    for (size_t i = 0; i < n; ++i) {
        if (dst[i] == nullptr) continue;
        for (size_t k = 0; k < helpers_.size(); ++k)
            if (helpers_[k]->holds(layers[i], experts[i])) { which[i] = (int) k; break; }
    }
    // One stream batch per helper, so several hits pay one wait; the misses go to the file source in one
    // call, which keeps its own batched-read merging for them.
    uint64_t blobs = 0, bytes = 0;
    const auto& lay = strata::kernels::cpu::expert_layout();
    for (size_t k = 0; k < helpers_.size(); ++k) {
        hl.clear(); he.clear(); hd.clear(); idx.clear();
        for (size_t i = 0; i < n; ++i)
            if (which[i] == (int) k) {
                hl.push_back(layers[i]); he.push_back(experts[i]); hd.push_back(dst[i]); idx.push_back(i);
            }
        if (hl.empty()) continue;
        got.assign(hl.size(), 0);
        std::string err;
        if (!helpers_[k]->copy_cached_blobs(hl.data(), he.data(), hd.data(), hl.size(), got.data(), err)) warn(err);
        for (size_t j = 0; j < got.size(); ++j)
            if (got[j]) {
                served[idx[j]] = 1;
                ++blobs;
                bytes += lay.blob_bytes(hl[j]);
            }
    }
    if (blobs != 0) {
        helper_blobs_.fetch_add(blobs, std::memory_order_relaxed);
        helper_bytes_.fetch_add(bytes, std::memory_order_relaxed);
    }
    ml.clear(); me.clear(); md.clear();
    for (size_t i = 0; i < n; ++i)
        if (!served[i]) { ml.push_back(layers[i]); me.push_back(experts[i]); md.push_back(dst[i]); }
    if (ml.empty()) return true;
    return fallback_.copy_blobs(ml.data(), me.data(), md.data(), ml.size());
}

void RemotePrefillSource::warn(const std::string& err) {
    if (err.empty() || warned_.exchange(true)) return;
    std::fprintf(stderr, "prefill: a helper cache read failed (%s); the file source fills that blob instead\n",
                 err.c_str());
}
} // namespace strata::core

#pragma once

#include "strata/core/expert_cache.hpp"
#include "strata/core/expert_source.hpp"
#include "strata/core/cache_spill.hpp"   // kTierGuardB: the single-source tier guard default
#include "strata/core/expert_helper_socket.hpp"   // M2: ExpertHelperPromptPlan + the prompt-tier statuses (CPU-only header)

#include <cuda_runtime.h>

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

namespace strata::core {
class RemoteExpertOpt;
class ExpertHelperClient;

/// W1 (--expert-helper-ram-relief): the in-process notice that a PROXY tier's mirrored residency was
/// released - the channel closed, the helper died, the peer is gone.  A source that dropped the
/// helper's pairs out of the arena re-reads them from the pack files on this, so a mid-run helper
/// loss degrades exactly as a run with no helper does today (the CPU serves the rows from the arena).
/// Only a source with dropped pairs registers one, and it clears its own in its close().  With nothing
/// registered - every run that does not use the flag - the notice costs one empty check on a path
/// that already only runs when a helper channel fails.
void set_helper_residency_release_hook(std::function<void()> fn);

/// A static, profile-filled expert tier on another CUDA device. CUDA0 keeps all
/// dense weights and state; results return through the existing pinned CPU rows.
class RemoteExperts {
public:
    RemoteExperts() = default;
    ~RemoteExperts();
    RemoteExperts(const RemoteExperts&) = delete;
    RemoteExperts& operator=(const RemoteExperts&) = delete;

    /// Initialise the device before the host expert arena registers
    /// tens of GiB of portable mapped memory with CUDA.
    static bool preflight(int device, double& free_gib, std::string& err);
    bool open(int device, int slots, int64_t layers, int64_t experts,
              const std::vector<std::pair<int32_t, int32_t>>& ranked,
              const ExpertCache& primary, ExpertSource& source,
              std::vector<uint8_t>& claimed, std::string& err, bool auto_size = false,
              int64_t guard_b = strata::core::cache_spill::kTierGuardB);

    /// R2 (opt-in, the CUDA-only `strata-expert-helper`): the PROXY opener.  NO device call of any
    /// kind happens in this object - the helper owns all NVIDIA memory out of process, and a HIP
    /// engine process runs this client with its own (ROCm) runtime mapped and no CUDA runtime at all.
    /// The channel is opened ONCE here at startup (connect + handshake + fill report); if the helper
    /// is absent this returns false with a named reason and the caller keeps the local CPU path.
    /// `ranked` are the range's fill-order pairs (rank order, stage-held pairs removed); `claimed`
    /// is the engine's (layers x experts) stage-held bitmap, sent so the helper fills nothing a
    /// stage owns.  The reply's resident set is mirrored: holds()/resident()/gib() answer from it.
    ///
    /// TWO CHANNELS (opt-in `path2`, the engine's --expert-helper-socket2): a SECOND helper socket,
    /// for the PROMPT tier alone.  With it the window tier (kBegin/kRows, and the drafter lane)
    /// keeps channel A - the one that handshakes AND sends the fill - and the prompt tier
    /// (kBeginP/kRowsP) rides channel B, so neither tier queues behind the other's in-flight request
    /// (the measured head-of-line cost: a draft lane on this socket cost -19.1% decode throughput;
    /// during a 78k prefill a bounded draft wait timed out twice).  Channel B's own hello carries the
    /// same geometry and the same caps - a caps mismatch is the client's named hello refusal, and
    /// then the prompt tier stays on channel A with a printed note, never a failed open.  Each
    /// channel latches INDEPENDENTLY: B dying falls the prompt tier back to local recompute while
    /// the window tier keeps serving (and the other way round).  `path2` empty = one channel,
    /// today's behaviour, byte for byte.
    bool open_socket(const std::string& path, int64_t slots, int64_t layers, int64_t experts,
                     int64_t n_embd, int64_t n_ff, int64_t layer_lo, int64_t layer_hi,
                     int64_t reserve_mib, int64_t timeout_ms,
                     const std::vector<std::pair<int32_t, int32_t>>& ranked,
                     const std::vector<uint8_t>& claimed, std::string& err,
                     const std::string& path2 = std::string());
    bool is_proxy() const { return proxy_; }
    /// The decode loop's per-request boundary: re-arms a tier that disabled itself after a helper
    /// failure inside the previous request (never mid-request: a failed tier stays out for the
    /// rest of that request).  Returns true when the helper is usable for the coming request.
    bool note_request_start();
    /// The dispatch's mid-request rule: a tier whose finish failed (or whose begin send failed) is
    /// disabled for the REST of the request; the dispatch recomputes exactly the claimed rows on the
    /// CPU and calls this.  Re-armed at the next request's note_request_start().
    void note_failure() { if (proxy_) disabled_ = true; }
    void note_fallback();   ///< counts one CPU fallback for the DONE line (the client's stat)
    bool helper_usable() const { return proxy_ && healthy_ && !disabled_; }
    /// The RESIDENCY question, not the dispatch question (`helper_usable`).  A proxy disabled for the
    /// rest of a request (`note_failure`) keeps its helper's VRAM: the helper process is alive, its
    /// slots are allocated, and promoting a pair it owns into the primary's cache would sit in BOTH
    /// caches.  The mirrored fill report describes real residency while the channel is OPEN
    /// (healthy_): residency is released only by a closed channel (`close()`, a channel failure -
    /// begin claims zero rows, the peer is gone, its VRAM is freed), the permanent loss the adaptive
    /// gate must be able to recover from.  Dispatch skips a disabled tier; ownership does not.
    bool proxy_residency_live() const { return proxy_ && healthy_; }

    // ================= The PROMPT tier's own usability (two-channel split) =================
    // Single channel: the prompt tier shares channel A, so `prompt_usable()` is exactly today's gate
    // (`helper_usable() && prompt_capable()`) and every call site keeps its behaviour.
    // Two channels: the prompt tier lives on channel B with its OWN latch.  B's death falls the
    // prompt tier back to local recompute and leaves the window tier serving; A's death does the
    // reverse.  The tier's channel is resolved ONCE per request (note_request_start) and pinned for
    // the request: prefill stages its rows into the pinned channel's buffer before it sends, so the
    // rows staged and the frame sent always belong to one channel - a channel that dies mid-request
    // makes the send fail by name, and the caller's same-layer fallback runs.
    bool prompt_usable() const {
        if (!proxy_) return false;
        const ExpertHelperClient* ch = prompt_chan();   // the pinned channel (resolved at the boundary)
        if (ch == nullptr) return false;
        if (ch == helper2_) return healthy2_ && !disabled2_;   // the prompt tier's own channel
        return healthy_ && !disabled_ && ch->prompt_rows_supported();
    }
    /// The prompt tier's mid-request latch (its own: the window tier's `note_failure` never touches
    /// it, and a prompt failure on channel B never touches the window tier).
    void note_prompt_failure() {
        if (!proxy_) return;
        if (prompt_chan() == helper2_ && helper2_ != nullptr) disabled2_ = true;
        else disabled_ = true;
    }
    uint64_t helper_requests() const;
    uint64_t helper_rows() const;
    uint64_t helper_bytes() const;
    uint64_t helper_timeouts() const;
    uint64_t helper_fallbacks() const;

    void close();

    /// `kind` is the primary verifier's classification (-1 = CPU candidate),
    /// or null on the one-token path. Entries already served on CUDA0 are excluded.
    bool begin(int64_t layer, const float* x, const int32_t* ids, int64_t n_tok,
               int64_t k, const int32_t* kind, const int32_t* primary_res,
               std::string& err);
    /// `claimed_out`/`n_claimed_out` (R2): the (token,k) entries THIS begin took, so the dispatch
    /// can recompute exactly those rows on the CPU if the helper's finish fails.  In-process tiers
    /// answer through owns(); the proxy records its claims here as it sends them.  Proxy mode honors
    /// `kind`: entries the dispatch served from a Foresight slot (kind 0, not primary-resident) are
    /// masked resident in the copy of `primary_res` the kBegin frame carries, so the helper skips
    /// them and `claimed_out` lists exactly the rows the helper answers.
    bool begin(int64_t layer, const float* x, const int32_t* ids, int64_t n_tok,
               int64_t k, const int32_t* kind, const int32_t* primary_res, std::string& err,
               std::vector<int32_t>& claimed_out);
    /// The (token,k) entries this tier's outstanding begin() took.  In-process tiers answer from
    /// their packed `owned_` bitmap; the proxy answers from its outstanding claims (H9: `owned_`
    /// is never allocated in proxy mode - reading it null-derefed the engine at the dispatch's
    /// kind loop).  Claims are routed indices in ascending order (proxy_begin builds them with an
    /// ascending i loop), so the search is binary; an empty claim list (no window in flight, or a
    /// begin that failed and cleared them) owns nothing.
    bool owns(int64_t index) const {
        if (proxy_) return std::binary_search(proxy_claims_.begin(), proxy_claims_.end(), (int32_t) index);
        return owned_[(size_t) index] != 0;
    }
    /// This helper's cache holds (layer, expert): begin() will take its rows unless the plan gave them away.
    /// Proxy mode answers from the helper's authoritative fill report (mirrored at open_socket): the helper
    /// fills exactly the pairs its report lists, so this is the same statement about a tier the engine
    /// cannot query in the hot path (one channel, one request at a time - a live query would serialise the
    /// decode loop behind the helper's replies).
    bool holds(int64_t layer, int32_t expert) const {
        if (proxy_) return proxy_holds(layer, expert);
        return cache_.slot_of(layer, expert) >= 0;
    }
    bool optimized_decode() const { return remote_opt_ != nullptr; }
    bool finish(float* out, std::string& err);
    int64_t resident() const { return proxy_ ? proxy_resident_ : cache_.resident(); }
    int64_t computed() const { return computed_; }
    int64_t launched_layers() const { return launched_layers_; }
    double gib() const { return proxy_ ? (double) proxy_bytes_ / 1073741824.0 : cache_.gib(); }
    uint64_t returned_bytes() const { return returned_bytes_; }
    uint64_t full_row_bytes() const { return full_row_bytes_; }
    /// host time spent in begin() (staging + launches) and in finish() (waiting for this GPU), cumulative
    double ms_begin() const { return proxy_ ? proxy_ms_begin_ : ms_begin_; }
    double ms_wait() const { return proxy_ ? proxy_ms_wait_ : ms_wait_; }

    /// STRATA_PREFILL_HELPER_SOURCE (opt-in): copy up to `n` cached blobs into the host buffers `dst[i]`
    /// on this helper's own stream and wait.  `hit[i]` is set where this helper held (layer, expert); the
    /// bytes are the ones open() filled from the same source, so the prompt path gets identical bytes.
    /// Misses are left to the caller.  False with `err` on a CUDA failure; the hits copied so far are
    /// complete (the stream is drained), so the caller falls back for the rest.  Safe from several threads
    /// (one stream, one lock): the prompt path's stager is threaded.
    bool copy_cached_blobs(const int32_t* layers, const int32_t* experts, uint8_t* const* dst, size_t n,
                           uint8_t* hit, std::string& err);

    // ================= M2 (worker prefill dispatch, docs/work/MIXED_BACKEND_RPC_PLAN.md): the prompt tier's
    // PROXY passthroughs =================
    /// The engine-side prefill claims whole experts the helper holds and takes their rows back over
    /// this channel (the P1 contract: begin_prompt/finish_prompt).  The proxy owns the client;
    /// prefill never touches it directly.  In-process tiers answer false - the prompt tier is the
    /// out-of-process helper's alone - and the buffer call is a named refusal, never device work.
    /// With the second channel (path2) live these run on channel B; see prompt_usable() above, whose
    /// channel the tier resolves per request (channel B while it is open, the primary as its fallback).
    bool prompt_capable() const {
        if (!proxy_) return false;
        const ExpertHelperClient* ch = prompt_chan();
        return ch != nullptr && ch->prompt_rows_supported();
    }
    /// The tier's OWN pinned activation buffer (expert_helper_socket.hpp: the D2H target, never cpu_x).
    /// With two channels BOTH clients map one (same geometry, so they map and fail together): the
    /// tier's channel is resolved once per request, and the buffer the dispatch stages into is always
    /// the buffer of the channel that will send.  Returns the client's named status.
    int prompt_buffer_ensure(int64_t n_embd) {
        if (!proxy_ || helper_ == nullptr) return expert_helper::kErrUnhealthy;
        int rc = helper_->prompt_buffer_ensure(n_embd);
        if (helper2_ != nullptr) {
            const int rc2 = helper2_->prompt_buffer_ensure(n_embd);
            if (rc == expert_helper::kOk) rc = rc2;
        }
        return rc;
    }
    /// The buffer itself (row r = plan.tokens[r] - the client's caller-fills contract,
    /// expert_helper_socket.hpp:146-149): the dispatch's D2H target.  Null when the tier has none.
    /// The const_cast at the copy site is that contract's designated-writer rule, noted there.
    const float* prompt_buffer_data() const {
        const ExpertHelperClient* c = prompt_chan();
        return c != nullptr ? c->prompt_buffer() : nullptr;
    }
    size_t prompt_buffer_bytes() const {
        const ExpertHelperClient* c = prompt_chan();
        return c != nullptr ? c->prompt_buffer_bytes() : 0;
    }
    /// The image of the LAST frame built on the tier's channel: its byte size (what the wire
    /// carried, so the engine's wire arithmetic is right for every format) and its format - 0 the
    /// engine's q8_1 blocks, 1 the fp32 rows, 2 the Q8_K blocks.  0 / -1 with no channel (an
    /// in-process tier) or before the tier's first frame.
    int64_t prompt_last_image_bytes() const {
        const ExpertHelperClient* c = prompt_chan();
        return c != nullptr ? c->prompt_last_image_bytes() : 0;
    }
    int prompt_last_image_format() const {
        const ExpertHelperClient* c = prompt_chan();
        return c != nullptr ? c->prompt_last_image_format() : -1;
    }
    /// The prompt tier's CLAIM BATCH: the engine's STRATA_CLAIM_BATCH (1 = today's wire exactly, one
    /// kBeginP per claim) and the peer's kFeaturePromptBatch bit, read from the tier's channel so the
    /// claim loop's gate and the transport agree.  0 for an in-process tier (no channel, no batch).
    int64_t prompt_claim_batch() const {
        if (!proxy_) return 0;
        const ExpertHelperClient* ch = prompt_chan();
        return ch != nullptr && ch->prompt_batch_supported() ? ch->claim_batch() : 1;
    }
    /// Send one layer's selected-token rows.  A send failure latches the tier out for the rest of
    /// the request (the decode rule, proxy_begin) and returns the named status; the caller's
    /// fallback is same-layer local recompute of the plan's rows.  An UNUSABLE tier refuses by name
    /// (kErrUnhealthy), never "claims zero and succeeds": the dispatch stages real rows into the
    /// tier's buffer BEFORE the send, so the same fallback arm must run - the claim gate checks
    /// usability first, so this only fires on a mid-request latch.
    int begin_prompt(int32_t layer, const ExpertHelperPromptPlan& plan, const int32_t* host_res,
                     int64_t deadline_ms);
    /// Read the layer's rows and scatter them at the pair indices.  A violation applies NOTHING and
    /// latches the tier; the caller recomputes EXACTLY the claimed rows locally (the tiled fallback).
    int finish_prompt(float* out, int64_t n_embd, const std::vector<int32_t>& claims,
                      int64_t timeout_ms, int32_t& rows);
    /// The prompt tier's own counters, kept SEPARATE from the decode window's (row-conservation
    /// evidence: a decode counter must not be able to mask a prompt-row deficit).  Two channels:
    /// both serve the same tier's rows, so the evidence sums the two channels.
    uint64_t prompt_requests() const {
        return (proxy_ && helper_ ? helper_->stat_prompt_requests() : 0) +
               (proxy_ && helper2_ ? helper2_->stat_prompt_requests() : 0);
    }
    uint64_t prompt_rows() const {
        return (proxy_ && helper_ ? helper_->stat_prompt_rows() : 0) +
               (proxy_ && helper2_ ? helper2_->stat_prompt_rows() : 0);
    }

    // ================= M7 (the drafter lane, docs/work/MIXED_BACKEND_RPC_PLAN.md sect12/12a): the
    // remote drafter's passthroughs =================
    //
    // The engine's serve path drafts on the helper: the rows the local DFlashDrafter would have
    // received go over the SAME channel as the window tier's frames, and the block comes back.
    // These forward to the client and change NOTHING about the window tier: a draft failure
    // latches DRAFTING off for the rest of the request (plan sect12 - drafts are speculative, the
    // verify path alone stays correct); it never disables the window tier and never fails the
    // request.  In-process tiers answer a named refusal - the drafter lane is the out-of-process
    // helper's alone.

    /// True when this proxy's helper ADVERTISED the drafter lane (loaded --dflash, kFeatureDraft).
    /// The engine's gate: STRATA_DRAFT_HELPER=1 AND this true AND has_dflash.
    bool draft_capable() const { return proxy_ && helper_ != nullptr && helper_->draft_supported(); }
    int draft_set_geometry(int64_t n_planes, int64_t taps_hidden) {
        return proxy_ && helper_ ? helper_->draft_set_geometry(n_planes, taps_hidden)
                                 : expert_helper::kErrUnhealthy;
    }
    /// Feed the helper's drafter ring (kDraftCtx frames, sub-chunked; rows are wire row-major -
    /// the caller transposed them from the verifier's tap-major taps).  Named statuses only,
    /// never a crash, never a hang: the client's bounds are checked before any write.
    int draft_context(const float* rows, int64_t n_rows, int64_t pos0, int reset);
    /// One verify round (one kDraftReq: the n_new committed rows AND the anchor; the helper runs
    /// add_context_f32 + propose back-to-back).  `out_picks`/`out_probs` are the caller's
    /// draft-block buffers and `out_cap` is their capacity in entries: a reply with more picks
    /// than the caller can hold is refused by name with NOTHING written (drafting then stays off
    /// for the request - never an overflow).  `n_out` is the reply's pick count.
    int draft_round(const float* rows, int64_t n_new, int64_t pos0, int32_t anchor,
                    int32_t* out_picks, float* out_probs, int32_t out_cap, int32_t& n_out);
    /// The drafter lane's counters (the M7 measurement: frames, rows, rounds per window).
    uint64_t draft_frames() const { return proxy_ && helper_ ? helper_->stat_draft_frames() : 0; }
    uint64_t draft_rounds() const { return proxy_ && helper_ ? helper_->stat_draft_rounds() : 0; }
    uint64_t draft_rows() const { return proxy_ && helper_ ? helper_->stat_draft_rows() : 0; }

private:
    friend class RemoteExpertOpt;
    /// R2 proxy: send one window and take its rows (the client's I/O is CPU-only; no device call).
    /// `kind` is the dispatch's classification of this window's entries; its kind-0 entries whose
    /// expert is not primary-resident were served from a Foresight slot and are masked out of the
    /// kBegin frame's residency bitmap (see begin above).
    bool proxy_begin(int64_t layer, const float* x, const int32_t* ids, int64_t n_tok, int64_t k,
                     const int32_t* kind, const int32_t* primary_res, std::string& err, std::vector<int32_t>& claimed_out);
    bool proxy_finish(float* out, std::string& err);
    /// The channel the PROMPT tier serves on right now: channel B when the split is live and pinned
    /// for this request (see prompt_usable()), channel A - today's single channel - otherwise.  The
    /// pin (`prompt_chan2_`) is resolved at the request boundary and never moves mid-request, so the
    /// rows the dispatch stages and the frame it sends are always on one channel.
    const ExpertHelperClient* prompt_chan() const {
        if (!proxy_) return nullptr;
        return (helper2_ != nullptr && prompt_chan2_) ? helper2_ : helper_;
    }
    ExpertHelperClient* prompt_chan() {
        if (!proxy_) return nullptr;
        return (helper2_ != nullptr && prompt_chan2_) ? helper2_ : helper_;
    }
    /// the mirrored fill report: bit (layer*experts + expert) of `proxy_resident_bits_`
    bool proxy_holds(int64_t layer, int32_t expert) const {
        if (!proxy_ || layer < 0 || expert < 0 || expert >= proxy_experts_) return false;
        const size_t i = (size_t) layer * (size_t) proxy_experts_ + (size_t) expert;
        return i < proxy_resident_bits_.size() && proxy_resident_bits_[i] != 0;
    }

    RemoteExpertOpt* remote_opt_ = nullptr;
    int device_ = -1;
    ExpertHelperClient* helper_ = nullptr;   ///< R2 proxy mode (null on every in-process tier)
    /// The PROMPT tier's second channel (opt-in --expert-helper-socket2): its own client, its own
    /// latch, its own handshake.  Null = one channel, and every prompt call runs on helper_ exactly
    /// as it always did.  It never sends kFill: the fill (and so the mirrored resident set) belongs
    /// to channel A, and both channels serve rows out of that one filled state.
    ExpertHelperClient* helper2_ = nullptr;
    bool proxy_ = false;
    bool healthy_ = false;                   ///< false after a channel failure: begin claims ZERO rows
    bool disabled_ = false;                  ///< a failed tier stays out for the rest of the request
    bool healthy2_ = false;                  ///< channel B (the prompt tier) is open
    bool disabled2_ = false;                 ///< channel B's own mid-request latch
    bool prompt_chan2_ = false;              ///< this request's prompt tier rides channel B
    int64_t proxy_layers_ = 0, proxy_experts_ = 0, proxy_n_embd_ = 0;
    int64_t proxy_lo_ = 0, proxy_hi_ = -1;
    int64_t proxy_timeout_ms_ = 20;
    // H12/r15: the DONE-line helper= counters survive the channel's close (the stats lived -
    // and died - inside the client object; a closed tier printed reqs=0 rows=0)
    uint64_t proxy_stat_reqs_ = 0, proxy_stat_rows_ = 0, proxy_stat_bytes_ = 0,
             proxy_stat_timeouts_ = 0, proxy_stat_fallbacks_ = 0;
    /// The same evidence for the PROMPT tier (the row-conservation line reads these).
    uint64_t proxy_stat_prompt_reqs_ = 0, proxy_stat_prompt_rows_ = 0;
    int64_t proxy_connect_ms_ = 2000;
    int64_t proxy_slots_ = 0;
    int64_t proxy_reserve_mib_ = 512;
    std::string proxy_path_;
    std::string proxy_path2_;   ///< the prompt tier's second channel (empty: single channel)
    std::vector<uint8_t> proxy_resident_bits_;  ///< the mirrored fill report, (layers x experts) bytes of 0/1
    int64_t proxy_resident_ = 0;
    int64_t proxy_bytes_ = 0;
    /// The (token,k) entry indices the OUTSTANDING begin claimed, in the order the helper must
    /// return its dense rows (group order, as the in-process tier packs `dst`): kRows row j is
    /// claims[j], so the client scatters them at their original indices.
    std::vector<int32_t> proxy_claims_;
    double proxy_ms_begin_ = 0, proxy_ms_wait_ = 0;
    int64_t n_expert_ = 0;
    int32_t groups_ = 0;
    int64_t computed_ = 0;
    int64_t launched_layers_ = 0;
    uint64_t returned_bytes_ = 0;
    uint64_t full_row_bytes_ = 0;
    double ms_begin_ = 0, ms_wait_ = 0;
    ExpertCache cache_;
    cudaStream_t stream_ = nullptr;
    cudaStream_t read_stream_ = nullptr;   ///< STRATA_PREFILL_HELPER_SOURCE: cache -> host copies
    std::mutex read_mu_;                   ///< one cache reader at a time (the stager is threaded)
    float* h_x_ = nullptr;
    float* h_out_ = nullptr;
    void* h_meta_ = nullptr;
    float* d_x_ = nullptr;
    float* z_x_ = nullptr;     ///< h_x_ as the helper GPU sees it (zero-copy: no input copy per layer)
    float* z_out_ = nullptr;   ///< h_out_ as the helper GPU sees it (zero-copy: no result copy)
    bool zero_copy_ = false;
    float* d_out_ = nullptr;
    uint8_t* d_q8_ = nullptr;
    float* d_scales_ = nullptr;
    float* d_xr_ = nullptr;                 // APR: the Hadamard-rotated expert input (CAP x H)
    const float* d_sx_ = nullptr;           // APR: device sign vectors (x: H, h: FF); nullptr = identity
    const float* d_sh_ = nullptr;
    void* d_scratch_ = nullptr;
    void* d_meta_ = nullptr;  ///< one contiguous upload of grouped indices, instead of five small copies
    int32_t* d_start_ = nullptr;
    int32_t* d_dst_ = nullptr;
    int32_t* d_tok_ = nullptr;
    int32_t* d_count_ = nullptr;
    unsigned long long* d_ptr_ = nullptr;
    std::vector<uint8_t> owned_;
    std::vector<uint8_t> layers_present_;
    std::vector<int32_t> group_of_, group_id_;
    std::vector<int32_t> start_, dst_, tok_, original_row_;
    std::vector<unsigned long long> ptr_;
};

/// STRATA_PREFILL_HELPER_SOURCE (opt-in): the prompt path's view of the expert tiers, where a blob a helper
/// GPU already holds in its VRAM cache is copied from that cache instead of being read from the files again.
/// Everything else is forwarded to `fallback` unchanged, so its batched unbuffered reads, its pinned RAM copy
/// and the prompt path's own decisions keep their meaning.  Nothing is cached here: every call asks the
/// helpers, so an entry an adaptive tier moves between requests is a miss (a file read), never a wrong blob.
///
/// The serve loop builds this only for a serial request path (no batch slots, no layer split), while the
/// helpers are idle: during a prompt read the helper GPUs compute nothing, so their caches can be read
/// without ordering the copies against their kernel streams.
class RemotePrefillSource final : public ExpertSource {
public:
    RemotePrefillSource(ExpertSource& fallback, const std::vector<RemoteExperts*>& helpers)
        : fallback_(fallback), helpers_(helpers) {}

    /// Whether any helper's VRAM cache holds (layer, expert) right now.
    bool from_helper(int64_t layer, int64_t expert) const {
        for (const RemoteExperts* h : helpers_) if (h->holds(layer, (int32_t) expert)) return true;
        return false;
    }
    /// Blobs / bytes this source has taken from the helpers so far (the per-request evidence line).
    uint64_t helper_blobs() const { return helper_blobs_.load(std::memory_order_relaxed); }
    uint64_t helper_bytes() const { return helper_bytes_.load(std::memory_order_relaxed); }

    const uint8_t* blob(int64_t layer, int64_t expert) override { return fallback_.blob(layer, expert); }
    const uint8_t* blob_stable(int64_t layer, int64_t expert) override {
        return fallback_.blob_stable(layer, expert);
    }
    int blob_parts(int64_t layer, int64_t expert, BlobPart* out) override {
        return from_helper(layer, expert) ? 0 : fallback_.blob_parts(layer, expert, out);
    }
    int64_t reads() const override { return fallback_.reads(); }
    bool pinned(int64_t layer, int64_t expert) const override {
        return from_helper(layer, expert) ? false : fallback_.pinned(layer, expert);
    }
    void begin_layer(int64_t layer, const int32_t* ids, int64_t k) override {
        fallback_.begin_layer(layer, ids, k);
    }
    const uint8_t* device_alias(int64_t layer, int64_t expert) const override {
        return from_helper(layer, expert) ? nullptr : fallback_.device_alias(layer, expert);
    }
    void prefetch(int64_t layer, int64_t expert) override { fallback_.prefetch(layer, expert); }
    uint64_t release(int64_t layer, int64_t expert) override {
        return from_helper(layer, expert) ? 0 : fallback_.release(layer, expert);
    }
    bool pcie_layer(int64_t layer) const override { return fallback_.pcie_layer(layer); }
    /// True where a helper holds the blob: the prompt path's stager must then take the copy_blob(s) route,
    /// which is where the cache-to-host copy happens.  Everywhere else the fallback answers as it always did.
    bool transient(int64_t layer, int64_t expert) const override {
        return from_helper(layer, expert) ? true : fallback_.transient(layer, expert);
    }
    double cached_share(int64_t samples) const override { return fallback_.cached_share(samples); }
    bool copy_blob(int64_t layer, int64_t expert, uint8_t* dst) override;
    bool copy_blobs(const int32_t* layers, const int32_t* experts, uint8_t* const* dst, size_t n) override;
    void prefetch(int64_t layer, const int64_t* experts, int64_t n) override {
        fallback_.prefetch(layer, experts, n);
    }
    void warm(int64_t layer, const int64_t* experts, int64_t n) override {
        fallback_.warm(layer, experts, n);
    }
    bool warms() const override { return fallback_.warms(); }
    bool advise_pairs(const std::pair<int32_t, int32_t>* pairs, int64_t n) const override {
        return fallback_.advise_pairs(pairs, n);
    }

private:
    void warn(const std::string& err);
    ExpertSource& fallback_;
    std::vector<RemoteExperts*> helpers_;
    std::atomic<uint64_t> helper_blobs_{0}, helper_bytes_{0};
    std::atomic<bool> warned_{false};
};
} // namespace strata::core

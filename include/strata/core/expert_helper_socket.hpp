// include/strata/core/expert_helper_socket.hpp - the engine's expert-helper socket client (R2).
//
// CPU-only by construction (no CUDA/HIP headers, no GPU symbols): the frames and their bounded
// poll-based I/O live in tools/mixed_gpu/sidecar/helper_frame.cpp, shared verbatim with the CUDA
// helper.  RemoteExperts owns one of these in proxy mode; the client itself makes no device call, so
// a HIP engine process runs the client while the helper owns all NVIDIA memory out of process.
//
// One outstanding request, one connection.  The engine opens the channel ONCE at startup (the helper
// is started manually, before the engine); if it is absent the engine warns and keeps the local CPU
// path for the whole run (helper-restart = engine-restart, documented).  Within a request the tier
// disables itself at the first begin-send failure or finish failure and re-arms at the next request.
#pragma once

#include "strata/core/expert_helper_protocol.hpp"

#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

namespace strata::core {

namespace expert_helper {
class Socket;   // tools/mixed_gpu/sidecar/helper_frame.cpp (AF_UNIX; socketpair works for tests)
}

/// R5 P1 (the selected-token PROMPT tier, r5-three-gpu-design.md §2): ONE layer's claim set, as the
/// selection rule defines it and as the kBeginP frame carries it.  P1 provides the library - the
/// selection, the packing, the reply's validation and the tier's own pinned activation buffer; P2
/// wires the dispatch decision that fills it.  Nothing here decides WHEN the tier is used.
struct ExpertHelperPromptPlan {
    std::vector<int32_t> pair_token;   // the claimed rows: the token of each pair (the layer's tokens)
    std::vector<int32_t> pair_expert;  // ..and its expert (parallel arrays, one entry per claimed row)
    std::vector<int32_t> tokens;       // the distinct selected tokens, ASCENDING: the q8 image's row order
    int64_t rows() const { return (int64_t) pair_token.size(); }
    int64_t n_sel_tok() const { return (int64_t) tokens.size(); }
    /// The q8 image row a token occupies (the frame's pair list indexes the image, not the layer).
    /// -1 when the token is not in the plan.  The tokens are ascending, so this is a binary search.
    int image_row(int32_t token) const {
        int lo = 0, hi = (int) tokens.size() - 1;
        while (lo <= hi) {
            const int mid = (lo + hi) / 2;
            if (tokens[(size_t) mid] == token) return mid;
            if (tokens[(size_t) mid] < token) lo = mid + 1; else hi = mid - 1;
        }
        return -1;
    }
    void clear() { pair_token.clear(); pair_expert.clear(); tokens.clear(); }
};

/// The design's claim rule, isolated as a pure function so the host test pins the exact arithmetic
/// (and P2 owns the dispatch call site): for ONE layer,
///   cnt[e] > 0  ∧  host_res[l*n_expert + e] < 0 (primary-NON-resident)  ∧  holds[e] (the proxy
///   serves it)  ∧  stable[e] (the CPU path's own blob_stable rule - what makes same-layer fallback
///   always possible)  ∧  cnt[e] <= kMaxRowsPerExpert  ∧  Σ cnt[e] <= kMaxRowsPerLayer  ∧  the union
///   of the claimed tokens <= kMaxSelTok.
/// `toks` is the per-expert token lists concatenated in EXPERT order (expert e owns
/// toks[sum(cnt[0..e-1]) .. +cnt[e]) - the shape the CPU share builds, prefill.cpp:3898-3931).
/// Deterministic and reproducible: experts are considered by cnt DESC, ties by expert ASC (the CPU
/// path's sorted-by-count shape), an expert is claimed WHOLE (all of its tokens) or not at all, and
/// an expert whose claim would breach a cap is skipped - the scan continues, so the result depends
/// on the input only, never on order of arrival.  Returns kOk with a (possibly empty) plan, or a
/// named geometry refusal when the INPUT itself is outside the contract (bad k, a negative count,
/// more pairs than the frame can carry).  An empty plan is a success: the layer stays fully local.
int select_prompt_rows(const int32_t* cnt, const int32_t* toks, const int32_t* host_res, int64_t layer,
                       const uint8_t* holds, const uint8_t* stable, int64_t n_expert, int64_t k,
                       int64_t max_rows_per_expert, int64_t max_rows_per_layer, int64_t max_sel_tok,
                       ExpertHelperPromptPlan& out);

class ExpertHelperClient {
public:
    ExpertHelperClient();
    ~ExpertHelperClient();
    ExpertHelperClient(const ExpertHelperClient&) = delete;
    ExpertHelperClient& operator=(const ExpertHelperClient&) = delete;

    /// Startup, handshake and fill deadlines here are RELATIVE milliseconds ("from now"); the
    /// client converts with deadline_after before touching the socket.  `deadline_ms <= 0` means
    /// this call's own default (hello/begin: 3 s, the fill reply: 600 s).  `finish`'s timeout_ms
    /// is likewise relative (--expert-helper-timeout-ms is read as ms from the window's start).
    /// Attach an already-connected socket (tests drive the client over a socketpair; the engine
    /// connects the helper's bound path).  `owned` closes the socket with the client.
    int attach_socket(int fd, bool owned);
    int connect(const std::string& path, int64_t connect_timeout_ms);
    bool open() const { return fd_ >= 0; }
    void close();

    /// The startup handshake.  `claimed` is the (layers x n_expert) stage-held bitmap (0/1 bytes);
    /// the helper fills nothing a stage owns.  Both exchanges are deadline-bound.
    int hello(int64_t n_layers, int64_t n_expert, int64_t n_embd, int64_t n_ff,
              int64_t layer_lo, int64_t layer_hi, int64_t slots, int64_t reserve_mib,
              const std::vector<uint8_t>& claimed, int64_t deadline_ms, int64_t& free_mib);

    /// Send the ranked fill order and take the helper's authoritative resident set.  The reply's
    /// pairs are exactly what holds()/resident()/gib() mirror for the rest of the run.
    int fill_report(const std::vector<std::pair<int32_t, int32_t>>& pairs, int64_t deadline_ms,
                    std::vector<std::pair<int32_t, int32_t>>& resident_pairs, int64_t& bytes);

    /// Send one window's activations (q8_1, n_tok*(n_embd/32)*36 bytes) + routed ids + the primary
    /// residency bitmap.  Returns after the send: the helper computes under the CPU pool's work.
    int begin(int32_t layer, int32_t n_tok, const int32_t* ids, const int32_t* primary_res,
              const uint8_t* q8, size_t q8_bytes, int64_t deadline_ms);

    /// kBeginQ8K (default OFF - STRATA_EXPERT_HELPER_Q8K, a peer that advertised kFeatureQ8K):
    /// `begin`'s window request with the activation image as ggml's Q8_K blocks ((n_embd/256)*292
    /// bytes per token) - the CPU reference's own gate/up image, quantized by the CALLER with
    /// cpu::native_quant_act so the bytes are exactly the ones the CPU-offloaded rows of this window
    /// run.  Refused locally (kErrRequest, nothing written) unless the peer advertised the bit: the
    /// frame type never reaches a peer that cannot type-decode it.  finish() reads the reply, the
    /// same kRows contract as begin's.
    int begin_q8k(int32_t layer, int32_t n_tok, const int32_t* ids, const int32_t* primary_res,
                  const uint8_t* q8k, size_t q8k_bytes, int64_t deadline_ms);

    /// Read the helper's rows for the outstanding begin and scatter them at the original indices.
    /// Validates: frame type/version, request id, sequence, payload length vs n_embd, index range vs
    /// `claims`, no duplicate index, every claimed index answered, all floats finite.  A validated
    /// reply is applied whole; any violation applies nothing and leaves the channel closed by the
    /// caller's rule (the dispatch recomputes the claimed rows on the CPU for this request).
    int finish(float* out, int64_t n_embd, int32_t n_tok, int64_t k, const std::vector<int32_t>& claims,
               int64_t timeout_ms, int32_t& rows);

    // ================= R5 P1: the selected-token PROMPT tier (kBeginP / kRowsP) =================
    //
    // The library P2's dispatch will call.  P1 provides it BOUNDED and gated; P1 wires NO dispatch
    // decision and NO engine call site (src/prefill/prefill.cpp and src/program/generate.cpp are
    // untouched by this lane), so the default path cannot reach any of it.

    /// True when the peer ADVERTISED the tier at hello (the ack carried the feature bit).  False for
    /// an R2 helper (its 24-byte legacy ack has no features field): the tier stays off, and
    /// begin_prompt refuses with a named status WITHOUT sending anything - the new frames never
    /// reach a peer that cannot type-decode them, so the old-peer refusal is the negotiation, not a
    /// race against a misparse.
    bool prompt_rows_supported() const { return prompt_rows_; }
    bool prompt_f32_supported() const { return prompt_f32_; }   // M4c (b): the peer quantizes fp32 itself
    bool q8k_supported() const { return q8k_; }   // the peer takes Q8_K activation images (kFeatureQ8K)
    /// The image of the LAST frame build_beginp_payload built (the claim line reports it): its byte
    /// size and its format - 0 the engine's q8_1 blocks, 1 the fp32 rows, 2 the Q8_K blocks.  The
    /// byte size is what the wire carried, so the engine's wire arithmetic is right for every
    /// format, not only for fp32.  Zero/negative before the first prompt-tier frame.
    int64_t prompt_last_image_bytes() const { return last_img_bytes_; }
    int prompt_last_image_format() const { return last_img_fmt_; }
    /// True when the peer ADVERTISED the claim batch (kFeaturePromptBatch: it serves kBeginPB/kRowsPB).
    /// A peer without the bit never sees the frame type: begin_prompt keeps the kBeginP path.
    bool prompt_batch_supported() const { return prompt_batch_; }
    /// The engine's claim batch: STRATA_CLAIM_BATCH clamped to 1..kMaxClaimBatch, DEFAULT 1.
    /// 1 is EXACTLY today's wire - one kBeginP per claim, one kRowsP per reply, no new frame type
    /// on the channel at all.  K>1 coalesces K consecutive begin_prompt calls into ONE kBeginPB and
    /// answers them with ONE kRowsPB (transport-only: same rows, same order of application).
    int64_t claim_batch() const { return claim_batch_; }

    /// The tier's OWN pinned host activation buffer (`ext_x` in the design): the tier owns it,
    /// allocates it ONCE, and it never exceeds kMaxSelTok * n_embd * 4 (2.5 MB at the pack's N=2560).
    /// It is NEVER the engine's `cpu_x` - that buffer does not exist on the canonical stream_all
    /// path (prefill.cpp:3778/3810-3824) and this buffer has no relationship to it: the test pins the
    /// ownership (every write the tier reads lands inside its own mapping).  Pinned = page-locked host
    /// memory (mmap + mlock, Linux); the device D2H copy into it is P2's call site, not this file's
    /// (CPU-only by construction, like the rest of the client).
    /// Returns kOk, kErrGeometry when n_embd is unusable, kErrInternal when the mapping/lock fails -
    /// the design's policy is then: tier disabled for the request with a printed notice, full local
    /// compute.  `release_prompt_buffer` frees it (the tier's own lifetime, never a borrowed page).
    int prompt_buffer_ensure(int64_t n_embd);
    void release_prompt_buffer();
    const float* prompt_buffer() const { return (const float*) ext_x_; }
    size_t prompt_buffer_bytes() const { return ext_x_bytes_; }
    int64_t prompt_buffer_embd() const { return ext_x_embd_; }
    /// Ownership test: is `p` one of THIS tier's rows (the buffer's own mapping, row-aligned)?
    bool prompt_buffer_owns(const void* p) const {
        const uintptr_t a = (uintptr_t) p, b = (uintptr_t) ext_x_;
        return ext_x_ != nullptr && p != nullptr && a >= b && a < b + ext_x_bytes_ &&
               ((a - b) % sizeof(float)) == 0;
    }

    /// Send ONE layer's selected token rows: the (token, expert) pairs of `plan`, the layer's PRIMARY
    /// residency bitmap (host_res, slot >= 0 = primary-resident - the helper claims none of those),
    /// and the selected tokens' activations, which the caller filled into prompt_buffer() in the
    /// plan's token order (row r = plan.tokens[r]).  The q8 image is built here by quantize_q8_1_cpu,
    /// so the frame's bytes are the same quantizer the decode contract pins.  Hard refusals BEFORE
    /// any write, all with this transport's named statuses: the peer never advertised the tier, the
    /// layer is outside the hello window, rows > kMaxRowsPerLayer, per-expert rows >
    /// kMaxRowsPerExpert, distinct tokens > kMaxSelTok, an image larger than kMaxSelTok rows, a
    /// payload over kMaxFramePayload, a buffer that does not cover the plan.  MSG_NOSIGNAL semantics
    /// are the transport's (sendmsg), unchanged.  Preserves the one-outstanding-request invariant: the
    /// layer's reply must be read by finish_prompt before any further request.
    ///
    /// The image's FORMAT is chosen by env + peer bit only: the fp32 rows (image_format 1) with a
    /// peer that advertised kFeaturePromptF32 - the default since M4c (b); ggml's Q8_K blocks
    /// (image_format 2) with STRATA_EXPERT_HELPER_Q8K and kFeatureQ8K; and the engine-quantized
    /// q8_1 blocks (image_format 0, this tier's original wire, `quantize_q8_1_cpu`'s bytes) with
    /// STRATA_HELPER_Q8_IMAGE - the helper uploads those verbatim and quantizes NOTHING on its
    /// device-lock path.  Byte-identity between the engine's quantizer and the helper's twin is what
    /// keeps the outputs fixed; see build_beginp_payload.
    int begin_prompt(int32_t layer, const ExpertHelperPromptPlan& plan, const int32_t* host_res,
                     int64_t deadline_ms);

    /// Read the tier's rows for the outstanding kBeginP and scatter them at the PAIR indices.
    /// `claims` are the pair indices (0..plan.rows()-1) the engine claimed; every claim must be
    /// answered, an index the engine did not claim is refused, duplicates are refused, every float
    /// must be finite.  A validated reply is applied WHOLE; any violation applies NOTHING and closes
    /// the channel - the caller's rule is the same-layer fallback (tiled CPU recompute) for this
    /// request, and the shared healthy_/disabled_ latch (no reconnect exists) for later windows.
    int finish_prompt(float* out, int64_t n_embd, const std::vector<int32_t>& claims,
                      int64_t timeout_ms, int32_t& rows);

    /// The CLAIM BATCH (default OFF, the engine's STRATA_CLAIM_BATCH): when `claim_batch_ > 1` AND the
    /// peer advertised kFeaturePromptBatch, `begin_prompt` builds the part and QUEUES it (nothing is
    /// written).  Two parts or more queued: the first `finish_prompt` flushes them as ONE kBeginPB and
    /// reads its ONE kRowsPB, and the parts are then answered in claim order, each validated against
    /// its OWN claims by the same validator a single kRowsP runs.  EXACTLY ONE part queued (a caller
    /// that finishes before its next begin - the prefill walk's shape): that flush is a plain kBeginP
    /// and a plain kRowsP, so an inert pipeline costs exactly batch=1.  With the env unset (K = 1)
    /// begin_prompt/finish_prompt are exactly today's code path and today's bytes.
    /// The queue's caps are hard and refused BEFORE any write: more than kMaxClaimBatch parts, a
    /// request over kMaxBatchPayload, more reply rows than kMaxBatchPayload covers.  The caller's
    /// per-claim `claims` must match the queued part's claims (the batch's order is the claim order);
    /// a mismatch is kErrSeq and closes the channel - the engine's same-layer fallback owns the rows.

    // ================= M7 (the drafter lane, docs/work/MIXED_BACKEND_RPC_PLAN.md sect12/12a):
    // kDraftCtx / kDraftReq =================
    //
    // The displaced DFlash drafter's engine-side methods, on the SAME channel as the tiers: one
    // draft frame is fully synchronous (send the request, read its own reply), so it never leaves
    // an outstanding request the window tier's begin/finish must not interleave with.  The peer
    // gate is the hello feature bit, the same pattern as prompt_rows_supported: a peer that never
    // advertised kFeatureDraft gets a named refusal and the draft frame types never reach it.
    //
    // ROWS ARE WIRE ROW-MAJOR, exactly as the protocol's structs describe them: one row =
    // n_planes * taps_hidden fp32 planes packed contiguously, plane p at row*n_planes + p.  The
    // verifier's taps are TAP-MAJOR (plane p's row r at taps[p*stride + r*hidden]); the caller
    // transposes (generate.cpp's df_wire_rows), and the helper transposes back
    // (tools/mixed_gpu/sidecar/helper_main.cpp: stage_draft_rows).  Nothing is copied twice here.

    /// True when the peer ADVERTISED the drafter lane at hello (the ack carried kFeatureDraft).
    /// False for every helper that loaded no --dflash: the draft methods refuse by name and no
    /// kDraftCtx/kDraftReq is ever put on the wire.
    bool draft_supported() const { return draft_; }
    bool draft_tap_bf16_supported() const { return draft_bf16_; }   // L8: bf16 taps on the wire
    /// The engine's LOCAL parse of the DFlash artifact, handed down once before any frame: the
    /// geometry every frame carries and every size check runs against (n_planes = the artifact's
    /// target_layers.size(), taps_hidden = the target's n_embd).  A geometry the peer's own
    /// artifact disagrees with is refused by the PEER's geometry check - this call only pins what
    /// this client builds into the frames.  kOk / kErrGeometry.
    int draft_set_geometry(int64_t n_planes, int64_t taps_hidden);

    /// Feed the helper's drafter ring: `n_rows` rows at absolute positions [pos0, pos0+n_rows),
    /// sent as kDraftCtx frames sub-chunked to kMaxDraftCtxRows, each frame answered by its own
    /// kDraftAck (every frame is synchronous, so a mid-request failure leaves the channel exactly
    /// as consistent as a finished window).  `reset` = 1 marks the request's FIRST frame (the
    /// helper empties its ring; valid only at pos0 == 0, the helper's own check).  Hard refusals
    /// BEFORE any write: no channel, no feature bit, no geometry set, n_rows < 1, pos0 < 0, a
    /// reset at a position other than 0, a frame over kMaxFramePayload.  A send/recv failure, a
    /// timeout, or a malformed ack CLOSES the channel (the transport's established rule) and
    /// returns the named status; the caller disables drafting for the rest of the request.  A
    /// TYPED kError reply is the helper's application-level refusal of THIS draft frame: it
    /// returns kErrRequest WITHOUT closing the shared channel (M7 sect12a, BUG B) - the caller
    /// disables drafting for this request and the window/prompt tiers keep using the channel,
    /// which the server keeps serving after the typed kError.
    /// M7 (sect12a, BUG E): every frame first passes the shared-channel gate (gate_draft_frame) -
    /// the pipelined window/prompt tiers may keep one request in flight, and a draft frame written
    /// behind an in-flight kBeginP/kBegin reads back THAT request's reply (the run-2 fault).  The
    /// gate waits up to the call's own bound and then answers kErrBusy: no frame reaches the wire,
    /// the channel stays open, and the caller disables drafting for the rest of the request.
    int draft_context(const float* rows, int64_t n_rows, int64_t pos0, int reset, int64_t timeout_ms);

    /// One verify round: the committed window rows (n_new in 1..kMaxDraftRound at pos0) PLUS the
    /// anchor token, in ONE kDraftReq - the helper runs add_context_f32 and propose back-to-back,
    /// which is the engine's own pairing contract (src/program/generate.cpp:16019: an
    /// unsynchronized add_context_f32 requires an immediate propose) satisfied by construction.
    /// The reply's picks and probabilities land in `out_picks` (and `out_probs`, nullable) - the
    /// caller's buffers must hold kMaxDraftPicks entries; a reply with more picks than `out_cap`
    /// is refused by name with NOTHING written.  `n_out` is the pick count on success.  The same
    /// named-status rules as draft_context.
    int draft_round(const float* rows, int64_t n_new, int64_t pos0, int32_t anchor,
                    int32_t* out_picks, float* out_probs, int32_t out_cap, int64_t timeout_ms,
                    int32_t& n_out);

    /// The drafter lane's own counters, counted SEPARATELY from the window tier's (the M7
    /// measurement reads drafts per request; a window counter must not mask a draft deficit).
    uint64_t stat_draft_frames() const { return draft_frames_; }
    uint64_t stat_draft_rounds() const { return draft_rounds_; }
    uint64_t stat_draft_rows() const { return draft_rows_; }

    /// Orderly close (a bounded best-effort kBye, then shutdown).
    void bye();

    /// Per-request evidence for the serve DONE line: requests, rows/bytes the helper returned,
    /// timeouts and fallbacks.  Cumulative; the driver prints the deltas.
    uint64_t stat_requests() const { return requests_; }
    uint64_t stat_rows() const { return rows_; }
    uint64_t stat_bytes() const { return bytes_; }
    uint64_t stat_timeouts() const { return timeouts_; }
    uint64_t stat_fallbacks() const { return fallbacks_; }
    void note_fallback() { ++fallbacks_; }
    double ms_begin() const { return ms_begin_; }
    double ms_wait() const { return ms_wait_; }
    /// The same evidence for the PROMPT tier, counted SEPARATELY: the design's row-conservation
    /// claim (rows_claimed == rows_returned per layer) is about prompt rows, and a decode-window
    /// counter must not be able to mask a prompt-row deficit.
    uint64_t stat_prompt_requests() const { return prompt_requests_; }
    uint64_t stat_prompt_rows() const { return prompt_rows_returned_; }
    uint64_t stat_prompt_bytes() const { return prompt_bytes_; }

private:
    int fd_ = -1;
    bool owned_ = false;
    uint32_t seq_ = 0;
    uint32_t req_id_ = 0;
    bool begin_outstanding_ = false;
    uint32_t begin_seq_ = 0;
    // M7 (sect12a, BUG E): the drafter lane shares this channel with the pipelined window/prompt
    // tiers, and with a layer split the tiers run on DIFFERENT threads: the chain's last stage
    // fires the prompt tap callback (src/program/generate.cpp:9383 dflash_on_taps) from the
    // successor stage's own thread (src/prefill/prefill.cpp:5360-5369 next_->run_impl on the
    // hand-off worker), while the other stage's thread has a kBeginP written and its reply
    // still unread (begin_prompt at src/core/expert_helper_socket.cpp:546, finish_prompt at :559).
    // The protocol carries ONE outstanding request, and the reply pairing is by req_id: two
    // threads issuing frames on this fd without a lock let a draft frame's ack read consume the
    // PEER thread's reply.  The live run-2 fault is exactly that: "draft ctx reply rejected:
    // req_id 986 (want 987) seq 1 (want 1)".  io_mtx_ serializes every socket transaction; the
    // draft gate below adds the outstanding-request check, so a draft frame is only written when
    // nothing else is in flight (the gate sleeps, never holding the lock).
    std::mutex io_mtx_;
    /// Bounded wait for the shared channel to have no in-flight request, then the caller ships its
    /// draft frame holding io_mtx_.  ON TRUE THE LOCK IS LEFT HELD (the frame's write, its reply's
    /// read and the pairing test all run inside it).  False = the bound expired, the lock is
    /// released and NOTHING was written: the caller answers kErrBusy, the channel stays open.
    bool gate_draft_frame(std::unique_lock<std::mutex>& lk, int64_t deadline_ms);
    int64_t n_expert_ = 0, n_embd_ = 0;
    int64_t layer_lo_ = 0, layer_hi_ = -1;          // the hello window (the tier serves inside it)
    bool prompt_rows_ = false;                      // the peer advertised kFeaturePromptRows
    bool prompt_f32_ = false;                       // the peer advertised kFeaturePromptF32 (M4c (b))
    bool q8k_ = false;                              // the peer advertised kFeatureQ8K (the Q8_K wire)
    bool prompt_batch_ = false;                     // the peer advertised kFeaturePromptBatch
    int64_t claim_batch_ = 1;                       // STRATA_CLAIM_BATCH, clamped: 1 = today's wire
    int64_t last_img_bytes_ = 0;                    // the image bytes of the last frame built (the wire's)
    int last_img_fmt_ = -1;                         // and its format: 0 q8_1, 1 fp32, 2 Q8_K
    /// One queued prompt-tier claim (the batch's part): the full kBeginP payload bytes the client
    /// built, the engine's claims for it, the begin's own write deadline (the one-part flush sends
    /// these bytes as a plain kBeginP and keeps that deadline), and - once a multi-part batch's
    /// kRowsPB is in hand - the part's own kRowsP payload bytes, validated by the same validator a
    /// standalone reply runs through.
    struct PromptPart {
        std::vector<uint8_t> request;
        std::vector<int32_t> claims;
        std::vector<uint8_t> reply;
        int64_t deadline_ms = 0;
    };
    std::vector<PromptPart> prompt_parts_;          // queued claims, then the answered batch in order
    int64_t batch_rows_ = 0;                        // the queued batch's total claimed rows (reply cap)
    bool batch_answered_ = false;                   // the queue's kRowsPB is read; staged parts remain
    /// The first finish_prompt of a MULTI-part queued batch: flush ONE kBeginPB, read its ONE kRowsPB,
    /// split it per part, then answer the front part.  Runs under io_mtx_ (the caller holds it).  A
    /// queue of EXACTLY ONE part never reaches here: finish_prompt sends it as a plain kBeginP and
    /// reads a plain kRowsP, so an inert pipeline costs exactly batch=1.
    int finish_prompt_batch(float* out, int64_t n_embd, const std::vector<int32_t>& claims,
                            int64_t timeout_ms, int32_t& rows);
    /// The single-claim WRITE both transports share: `begin_prompt` sends the payload it just built,
    /// and a one-part flush sends the staged bytes as the same frame.  The caller holds io_mtx_ (the
    /// protocol's one-outstanding-request rule spans the write and the reply's read).
    int write_beginp(const std::vector<uint8_t>& payload, int64_t rows, int64_t deadline_ms);
    /// The prompt tier's request body (BeginPReq + pairs + primary bitmap + the image quantized from
    /// the tier's own rows) with every cap begin_prompt enforces.  One builder for both transports: a
    /// single claim writes these bytes as kBeginP, a batch queues them as one kBeginPB part, so a
    /// batched part is byte-for-byte the request a standalone kBeginP carries.
    int build_beginp_payload(int32_t layer, const ExpertHelperPromptPlan& plan, const int32_t* host_res,
                             std::vector<uint8_t>& payload, int64_t& rows_out);
    bool draft_ = false;                            // the peer advertised kFeatureDraft (M7)
    bool draft_bf16_ = false;                       // the peer advertised kFeatureDraftBf16 (L8 wire v2)
    int64_t draft_planes_ = 0, draft_hidden_ = 0;   // the draft geometry the engine pinned
    uint64_t draft_frames_ = 0, draft_rounds_ = 0, draft_rows_ = 0;   // the drafter lane's counters
    void* ext_x_ = nullptr;                         // the tier's OWN pinned rows (never cpu_x)
    size_t ext_x_bytes_ = 0;
    int64_t ext_x_embd_ = 0;
    expert_helper::RxBuffer rx_;                    // helper-ipc-diet: ONE grow-only receive buffer for
                                                    // every read_frame (all reads run under io_mtx_ after
                                                    // the startup handshake, so it never races itself)
    int64_t prompt_pairs_ = 0;                      // the outstanding kBeginP's pair count
    uint64_t requests_ = 0, rows_ = 0, bytes_ = 0, timeouts_ = 0, fallbacks_ = 0;
    uint64_t prompt_requests_ = 0, prompt_rows_returned_ = 0, prompt_bytes_ = 0;
    double ms_begin_ = 0, ms_wait_ = 0;
};

/// A pure validation of a kRows payload against the outstanding begin's claims.  Split out so the
/// host fault tests exercise the exact code path the engine runs, with no socket in sight.
/// Writes `out` (rows at the original indices) ONLY when every check passes.
int validate_rows_payload(const uint8_t* payload, size_t n, int64_t n_embd,
                          const std::vector<int32_t>& claims, float* out, int32_t& rows);

/// The proxy's engine-side q8_1 quantizer of the window's activations (the kBegin payload), CPU-only:
/// ggml's fp16 conversion, no device call.  Lives with the client (src/core/expert_helper_socket.cpp)
/// so the host test pins it with no device in sight.
///
/// Bit-identical to the CUDA kernel's blocks for the parts that ARE provable: amax, d (same #606 clamp)
/// and q (q depends on d alone).  The fp16 SUM field is ULP-close to the device's butterfly reduction,
/// NOT proven bit-equal - IEEE-754 addition is not associative - and the acceptance is the helper's rows
/// on hardware, not this function's bytes.  See the definition's note; the golden vectors in
/// tests/core/expert_helper_socket_test.cpp are a regression pin of THIS code, not device parity.
void quantize_q8_1_cpu(const float* x, int64_t n, uint8_t* y);

/// The prompt tier's Q8_K row quantizer (the kBeginP image_format 2 payload): ggml's scalar
/// reference `quantize_row_q8_K_ref` semantics restated with the same bits, so the wire's image is
/// byte-for-byte the CPU reference's own gate/up activation.  Lives here for the same reason as
/// quantize_q8_1_cpu: the host test pins it with no device in sight.  (The all-zero block's bsums
/// are zeroed where ggml leaves them unwritten - the Q8_K kernel reads only d and qs; see the
/// definition's note.)
void quantize_row_q8k_cpu(const float* x, int64_t n, uint8_t* y);

}  // namespace strata::core

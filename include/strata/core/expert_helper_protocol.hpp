// include/strata/core/expert_helper_protocol.hpp - the expert helper's framed AF_UNIX transport (R2).
//
// ONE local SOCK_STREAM AF_UNIX channel between the engine and the CUDA-only `strata-expert-helper`:
// a fixed binary frame - 24-byte header (magic, protocol version, type, sequence, request id, payload
// length) + payload - over `writev`/`readv` with partial I/O, EINTR, EOF and EPIPE handled on every
// path, MSG_NOSIGNAL on every send (the process must never gain a SIGPIPE default), and one outstanding
// request at a time.  Peer credentials (SO_PEERCRED) are checked at connect on both ends: the socket is
// the helper's own to bind (mode 0600, created fresh, never over a foreign file), so a same-uid peer is
// all that is proved; a foreign uid is refused.
//
// This header and its .cpp are CPU-only by construction: no CUDA/HIP headers, no GPU symbols.  The
// engine's proxy client (expert_helper_socket) and the CUDA helper's server loop share it, and the
// host-IPC tests compile it with no GPU runtime linked - the proof is the readelf/nm evidence, not a
// flag.  Socket paths are AF_UNIX byte paths; Windows has no AF_UNIX in this engine's supported
// topology, so connect() refuses with a named reason there (an explicit refusal, never a build break).
//
// The wire is little-endian.  Frame types are fixed; the payload layouts are the structs below packed.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <sys/types.h>
#include <type_traits>
#include <vector>

namespace strata::core {

namespace expert_helper {

/// An ABSOLUTE deadline (the transport's unit) `ms` milliseconds from now, computed with
/// CLOCK_MONOTONIC.  Every caller that holds a RELATIVE timeout (the engine's --expert-helper-
// timeout-ms, the helper's --timeout/--begin-timeout) converts with this and passes the result
/// down: a bare relative value would be read as an absolute stamp and expire at once.
int64_t deadline_after(int64_t ms);

/// Put a descriptor in O_NONBLOCK, the mode this channel's I/O requires (see Socket).  Idempotent.
void set_io_nonblocking(int fd);

/// R5 P1 (the helper's SIGTERM begin-wait fix): install a process-wide WAKE descriptor for every
/// bounded poll this transport makes, and clear it with -1.  A poll that finds the wake descriptor
/// ready returns the SAME value an expired deadline returns (0), so the caller takes its existing
/// named-timeout path and exits - a 30-minute --begin-timeout wait ends at once on a signal.
/// The CUDA helper installs a self-pipe its SIGTERM/SIGINT handler writes one byte to (write() is
/// async-signal-safe); the engine's client NEVER installs one, so every engine-side wait keeps its
/// exact existing behaviour.  -1 (the default) changes nothing: the poll stays a one-fd poll.
void set_poll_wake(int fd);

/// R5 P1 (the item-0 fix's LIVE test, 2026-10-29 ~06:07): a latched SIGTERM/SIGINT is FINAL.  The
/// helper re-listens ONLY for a peer that left before hello in a process nobody asked to stop.  The
/// live helper proved the hole: one SIGTERM logged its own exit line ~10 times, each time the
/// "the peer left before hello; NOT exiting, listening again" branch overrode the latch and re-bound,
/// and the helper survived 60 s until SIGKILL.  This predicate is the single source of truth for that
/// loop-back edge: helper_main.cpp's `goto accept_peer` guard, and the CPU suite's child that
/// reproduces the edge with a real signal (no GPU, no real helper process).
bool helper_relisten_allowed(bool stop_latched, bool hello_seen, bool once);

inline constexpr uint32_t kMagic = 0x53544848u;   // "STHH": Strata expert helper
inline constexpr uint32_t kProtoVersion = 2u;     // R2's ABI v2 transport (nt<=8, k10, <=80 entries)
inline constexpr size_t kHeaderBytes = 24;

// Geometry ceilings the protocol accepts (the engine's fixed kernel contract).
inline constexpr int64_t kMaxTok = 8;             // strata::kernels::cpu::MAXT
inline constexpr int64_t kK = 10;                 // the pack's routing width
inline constexpr int64_t kMaxEntries = kMaxTok * kK;   // 80 owned entries per begin
inline constexpr int64_t kMaxEmb = 65536;
inline constexpr int64_t kMaxFf = 65536;
inline constexpr int64_t kMaxPairs = 1 << 20;    // ranked/claimed pair lists (24576 here)

// R5 P1 (the selected-token prompt tier, r5-three-gpu-design.md §2): the tier's OWN ceilings,
// independent of the chunk's T.  They bound the FRAMES: q8 image 8192 * (2560/32) * 36 =
// 23,592,960 B (22.5 MiB) and rows reply 8192 * (4 + 2560*4) = 83,838,976 B (80 MiB), both under
// kMaxFramePayload (128 MiB).  M4b (2026-10-30, user-authorized): the old 256/256/64 caps were
// measured BINDING on real hardware - every layer of every chunk claimed exactly 256 rows (~0.3% of
// a large chunk's per-layer work) while the round trip still paid its overhead, so prefill measured
// -2.4% on the long arm; raised so the helper mines its resident set at a real share of the layer.
// Every one of these is a hard refusal, checked before either side allocates anything.
inline constexpr int64_t kMaxSelTok = 8192;        // distinct selected token rows for one layer
inline constexpr int64_t kMaxRowsPerLayer = 8192;  // claimed rows for one layer (Σ cnt[e])
inline constexpr int64_t kMaxRowsPerExpert = 256;  // claimed rows for one expert of one layer
inline constexpr int64_t kMaxFallbackRowsTotal = 4096;   // tiled same-layer fallback, per request
inline constexpr uint32_t kFeaturePromptRows = 1u;       // HelloAck.features bit 0 (the tier exists)
inline constexpr uint32_t kFeaturePromptF32 = 2u;        // HelloAck.features bit 1 (fp32 activations, M4c (b))
inline constexpr uint32_t kFeatureDraft = 4u;            // HelloAck.features bit 2 (M7: the drafter lane)
inline constexpr uint32_t kFeatureDraftBf16 = 8u;        // HelloAck.features bit 3 (L8: bf16 draft taps)

// Frame types.  Exactly one request is outstanding: the engine sends hello, fill, and a begin; each
// expect a reply of the named type with the same request id.  A frame with an unexpected type, version,
// magic, length, sequence or request id is a protocol failure: the reader never interprets it.
enum FrameType : uint32_t {
    kHello = 1,        // engine -> helper: geometry, layer range, slot/budget request
    kHelloAck = 2,     // helper -> engine: the helper's limits and protocol agreement
    kFill = 3,         // engine -> helper: ranked (layer,expert) pairs, in fill order
    kFillReport = 4,   // helper -> engine: the resident set it filled (authoritative)
    kBegin = 5,        // engine -> helper: window activations (q8_1) + routed ids + primary residency
    kRows = 6,         // helper -> engine: raw expert rows with the original entry indices (begin reply)
    kBye = 7,          // engine -> helper: orderly close
    kError = 8,        // helper -> engine: a named refusal of the outstanding request
    // R5 P1 (opt-in selected-token PROMPT rows; r5-three-gpu-design.md §2).  THE SAME TRANSPORT,
    // THE SAME VERSION: a new FRAME TYPE, so an old peer answers with this transport's own typed
    // protocol refusal (kErrProtocol / kError), never a silent misparse and never a hang - a frame
    // type the reader does not know is never interpreted.  A new client never sends them at all to a
    // peer that did not advertise the feature bit (see HelloAck::features), so the refusal is the
    // fallback for a mixed build, not the normal path.
    kBeginP = 9,       // engine -> helper: ONE layer's selected token rows (the prompt tier)
    kRowsP = 10,       // helper -> engine: the computed rows with the PAIR indices (beginP reply)
    // M7 (the drafter lane, plan sect12): the SAME extension rule as kBeginP/kRowsP above - a new
    // frame type on the same transport, never sent to a peer that did not advertise
    // kFeatureDraft, so a mixed build answers with this transport's own typed refusal.
    kDraftCtx = 11,    // engine -> helper: context tap rows for the drafter's ring (the prompt phase)
    kDraftAck = 12,    // helper -> engine: the empty ack of kDraftCtx
    kDraftReq = 13,    // engine -> helper: one round - the committed window rows' taps + the anchor
    kDraftReply = 14,  // helper -> engine: the block's draft picks and their probabilities
    // L8 (drafter wire v2, default OFF - the engine's STRATA_DRAFT_TAP_BF16): the SAME taps on
    // HALF the wire - the rows of kDraftCtx/kDraftReq travel as bf16 (2 bytes per tap) instead of
    // fp32, same DraftCtxReq/DraftReq headers.  The SAME extension rule as kBeginP and the M7
    // lane above: a new frame type, sent only to a peer that advertised kFeatureDraftBf16, so a
    // mixed build answers with this transport's own typed refusal, never a silent misparse.
    kDraftCtxBf16 = 15,   // engine -> helper: kDraftCtx's rows as bf16
    kDraftReqBf16 = 16,   // engine -> helper: kDraftReq's rows as bf16
};

// Frame limits the reader enforces before allocating anything.
inline constexpr size_t kMaxFramePayload = 128u << 20;   // 128 MiB: above the M4b caps' 80 MiB rows reply

// M1 (capacity): every MiB-valued knob on this path (`--expert-helper-mib`, `--expert-helper-reserve-mib`,
// the helper's `--reserve-mib`) is spent as `mib * 1048576` BYTES.  The largest count whose byte value
// is representable in int64: past it the number is not a budget, and it is refused by name (never a
// wrap, never a negative budget).
inline constexpr int64_t kMaxMiB = 9223372036854775807LL / 1048576;   // 8,796,093,022,207 MiB

// The header on the wire, little-endian, kHeaderBytes bytes.
struct Header {
    uint32_t magic = kMagic;
    uint32_t version = kProtoVersion;
    uint32_t type = 0;
    uint32_t seq = 0;
    uint32_t req_id = 0;
    uint32_t len = 0;   // payload bytes
};

// kHello payload: geometry + the request.  `claimed_bytes` bytes of bitmap follow `sizeof(HelloReq)`:
// (layer*n_expert+expert)/8, bit 1 = the owning stage holds it (the helper fills nothing a stage owns).
struct HelloReq {
    int32_t n_layers = 0;
    int32_t n_expert = 0;
    int32_t n_embd = 0;
    int32_t n_ff = 0;
    int32_t layer_lo = 0;
    int32_t layer_hi = 0;     // inclusive
    int64_t slots = 0;        // exact count (auto resolved by the engine)
    int64_t reserve_mib = 512;
    uint32_t claimed_bytes = 0;
    uint32_t reserved0 = 0;
};

struct HelloAck {
    int32_t max_tok = 0;
    int32_t k = 0;
    int32_t max_entries = 0;
    int32_t status = 0;       // 0 = agreed, else an expert_helper status
    int64_t free_mib = 0;     // the device's free VRAM as the helper sees it (report only)
    // R5 P1: the feature bits this helper serves (kFeaturePromptRows = the kBeginP/kRowsP tier).
    // APPENDED, so the exchange stays byte-compatible BOTH ways, which is what makes an old peer
    // safe: an OLD helper answers the 24-byte legacy shape (kHelloAckLegacyBytes) and a new client
    // reads features = 0 from it, so it never sends kBeginP at all; a new helper answers 32 bytes
    // and an OLD client memcpy's its 24-byte prefix and ignores the rest.  A feature the peer did
    // not advertise is NEVER used - there is no probing, no guess, no reliance on the refusal.
    uint64_t features = 0;
};
inline constexpr size_t kHelloAckLegacyBytes = 24;   // sizeof of the R2 ack (no features field)

// kFill payload: `n_pairs` pairs (int32 layer, int32 expert) in the engine's fill order (rank order in
// the range, stage-held pairs already removed).  The helper fills in this order and reports what fit.
struct FillReq {
    int32_t n_pairs = 0;
    int32_t reserved0 = 0;
};

// kFillReport payload: `n_resident` pairs the helper actually holds (authoritative; the engine mirrors
// it into holds()/resident/gib and never re-queries it in the hot path).
struct FillReport {
    int64_t resident = 0;
    int64_t bytes = 0;        // slot bytes the helper allocated for the resident set
    int64_t scratch_bytes = 0;
    int64_t buffers_bytes = 0;
    int32_t n_pairs = 0;
    int32_t reserved0 = 0;
};

// kBegin payload: the window's routed ids follow `sizeof(BeginReq)` as `n_tok*k` int32s (the ids in
// routing order, exactly the array the engine's dispatch passes to its tiers; ids_bytes = n_tok*k*4);
// then (n_tok*n_expert + 7)/8 bytes of PRIMARY residency: bit 1 = (layer, expert) resident on the
// primary (the engine computed none of the primary's rows, so the helper claims none of those
// entries).  The entries listed in `ids` are the window's routed ids in routing order (token t:
// ids[t*k .. t*k+k-1]); the helper claims each entry whose expert it holds and is not
// primary-resident.  `q8` is n_tok * (n_embd/32) * 36 bytes: every token's activation as Q8_1
// blocks - the same staging the in-process tier's quantize_q8_1_rows produces.
struct BeginReq {
    int32_t layer = 0;
    int32_t n_tok = 0;
    int32_t k = 0;
    int32_t ids_bytes = 0;    // n_tok*k*4 (the int32 ids, routing order)
    int32_t n_q8 = 0;         // n_tok * (n_embd/32) * 36
    int32_t reserved0 = 0;
};

// kRows payload: `n_rows` rows follow `sizeof(RowsResp)`: for each, an int32 original entry index and
// n_embd floats (UNWEIGHTED raw expert rows, exactly what the in-process tier writes into the host
// rows; the engine weights them once in the existing moe_combine).  Rows whose entry the helper did
// not claim are absent: the engine's finish fails for the missing rows and the dispatch recomputes
// them on the CPU (same-request fallback), never a silent zero.
struct RowsResp {
    int32_t n_rows = 0;
    int32_t reserved0 = 0;
};

// ---- R5 P1 (the selected-token prompt tier): kBeginP / kRowsP payloads. ----
//
// kBeginP asks ONE layer of the helper's window (32..47 on the split32 box) to compute a bounded
// set of SELECTED token rows: `n_pairs` (token, expert) pairs, each token's activation as the same
// canonical Q8_1 blocks kBegin carries (the image is indexed by the pair's TOKEN, so one activation
// set serves all of that token's rows), plus this layer's PRIMARY residency bitmap exactly as
// kBegin's (bit 1 = the primary owns the expert's row, so the helper claims none of those pairs).
// A pair the helper does not hold, or whose expert the primary owns, is absent from the reply - the
// engine's finish_prompt sees the missing rows and recomputes them on the CPU for this request
// (same-layer fallback), never a silent zero.
struct BeginPReq {
    int32_t layer = 0;
    int32_t n_sel_tok = 0;      // distinct selected tokens, 1..kMaxSelTok (the q8 image's rows)
    int32_t k = 0;              // kK (the pack's routing width)
    int32_t n_pairs = 0;        // claimed rows, 1..kMaxRowsPerLayer
    int32_t n_q8 = 0;           // the image section's bytes: q8 -> n_sel_tok*(n_embd/32)*36, fp32 -> n_sel_tok*n_embd*4
    int32_t image_format = 0;   // 0 = canonical q8_1 blocks, 1 = fp32 rows the helper quantizes (kFeaturePromptF32)
};

// The kBeginP body, in this exact order after sizeof(BeginPReq):
//   n_pairs * 8 bytes: (int32 token, int32 expert) - token indexes the q8 image (0..n_sel_tok-1)
//   (n_sel_tok * n_expert + 7) / 8 bytes: the layer's PRIMARY residency bitmap
//   n_q8 bytes: the selected tokens' activations (Q8_1 blocks, or fp32 rows when image_format is 1)
// Every size is checked before anything is allocated: n_sel_tok <= kMaxSelTok, n_pairs <=
// kMaxRowsPerLayer, per-expert pairs <= kMaxRowsPerExpert, k == kK, the exact byte total.
struct RowsPResp {
    int32_t n_rows = 0;         // the rows the helper computed (<= n_pairs)
    int32_t reserved0 = 0;
};

// The kRowsP body: n_rows * (4 + n_embd*4) bytes after sizeof(RowsPResp): an int32 PAIR index into
// the request's pair list and n_embd UNWEIGHTED floats - the same row shape as kRows, and the same
// contract: rows for a pair the engine did not claim are refused there, every claim it made must be
// answered, and all floats must be finite.

// ---- M7 (the drafter lane; docs/work/MIXED_BACKEND_RPC_PLAN.md sect12): the draft frames. ----
//
// The displaced drafter is Dflash2Drafter's contract over the wire: the engine owns the target's
// forward and ships the TAP ROWS (the target's residual at the drafter's tap layers, one row =
// n_planes * taps_hidden fp32, planes packed contiguously exactly as Dflash2Drafter::context /
// ::draft take them); the helper owns the weights, the ring and the forward, and returns the block.
// The prompt phase feeds the ring with kDraftCtx (reset=1 on the first frame of a request, rows
// sub-chunked to kMaxDraftCtxRows so a frame stays under kMaxFramePayload); decode sends one
// kDraftReq per verify round (the committed window rows plus the anchor token); kDraftReply carries
// the selector's picks and probabilities.  A refusal or a timeout disables drafting for the rest of
// the request - drafts are speculative, so the target's verify path alone stays correct.
inline constexpr int64_t kMaxDraftRound = 8;      // Dflash2Drafter::kRows - 1 (committed rows per round)
inline constexpr int64_t kMaxDraftPicks = 16;     // reply picks bound (7 at the published geometry)
inline constexpr int64_t kMaxDraftCtxRows = 2048; // context rows per frame: 2048*5*2560*4 = 100 MiB < 128 MiB

// kDraftCtx payload: `n_rows` tap rows follow `sizeof(DraftCtxReq)` as
// n_rows * n_planes * taps_hidden * 4 bytes.
struct DraftCtxReq {
    int32_t n_rows = 0;        // 1..kMaxDraftCtxRows
    int32_t n_planes = 0;      // the artifact's tap planes (5 at the published geometry; geometry-checked)
    int32_t taps_hidden = 0;   // the target's n_embd (2560)
    int32_t reset = 0;         // 1 = the first frame of a new request: the helper empties its ring first
    int64_t pos0 = 0;          // the absolute position of row 0
};

// The pre-n_drafts DraftReq size: an engine built before the pick-cap field still talks to a
// current helper (its 24-byte requests are recognised and the helper chooses the block).
constexpr uint32_t kDraftReqLegacyBytes = 24;

// kDraftReq payload: `n_new` tap rows follow `sizeof(DraftReq)` with the same packing.
struct DraftReq {
    int32_t n_new = 0;         // 1..kMaxDraftRound: the committed window rows since the last round
    int32_t n_planes = 0;
    int32_t taps_hidden = 0;
    int32_t anchor = 0;        // the last committed token: the block drafts after it
    int64_t pos0 = 0;          // the absolute position of the first new row
    int32_t n_drafts = 0;      // 0 = legacy (the helper chooses the block); else the engine's reply cap 1..kMaxDraftPicks
};

// kDraftReply payload: `n_drafts` int32 token ids follow `sizeof(DraftReply)`, then `n_drafts` fp32
// probabilities - the selector's pick per block row, the same arrays Dflash2Drafter::draft fills.
struct DraftReply {
    int32_t n_drafts = 0;      // 1..kMaxDraftPicks
    int32_t reserved0 = 0;
};

// kError payload.
struct ErrResp {
    int32_t status = 0;
    int32_t detail = 0;
    char text[112] = {};
};

// ---- The wire's LAYOUT IS PINNED, not assumed. ----
//
// The frame payloads travel as raw struct bytes: the sender writes `sizeof(X)` and the reader
// memcpy's `sizeof(X)` (src/core/expert_helper_socket.cpp, tools/mixed_gpu/sidecar/helper_main.cpp),
// and the two ends are built by DIFFERENT compilers - the helper by nvcc, the engine by hipcc/cc.
// Every struct below relies on the natural SysV/x86-64 layout (the int64 fields land on 8-byte
// offsets, so there is no tail padding to disagree about); nothing here is #pragma pack'd, because
// a packed struct would CHANGE the bytes for a peer compiled without the pragma.  These asserts are
// the guard: the same header compiles into both builds, so a layout the two compilers disagree on
// becomes a COMPILE ERROR in whichever build sees it, instead of a silent wire-corruption class
// (a shifted `pos0`, a `reset` read out of a neighbouring field).  An int32 followed by an int64 at
// an odd offset would pad - and every such field below is asserted by offsetof, not by a comment.
static_assert(std::is_standard_layout<Header>::value && std::is_trivially_copyable<Header>::value,
              "the frame header must be a plain standard-layout byte block");
static_assert(sizeof(Header) == kHeaderBytes, "the frame header's sizeof must equal kHeaderBytes");
static_assert(sizeof(HelloReq) == 48 && offsetof(HelloReq, slots) == 24 &&
              offsetof(HelloReq, reserve_mib) == 32 && offsetof(HelloReq, claimed_bytes) == 40 &&
              offsetof(HelloReq, reserved0) == 44,
              "HelloReq's layout is pinned on the wire (the claimed bitmap starts at sizeof)");
static_assert(sizeof(HelloAck) == 32 && offsetof(HelloAck, free_mib) == 16 &&
              offsetof(HelloAck, features) == 24 && kHelloAckLegacyBytes == 24,
              "HelloAck: the 24-byte legacy prefix plus the appended features field");
static_assert(sizeof(FillReq) == 8, "FillReq's layout is pinned on the wire");
static_assert(sizeof(FillReport) == 40 && offsetof(FillReport, n_pairs) == 32,
              "FillReport's layout is pinned on the wire");
static_assert(sizeof(BeginReq) == 24 && offsetof(BeginReq, ids_bytes) == 12 &&
              offsetof(BeginReq, n_q8) == 16,
              "BeginReq's layout is pinned on the wire (ids start at sizeof)");
static_assert(sizeof(RowsResp) == 8, "RowsResp's layout is pinned on the wire (rows start at sizeof)");
static_assert(sizeof(BeginPReq) == 24 && offsetof(BeginPReq, n_q8) == 16 &&
              offsetof(BeginPReq, image_format) == 20,
              "BeginPReq's layout is pinned on the wire (pairs start at sizeof)");
static_assert(sizeof(RowsPResp) == 8, "RowsPResp's layout is pinned on the wire");
static_assert(sizeof(DraftCtxReq) == 24 && offsetof(DraftCtxReq, reset) == 12 &&
              offsetof(DraftCtxReq, pos0) == 16,
              "DraftCtxReq's layout is pinned on the wire (pos0 sits on the 8-byte boundary "
              "after the four int32s - an int32/int64 mix pads, so the offset is asserted)");
static_assert(sizeof(DraftReq) == 32 && offsetof(DraftReq, anchor) == 12 && offsetof(DraftReq, pos0) == 16 &&
              offsetof(DraftReq, n_drafts) == 24 && kDraftReqLegacyBytes == 24,
              "DraftReq's layout is pinned on the wire (n_drafts appends after pos0; bytes 28-31 "
              "are alignment padding, never read as a field)");
static_assert(sizeof(DraftReply) == 8, "DraftReply's layout is pinned on the wire (picks start at sizeof)");
static_assert(sizeof(ErrResp) == 120 && offsetof(ErrResp, text) == 8,
              "ErrResp's layout is pinned on the wire (both ends memcpy sizeof(ErrResp))");

// The status space of this transport (distinct from the prep ABI's): 0 ok, every nonzero a named
// failure.  The engine reports the name; a helper failure is never an engine crash.
enum Status {
    kOk = 0,
    kErrConnect = 1,        // connect refused/timeout/unix-path unsupported
    kErrPeer = 2,           // peer credentials refused (foreign uid, protocol garbage)
    kErrProtocol = 3,       // magic/version/length/type violation
    kErrGeometry = 4,       // the hello geometry is outside the kernel contract
    kErrUnhealthy = 5,      // channel closed mid-request (EOF/EPIPE), begin refused
    kErrTimeout = 6,        // a bounded poll deadline expired (read/write/connect)
    kErrShort = 7,          // a payload shorter than the frame's own contract
    kErrBadId = 8,          // a reply index is out of range, duplicated, or not claimed
    kErrMissing = 9,        // the reply is well-formed but a claimed row is absent
    kErrNonFinite = 10,     // a reply row carries a non-finite value
    kErrSeq = 11,           // sequence / request-id mismatch
    kErrRequest = 12,       // the helper refused the request (kError detail in text)
    kErrInternal = 13,
    // M7 (sect12a, BUG E) CLIENT-SIDE ONLY, appended: this status never goes on the wire and no
    // peer ever sends it, so the status space stays byte-compatible both ways.  The drafter lane
    // shares the channel with the pipelined window/prompt tiers, which keep ONE request
    // outstanding across a layer's compute (this header's own rule, line 6-7).  A draft frame
    // that will not fit behind the in-flight request answers with THIS name: no frame is written,
    // the channel stays open, and the caller disables drafting for the rest of the request.
    kErrBusy = 14,          // a draft frame found an in-flight request on the shared channel
};
const char* status_name(int status);

// The AF_UNIX channel with deadline-bound I/O.  A deadline is an ABSOLUTE CLOCK_MONOTONIC millisecond
// stamp; deadline_ms < 0 means "as late as a poll can mean" (the caller's own bound), 0 means "now".
// The descriptor is ALWAYS non-blocking for this channel's I/O (adopt does it, connect keeps it,
// accept sets it): every write is one poll and one writev, and a full socket buffer answers EAGAIN,
// which re-polls against the same deadline.  A blocking descriptor cannot promise that - a 1.5 MB
// write to a peer that is busy computing blocks INSIDE the kernel (sock_alloc_send_pskb), where no
// deadline exists - so the socket never runs blocking.  Writes use MSG_NOSIGNAL; the process keeps
// its SIGPIPE default untouched.
class Socket {
public:
    Socket() = default;
    ~Socket();
    Socket(const Socket&) = delete;
    Socket& operator=(const Socket&) = delete;
    int fd() const { return fd_; }
    void close();
    /// Adopt/release an existing descriptor (the client owns lifetime, the Socket the I/O rules).
    /// Adopting puts the descriptor in O_NONBLOCK: the deadline-bound I/O above needs it.
    void adopt(int fd) { fd_ = fd; set_io_nonblocking(fd); }
    void release() { fd_ = -1; }
    uint32_t seq_out() const { return seq_out_; }
    int connect(const std::string& path, int64_t connect_timeout_ms, bool check_peer = true);
    /// Bind the helper's own path (refuses an existing file; creates the socket 0600).  `unbind`
    /// unlinks ONLY the path this socket bound - never a foreign file.
    int listen_bind(const std::string& path);
    void unbind();
    int accept(int64_t deadline_ms, bool check_peer = true);
    int write_all(const uint8_t* data, size_t n, int64_t deadline_ms) const;
    int read_all(uint8_t* data, size_t n, int64_t deadline_ms) const;
    int write_frame(uint32_t type, uint32_t req_id, const uint8_t* payload, size_t n, int64_t deadline_ms);
    /// H13: a REPLY echoes the request frame's sequence - the client's finish() validates the echo
    /// (write_frame's own counter is connection-lifetime on the helper side and never matches the
    /// per-call client socket's counter; replies that used it were rejected as kErrSeq and the
    /// channel closed after one window - r11..r16).
    int write_reply(uint32_t type, uint32_t req_id, uint32_t echo_seq, const uint8_t* payload, size_t n,
                    int64_t deadline_ms);
    int read_frame(Header& head, std::vector<uint8_t>& payload, int64_t deadline_ms) const;
    /// SO_PEERCRED on the accepted/connected stream: the peer must run as the SAME uid.  0 = ok.
    static int peer_uid(int fd);
    static int peer_ok(int fd, uid_t expect);
    /// Non-blocking liveness: kOk when the stream is usable, kErrUnhealthy when the peer is gone.
    int peer_closed() const;

private:
    int write_seq(uint32_t seq, uint32_t type, uint32_t req_id, const uint8_t* payload, size_t n,
                  int64_t deadline_ms);
    int fd_ = -1;
    std::string bound_path_;
    uint32_t seq_out_ = 0;
};

// Header codec (host order <-> wire bytes; little-endian on the wire).
void encode_header(const Header& h, uint8_t* out);
int decode_header(const uint8_t* in, Header& out);   // kOk / kErrProtocol / kErrShort

// Socket path safety: an absolute path, printable, within sun_path, and NOT an existing file
// (the helper binds its own socket fresh; the engine never unlinks a foreign file).
bool socket_path_ok(const std::string& path, std::string& why);
// The connect-side check the ENGINE runs: same shape rules, but a socket the helper has BOUND is the
// expected case (the helper starts before the engine); only a foreign non-socket file is refused.
bool socket_path_connect_ok(const std::string& path, std::string& why);

// The kHello claimed bitmap's UNPACK, shared so the host CPU test pins the exact bit semantics the CUDA
// helper runs (H8): the wire packs bit-per-pair - pair i (layer*n_expert+expert) is bit (i%8) of byte
// (i/8), bit 1 = a stage holds that pair - while serve_fill indexes byte-per-pair, so the helper must
// unpack the arriving bytes into one byte per pair.  `out` is `n_pairs` bytes; pairs with no packed
// byte (nbytes <= i/8) are UNCLAIMED and the caller's bytes are never read past `nbytes`.  The engine's
// own packed size is (n_pairs+7)/8.  CPU-only, like the rest of this transport.
void unpack_claimed_bitmap(const uint8_t* bits, size_t nbytes, size_t n_pairs, std::vector<uint8_t>& out);

}  // namespace expert_helper
}  // namespace strata::core

// On-disk persistence of a conversation's state (SavedConversation): one file holding the running state, the
// checkpoints the caller passes (the engine passes only the deepest one), every QSA layer's authoritative K/V and
// the draft layer's K/V.  Pure host code: no CUDA.  Format v1 and the fail-closed read order are in
// docs/DETAILS.md (Session files).
//
// Format v1 is little-endian, with fixed-width integers and IEEE-754 floats; a big-endian build is refused at
// compile time.  The hashes detect accidental corruption; they do not authenticate a file: restore trusted files only.
#pragma once

#include "strata/core/conversation_cache.hpp"
#include "strata/core/dflash_ring.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace strata::core {

// What the file is bound to: the model files it was computed with, and the engine settings that change what the
// saved bytes mean.  A read with any other identity is refused before the payload is parsed.
struct SessionFileIdentity {
    uint64_t model = 0, config = 0;
};

// Non-cryptographic 64-bit hash with xxHash64-style rounds (NOT the standard XXH64 stream): corruption and identity
// checks, not adversarial input.  Streaming and one-shot agree.
class SessionHasher {
public:
    explicit SessionHasher(uint64_t seed = 0);
    void update(const void* data, size_t n);
    uint64_t digest() const;
private:
    void block(const uint8_t* p);
    uint64_t lane_[4];
    uint8_t pending_[32];
    size_t npending_ = 0;
    uint64_t total_ = 0;
};
uint64_t session_hash64(const void* data, size_t n, uint64_t seed);

// Typed, length-delimited fields for an identity hash: each field is (tag, type, length, bytes), so two different
// field lists never give the same byte stream, and a double is hashed by its exact bits (no decimal rounding).
class SessionIdentityBuilder {
public:
    explicit SessionIdentityBuilder(uint64_t seed) : hash_(seed) {}
    void str(const std::string& tag, const std::string& value);
    void i64(const std::string& tag, int64_t value);
    void u64(const std::string& tag, uint64_t value);
    void f64(const std::string& tag, double value);
    void bytes(const std::string& tag, const void* data, size_t n);
    uint64_t digest() const { return hash_.digest(); }
private:
    void field(const std::string& tag, uint8_t type, const void* data, uint64_t n);
    SessionHasher hash_;
};

// The RESOLVED rope configuration (rope_scaling.hpp, after the CLI flags and the model file's keys met).  The cached
// K is post-RoPE, so a different rotation makes the saved K mean something else.
struct SessionRope {
    int64_t type = 0;
    double freq_base = 0, factor = 0, freq_scale = 0, orig_ctx = 0, ext_factor = 0, attn_factor = 0, beta_fast = 0,
           beta_slow = 0;
};

// Every engine setting that changes what the saved state means.  Sampling, seeds, draft tuning, the expert tier and
// the prompt-reading chunk are not here: they change what is computed next, not what the saved cells hold.
struct SessionConfig {
    std::string engine_version;     // a file is bound to one engine version
    std::string backend;            // "cuda" or "hip": no cross-backend restore
    std::string kv;                 // --kv
    int64_t max_context = 0, kv_resident = 0, mtp_window = 0;
    bool kv_rot = false;            // STRATA_KV_ROT
    SessionRope rope;
    // the control vectors as loaded: a digest of the tables the engine uploaded (every file's content x its exact
    // scale, summed), the mode, the layer range and the direction; 0 when none is loaded
    uint64_t cvec = 0;
    // the arithmetic switches that change the computed state (name, value), in a fixed order
    std::vector<std::pair<std::string, int64_t>> switches;
};
uint64_t session_config_fingerprint(const SessionConfig& config);

// One model input the engine loaded and the role it plays ("native shard 1", "expert blk.3.ffn_up", ...).  The role,
// not the path, enters the fingerprint, so a moved model folder still matches.  Every (role, file) pair counts: one
// file in two roles is two entries (its bytes are read once).  `optional`: a missing file is recorded as absent
// instead of failing (a pack without experts.bin).
struct SessionModelFile {
    std::string role, path;
    bool optional = false;
};
// Sampled fingerprint of the model inputs: role, size, first and last MiB of each entry, in the given order (a file
// listed under several roles is read once).  Paths are UTF-8 (wide APIs on Windows; invalid UTF-8 is refused).  An
// edit in the middle of a file that keeps its size is NOT detected: model files must not change while sessions saved
// with them are kept.  `beat` (optional) is called after each distinct file is read.
bool session_model_fingerprint(const std::vector<SessionModelFile>& files, uint64_t& fingerprint, std::string& error,
                               const std::function<void()>& beat = {});

// What the engine loaded, as its loaders resolved it (generate.cpp fills it).  `experts`: (role, file) of the expert
// source - the pack's experts.bin, or every expert tensor read from a GGUF in place, per layer and role, as
// native_experts.txt resolved it (ExpertSource::model_inputs).
struct SessionInputs {
    std::vector<std::string> native_shards, native_dense, native_head;
    std::string embedding, ple, pack, mtp;
    std::vector<std::pair<std::string, std::string>> experts;
};
// The fingerprint's list: every role with its file, none merged (a path in two roles is two entries).
std::vector<SessionModelFile> session_model_inputs(const SessionInputs& inputs);

// Why a session file operation failed, for the caller's answer (the server maps it to an HTTP status):
//   invalid   refused: not a session file, corrupt, another model or configuration, over this engine's limits, a
//             link, nothing to save (400; nothing changed)
//   storage   no room on the disk: the free-space preflight, or ENOSPC / EDQUOT / EFBIG (507)
//   memory    the RAM preflight refused it, or an allocation failed (503)
//   io        any other failure: permissions, read/write/flush/rename errors, a missing folder (500)
enum class SessionError { none, invalid, storage, memory, io };
const char* session_error_name(SessionError e);
struct SessionStatus {
    SessionError error = SessionError::none;
    // write: the new file has replaced `path` (the rename happened).  Together with a failure: the file's bytes are
    // complete and flushed, but the folder flush after the rename failed, so the new name may not survive a power loss
    bool published = false;
    // write (POSIX): this filesystem cannot flush a folder (fsync EINVAL); the save succeeded without that flush
    bool dir_flush_unsupported = false;
};

// Progress of a file transfer: (bytes done, bytes in the file).  A write reports once when its temporary file is
// created (0) and after every block it hands to the OS (16 MiB, or less for the last one: a small file reports too);
// a read reports after every block it reads (16 MiB, or what is left; the first one, which holds the header, too).
// Each report follows a completed transfer of at most 16 MiB: no step between two reports moves more.
using SessionProgress = std::function<void(uint64_t done, uint64_t total)>;
// A step that does not stream - one call that blocks until it is done, over far more than 16 MiB or on the OS's
// schedule (flushing the file, the rename and folder flush) - is announced before it starts, with the bytes it
// concerns.  The engine turns this into an explicit, bounded allowance (session_phase_limit_s) for its watchdog and
// for the server; when the allowance runs out the step is stuck.
using SessionPhase = std::function<void(const char* phase, uint64_t bytes)>;
// The longest a non-streaming step over `bytes` may take before both sides call it stuck: 60 s plus one second per
// 4 MiB (a slow disk or network folder at 4 MiB/s), at most an hour.  1.2 GB: 345 s.
int64_t session_phase_limit_s(uint64_t bytes);

// Optional knobs of a write.
struct SessionWriteOptions {
    // the free-space PREFLIGHT: refused when the disk has less than the new file plus this many bytes free before the
    // write starts.  Not a reservation: other writers can take the space afterwards.
    uint64_t min_free_bytes = 0;
    bool durable = true;            // flush the file before the rename and (POSIX) the folder after it
    SessionProgress progress;
    // "flush" (the file's flush, `bytes` = the file) and "publish" (rename and folder flush), before each starts
    SessionPhase phase;
    // the transfer mode, reported at the first block that reaches the OS (and again if it changes mid-write: a
    // filesystem that took O_DIRECT at open and refuses the transfer drops to buffered).  True = strict direct I/O.
    // Optional: a caller that needs to know which path its bytes took (the disk tier checks its `direct` promise).
    std::function<void(bool direct)> mode;
    // tests: return an errno value to make a step fail ("write", "file_flush", "rename", "dir_flush"), 0 to go on
    std::function<int(const char* step)> fault;
};

// Writes a temporary file beside `path` (a unique hidden name, created exclusively: never an existing file or link;
// mode 0600 on POSIX), flushes it, renames it over `path` and, on POSIX, flushes the folder (Windows has no folder
// flush: the rename is MoveFileExW with MOVEFILE_WRITE_THROUGH).  A failure BEFORE the rename keeps an existing file
// at `path` and removes only this call's own temporary file.  A failure of the folder flush AFTER the rename returns
// false with `status->published`: the new file is at `path` (the old one is gone), its directory entry may not
// survive a power loss.  A filesystem that cannot flush a folder (EINVAL) is not a failure; `status` says so.
bool session_file_write(const std::string& path, const SavedConversation& image, const SessionFileIdentity& id,
                        size_t& bytes, std::string& error, const SessionWriteOptions& options = {},
                        SessionStatus* status = nullptr);

// One K/V layer read straight from where it lives (device or pinned host pool) into the file's staging buffer,
// without a full host copy first.  `read(part, offset, dst, n)` copies bytes [offset, offset+n) of part
// 0..4 = k, v, k_scale, v_scale, pooled; after a failed read `error()` (optional) says why.  The file bytes are
// identical to writing a captured ConversationKv.
struct SessionKvSource {
    int format = 0;
    int64_t cells = 0, heads = 0, head_dim = 0, page_size = 0, pooled_rows = 0, idx_dim = 0;
    std::array<size_t, 5> sizes{};
    std::function<bool(size_t part, size_t offset, void* dst, size_t n)> read;
    std::function<std::string()> error;
};
// `meta` carries everything but the K/V (meta.kv must be empty); `kv` supplies the layers in order.
bool session_file_write(const std::string& path, const SavedConversation& meta, const std::vector<SessionKvSource>& kv,
                        const SessionFileIdentity& id, size_t& bytes, std::string& error,
                        const SessionWriteOptions& options = {}, SessionStatus* status = nullptr);

// The checkpoints worth a disk write: the deepest one (the next turn's resume point when the live tail was
// rewritten).  Earlier checkpoints only serve edits further back and cost ~118 MB each at this geometry.
std::vector<ConversationCheckpoint> session_checkpoints_to_save(const std::vector<ConversationCheckpoint>& chain);
// The same choice without a copy (nullptr for an empty chain).
const ConversationCheckpoint* session_deepest_checkpoint(const std::vector<ConversationCheckpoint>& chain);

// What a SAVE copies into RAM besides what the engine already holds, for its RAM preflight.
struct SessionSaveLive {
    uint64_t state_bytes = 0;   // the live running state (gdn + ple + every owned QSA layer's tail, dead, block_pos)
    uint64_t tokens = 0, images = 0;
    uint64_t kv_layers = 0;     // K/V sources (owned QSA layers + the draft)
};
// The peak, with saturating arithmetic: two copies of the deepest checkpoint (the list handed to the capture and the
// one inside the captured image), the copied live state with its token and image lists, the K/V source directory,
// the 16 MiB write buffer and 1 MiB for the small vectors.
uint64_t session_save_peak_bytes(const ConversationCheckpoint* deepest, const SessionSaveLive& live);
// The SAVE's RAM PREFLIGHT, BEFORE anything is copied: `admit` is asked for session_save_peak_bytes; only when it
// agrees is the deepest checkpoint copied into `out`.  On a refusal nothing was allocated, `out` is empty, `chain`
// and the live state are untouched and no file was opened.  Not a reservation.
bool session_save_checkpoints(const std::vector<ConversationCheckpoint>& chain, const SessionSaveLive& live,
                              const std::function<bool(uint64_t need_bytes, std::string& why)>& admit,
                              std::vector<ConversationCheckpoint>& out, std::string& why);

// What a read checks before it allocates.  Every count is checked against the bytes left in the payload and against
// these limits BEFORE its array is allocated; the defaults accept whatever the payload can hold.  The engine sets all
// of them from its runtime (conversation_session_read_limits), and then the file size is bounded too.
struct SessionReadLimits {
    uint64_t max_file_bytes = UINT64_MAX;
    uint64_t max_tokens = UINT64_MAX;       // token and image counts of the live state and of each checkpoint
    uint64_t max_checkpoints = UINT64_MAX;
    uint64_t max_kv_layers = UINT64_MAX;
    // the runtime's geometry and layer range: a file with others is refused before its state arrays are read
    std::optional<std::array<int64_t, 18>> geometry;
    std::optional<std::pair<int64_t, int64_t>> layer_range;
    // the most bytes of each running-state array (gdn, ple, tails, dead, block_pos), live and per checkpoint
    std::array<uint64_t, 5> max_state_bytes{UINT64_MAX, UINT64_MAX, UINT64_MAX, UINT64_MAX, UINT64_MAX};
    // per K/V layer (the QSA layers, then the draft): the most bytes of each part (k, v, k_scale, v_scale, pooled).
    // A layer past the end of the list is bounded by the payload only.
    std::vector<std::array<uint64_t, 5>> max_kv_bytes;
    // the RAM PREFLIGHT: asked once the header and identity match and before any payload allocation, with what the
    // parse holds at its peak (session_read_peak_bytes); false refuses the file.  Not a reservation.
    std::function<bool(uint64_t need_bytes, std::string& why)> admit;
    SessionProgress progress;
};
// The largest file these limits admit (UINT64_MAX when one of them is open); session_file_read applies it.
uint64_t session_read_max_file_bytes(const SessionReadLimits& limits);
// What the RAM preflight is asked for a file of `file_bytes`: the parsed image (about the payload), the 16 MiB read
// buffer, and the per-allocation overhead of its arrays (a page per 16 MiB segment plus the small vectors).
uint64_t session_read_peak_bytes(uint64_t file_bytes);
// Full validation (a regular file with one link, size, header, identity, bounded parse, payload hash) before `image`
// is replaced.  The file is opened without following a symbolic link (or a reparse point) at its name, and the checks
// are made on the opened handle.  On failure `image` is unchanged and `status->error` says what kind of failure.
bool session_file_read(const std::string& path, const SessionFileIdentity& id, SavedConversation& image,
                       size_t& bytes, std::string& error, const SessionReadLimits& limits = {},
                       SessionStatus* status = nullptr);

// The folder the free-space preflight asks about for `path`, by the rules of Windows or of POSIX (exposed so both can
// be tested anywhere).  Windows: backslashes and a trailing backslash, as GetDiskFreeSpaceExW requires for a UNC
// folder (\\server\share\dir\) and gives a drive root (C:\); empty for a bare file name (the current folder).
std::string session_free_space_dir(const std::string& path, bool windows_rules);

// ============================ format v2 ============================
// A split-aware, region-streamed session file: its own magic and version, so v1's bytes and the v1 read/write
// behavior above stay exactly as they are, and each reader refuses the other's file.  A v2 file is a bounded
// manifest that NAMES every bulk region, the region bytes concatenated in the manifest's order, and a trailer
// carrying the per-region checksums (the committed marker: end marker plus the manifest, payload and region-table
// checksums).  Nothing here materializes a region's bytes as a host image: a write streams them from the source
// that owns them, a read streams them into the sink the caller owns.
//
// Layout:  header(64) | manifest | regions | region table | trailer(32)
// Header:  magic[8] version:u32 header_size:u32 model:u64 config:u64 payload:u64 manifest_bytes:u64 reserved:u64
//          header_hash:u64 (session_hash64 of bytes 0..55, seed 0).  The reserved field must be zero: a v2 reader
//          refuses a header it cannot fully interpret.
// Manifest: sections, each  tag:u32 body_len:u64 body;  required tags in this order LAYT DRFT TOKN IMAG CPST RDSK,
//          then the terminator tag 0.  An unknown tag, a missing required tag, a repeated tag, a section whose
//          fields do not fill its declared length, or leftover bytes are refusals: v2 has no optional sections.
// Region table:  count:u64 then count x checksum:u64, positional to the manifest's region directory; region
//          checksums are session_hash64 of the region's bytes (seed 0), so one damaged region is found by name.
// Trailer:  payload_hash:u64 manifest_hash:u64 region_table_hash:u64 end_marker[8].

// What a v2 region's bytes are.  A kind the reader does not define is a refusal, never a skipped region.
enum class SessionRegionKind : int32_t {
    none = 0,
    live_state = 1,        // a stage's live running state: parts 0..4 = gdn, ple, tails, dead, block_pos
    kv = 2,                // one layer of one stage: parts 0..4 = k, v, k_scale, v_scale, pooled
    checkpoint_state = 3,  // a checkpoint's running state for one stage (parts 0..4 as live_state)
    mtp = 4,               // one stage's optional MTP layer K/V (parts 0..4)
    dflash = 5,            // the DFlash physical ring cells (part 0 only, stage 0, index 0)
    dflash_pair = 6,       // a checkpoint's PAIRED ring image at its own boundary (part 0, stage 0,
                           // index = the checkpoint ordinal); the manifest's checkpoint meta carries
                           // the exact counters captured with it
};
const char* session_region_kind_name(int32_t kind);   // "live_state", ... ; "unknown" for a kind v2 does not define
// the part count of the state and K/V regions (gdn/ple/tails/dead/block_pos, k/v/k_scale/v_scale/pooled)
constexpr int64_t kSessionStateParts = 5, kSessionKvParts = 5;
constexpr uint64_t kSessionMaxStages = 64;   // a v2 file's logical stage count, bounded before anything is read

// One bulk region: what it is, where its bytes sit in the file's region block, how long they are, and their
// checksum (the trailer's, filled by the write; a read compares against it and reports the verified values back
// on each region).  Offsets are in canonical order (kind, stage, index, part) and tile the region block with no
// gap and no overlap.
struct SessionRegion {
    int32_t kind = 0;      // SessionRegionKind
    int64_t stage = 0;     // 0: the first logical stage, >0: a later one
    int64_t index = 0;     // kv/mtp: the layer ordinal within its stage; checkpoint_state: the checkpoint ordinal; else 0
    int64_t part = 0;      // 0..4 for the state and K/V regions; 0 for dflash
    uint64_t offset = 0, bytes = 0, checksum = 0;
    bool operator==(const SessionRegion&) const = default;
};

// One logical stage of the layout the bytes belong to: its layer carve and the KV format its cells were written
// in.  A v2 file is bound to the whole ordered stage list, not only to the first stage's range.
struct SessionStageLayout {
    int64_t layer_lo = 0, layer_hi = 0, kv_format = 0;
    bool operator==(const SessionStageLayout&) const = default;
};
struct SessionLayoutIdentity {
    std::array<int64_t, 18> geometry{};
    std::vector<SessionStageLayout> stages;   // stage order; the first stage is stages[0]
    bool operator==(const SessionLayoutIdentity&) const = default;
};

// The drafter the bytes belong to: its type and the fingerprint of the loaded artifact (plain data: the engine
// fills the real values; the file only compares them), the ring layout the saved cells are indexed by, and the
// EXACT host counters the capture took.  committed_end < 0: no draft state in this file.  The counters are kept
// as they are, never normalized to the token length or to each other (appended_through > committed_end is legal
// existing state: rejected speculative rows are part of the ring's horizon).
struct SessionDraftIdentity {
    int64_t type = 0, ring_cells = 0, window = 0, capacity = 0, committed_end = -1, appended_through = -1;
    uint64_t artifact = 0;
    bool operator==(const SessionDraftIdentity&) const = default;
};

// A checkpoint as the manifest stores it: a prefix LENGTH into the file's single token vector (the ids are not
// repeated per checkpoint), the images it covers (the shared image keys below that length), its LRU stamp, its
// pin, how many later-stage parts its state is split into, and the --prefill-pipe plan a continuation reads with
// (integers and the pipe value's exact bits, not a struct dump).
struct SessionCheckpointMeta {
    uint64_t prefix = 0;
    uint64_t used = 0;
    bool pinned = false;
    int64_t stage_parts = 0;   // 0: one part (no split); else the number of LATER stages this checkpoint covers
    ConversationCheckpoint::PromptPlan plan;
    // The paired draft capture at this checkpoint's boundary: the ring image the file carries as
    // a dflash_pair region of `ring_bytes` bytes (checksum `ring_checksum`), and the EXACT
    // counters that capture took. ring_id 0: target-only (no pair, no region). A pair is legal
    // only with ring_committed_end == prefix (captured AT this position), its ring layout equal
    // to the file's draft, and appended_through >= committed_end inside the capacity - the
    // counters are kept as captured, never normalized.
    uint64_t ring_id = 0, ring_bytes = 0, ring_checksum = 0;
    int64_t ring_cells = 0, ring_window = 0, ring_capacity = 0, ring_committed_end = -1,
            ring_appended_through = -1;
};

// Everything a v2 file declares about a conversation, in bounded metadata: the identities it is bound to, the
// token ids and image keys each stored ONCE, the checkpoint prefix references, and the region directory.
struct SessionManifestV2 {
    SessionLayoutIdentity layout;
    SessionDraftIdentity draft;
    bool cvec = true;
    std::vector<int32_t> tokens;
    std::vector<ConversationImageKey> images;
    std::vector<SessionCheckpointMeta> checkpoints;
    std::vector<SessionRegion> regions;
};

// A v2 region's bytes, read from whoever owns them (a device pool, a pinned host pool) straight into the file's
// staging buffer: no host copy of the region, and the source is not the file's owner.  `read(offset, dst, n)`
// fills [offset, offset+n) of the region; after a failed read `error()` (optional) says why.  The source must
// stay unchanged until the file is published.
struct SessionRegionSource {
    int32_t kind = 0;
    int64_t stage = 0, index = 0, part = 0;
    uint64_t size = 0;   // the source's own extent; it must equal the manifest's declared length
    std::function<bool(size_t offset, void* dst, size_t n)> read;
    std::function<std::string()> error;
};

// Writes a v2 generation.  `manifest` declares the identities, the token/image directory, the checkpoints and the
// region directory (its regions' offsets/checksums are filled by the write, so callers leave them zero); `sources`
// supplies exactly one source per declared region, matched by kind/stage/index/part and by size.  The declared
// region order is canonicalized (kind, stage, index, part) with its sources, so a caller's order is not part of
// the format.  Refusals (inconsistent manifest, a source that does not match, an over-large state) happen before
// any disk access; the publication machinery, progress, phases and free-space preflight are v1's.
bool session_file_write_v2(const std::string& path, const SessionFileIdentity& id, const SessionManifestV2& manifest,
                           const std::vector<SessionRegionSource>& sources, size_t& bytes, std::string& error,
                           const SessionWriteOptions& options = {}, SessionStatus* status = nullptr);

// Where the streamed regions go.  The read calls validate_region() for EVERY region in its validation pass,
// before a single byte of any region is written; write() runs only in the transfer pass.  So a checksum failure
// anywhere in the payload, a foreign identity, a region the destination cannot take, a truncated file and an
// admission refusal all leave the sink with nothing written.  Blocks are at most 16 MiB, region by region, in the
// manifest's order; finish() runs after every region has landed and the checksums matched.
struct SessionRegionSink {
    std::function<bool(const SessionRegion& region, std::string& why)> validate_region;
    std::function<bool(const SessionRegion& region, uint64_t offset, const void* bytes, size_t n, std::string& why)> write;
    std::function<bool(std::string& why)> finish;
};

// What the reader requires the file to have been written with.  A layout or draft identity mismatch is refused
// before the sink sees a byte; a check a caller leaves off is the caller's own documented decision.
struct SessionV2Requirement {
    bool check_layout = true, check_draft = true;
    SessionLayoutIdentity layout;
    SessionDraftIdentity draft;
};

// Bounds of a v2 read.  Every count and extent is checked against the bytes left in the payload and against these
// limits BEFORE anything is allocated; the manifest and the region directory are the only things a v2 read
// allocates, so its peak memory does not grow with the conversation.
struct SessionReadV2Limits {
    uint64_t max_file_bytes = UINT64_MAX;
    uint64_t max_manifest_bytes = 64u << 20;   // the manifest is metadata; it is never a payload
    uint64_t max_tokens = UINT64_MAX, max_images = UINT64_MAX, max_checkpoints = UINT64_MAX;
    uint64_t max_stages = kSessionMaxStages, max_regions = UINT64_MAX, max_region_bytes = UINT64_MAX;
    // the RAM PREFLIGHT: asked once the header and identity match and before the manifest is read, with
    // session_read_v2_peak_bytes; false refuses the file.  Not a reservation.
    std::function<bool(uint64_t need_bytes, std::string& why)> admit;
    SessionProgress progress;
    // the transfer mode of the opened file, reported once its header has been read (and again mid-read if a
    // filesystem refuses the aligned transfer and the reader drops to buffered).  Optional, as in the write.
    std::function<void(bool direct)> mode;
};
// What the RAM preflight is asked for: the manifest, the region directory, one 16 MiB transfer buffer (shared by
// both passes) and the small vectors.  Independent of the payload length.
uint64_t session_read_v2_peak_bytes(uint64_t manifest_bytes, uint64_t regions);

// Reads a v2 generation through `sink`.  `manifest` receives what the file declares (identities, tokens, images,
// checkpoints, region descriptors with their offsets and checksums) and is left untouched on failure.  Pass 1
// streams the regions and verifies their checksums, calling validate_region and reporting progress; pass 2
// streams them again from the same opened file into the sink.  With `sink == nullptr` only the validation pass
// runs: the file is verified and nothing is written anywhere.  A read with a sink moves the file's region bytes
// twice, so its progress counts both passes and ends at the file size plus the region bytes; a validation-only
// read reports out of the file size.  The file is opened and validated as v1's read does (no symlink followed, one
// name, regular file), and the same kinds of failure are reported.
bool session_file_read_v2(const std::string& path, const SessionFileIdentity& id, const SessionV2Requirement& want,
                          SessionManifestV2& manifest, size_t& bytes, std::string& error,
                          const SessionReadV2Limits& limits = {}, SessionRegionSink* sink = nullptr,
                          SessionStatus* status = nullptr);

// A checkpoint's paired ring capture as the engine stores it at the checkpoint boundary: a
// bounded sidecar in the disk tier's folder - header (magic STRDRIN2, the EXACT counters, the
// captured token extent, the payload checksum) then the ring bytes in the same state/part order
// the v2 dflash region uses.  It is the capture-time home of the pair (the published generation
// packs the same bytes into its dflash_pair region); a rewind reads it back.  The read refuses a
// missing/truncated/foreign file, counters that are not `want`'s, an extent that is not
// `tokens`, and a checksum mismatch, leaving `out` untouched every time - a safe miss, never a
// partial image.  The sidecar is the engine's own bounded file (ring-sized, capped at 64 MiB).
bool session_ring_sidecar_write(const std::string& path, const DFlashSnapshotMeta& meta, int64_t tokens,
                                const void* bytes, uint64_t n, std::string& error);
bool session_ring_sidecar_read(const std::string& path, const DFlashSnapshotMeta& want, int64_t tokens,
                               std::vector<uint8_t>& out, std::string& error);

} // namespace strata::core

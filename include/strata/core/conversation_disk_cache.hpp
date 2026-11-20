// include/strata/core/conversation_disk_cache.hpp - the host-side policy of the automatic DISK conversation tier.
//
// Everything here is plain host code: the index of published generations, the quota that prices a write BEFORE it
// starts, the publication order, and the restart inventory that rebuilds the index from committed manifests without
// reading a payload.  It knows no device, no session, and no K/V: the bytes a generation holds are its file, and the
// engine streams them with `session_file_write_v2` / `session_file_read_v2` (conversation_file.hpp).  Keeping the
// policy here keeps it testable on the CPU and out of the serve loop.
//
// The rules (plan 6.1-6.3):
//   * the index holds ONE entry per published generation: its token ids once, its image keys, its cvec, the
//     checkpoint prefix lengths with their pin/use stamps and pair ids, the layout/draft identity, the file path,
//     the byte totals, and the sidecars it references.  No payload body is ever indexed.
//   * the index is bounded: its own byte cap (inside --conversation-cache-disk-ram-mib) and an entry-count bound.
//     Metadata that cannot fit is skipped, never allocated past the cap.
//   * reserve before writing: the complete planned write (the file's exact bytes, the pair bodies it packs, the
//     index entry, the atomic replacement's overhead) is priced first, against published generations + this
//     reservation + the protected incoming donor, and against the filesystem's free space plus the free-space floor.
//     The oldest eligible unpinned generation goes to fit; a guarded, referenced or pinned generation never does,
//     and when everything left is protected the new cache write is skipped.
//   * publish: temporary file, file flush, atomic rename, directory flush (conversation_file's machinery), and the
//     index changes only AFTER the rename.  Superseded generations retire then, never before a write that may fail.
//   * the startup inventory rebuilds the index from committed manifests ONLY, validates the identities it can check,
//     and reconciles orphan temporaries, orphan sidecars and interrupted publications from the folder's contents.
//     Cleanup touches only this namespace's own files.
#pragma once

#include "strata/core/conversation_cache.hpp"
#include "strata/core/conversation_file.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace strata::core {

// One published generation exactly as the index holds it: metadata, no payload.  `guarded`/`referenced` are the
// runtime protections eviction and retirement respect; they are never part of a published file.
struct DiskGeneration {
    uint64_t serial = 0;                 // never reused
    std::string path;                    // the tier's own file
    std::vector<int32_t> tokens;         // the conversation's tokens, stored ONCE
    std::vector<ConversationImageKey> images;
    bool cvec = true;
    uint64_t bytes = 0;                  // the published file's size
    std::vector<uint64_t> ring_ids;      // the pair sidecars its checkpoint metas reference
    // EVERY checkpoint prefix the published manifest holds, in its own order: the lookup's candidate list, so an
    // eligible checkpoint answers even when the deepest scalars below are longer than the prompt.  Both paths that
    // build an entry fill it from the manifest's metas - `scan()` at the restart inventory, the park's publish at
    // the runtime one - and its bytes are priced into disk_generation_index_bytes, like everything else here.
    std::vector<int64_t> ckpt_prefixes;
    int64_t anchor = 0;                  // deepest checkpoint prefix (the target anchor)
    int64_t pair_prefix = 0;             // deepest checkpoint prefix with a legal pair (0: none)
    bool tail_draft = false;             // the generation's own ring horizon retains a resume at its end
    uint64_t used = 0;                   // use stamp: the LRU order (a lookup hit stamps it)
    bool pinned = false;                 // a pinned root: eviction never takes the only pinned one
    bool dir_flush_pending = false;      // published; the folder flush after the rename failed (lane 1's case)
    bool guarded = false;                // the incoming donor a restore is mounting
    bool referenced = false;             // its file is open: nothing evicts or removes it
};

// The result of a disk lookup: the generation and the ONE prefix a restore mounts.  `selected` is 0 when nothing
// matches; otherwise it is the deepest strictly eligible prefix of the prompt - the generation's full length, or one
// of the checkpoint prefixes that generation really publishes.  A checkpoint's PAIR is deliberately NOT decided
// here: whether that checkpoint's ring is this drafter's and still retains the resume is a fact of the published
// manifest, and only the restore - which validates the manifest, the region directory and the checksums - can say
// so.  `pair_prefix`/`anchor` keep their old meaning for their own consumers (the metrics, the sidecar reclaim);
// the lookup never consults them instead of the checkpoint list.
struct DiskBestMatch {
    const DiskGeneration* gen = nullptr;
    int64_t selected = 0;                // the prefix length to restore (< prompt.size(), <= tokens.size())
};

// The index's bound for one entry: its vectors' bytes, its path, and the fixed cost of the entry itself.
uint64_t disk_generation_index_bytes(const DiskGeneration& g);

// What the startup scan found.  `corrupt`/`foreign` generations are counted and removed: they can never be read
// back, and leaving them charged to the quota would evict usable generations for nothing.
struct DiskCacheInventory {
    size_t generations = 0, sidecars = 0, orphan_temps = 0, orphan_sidecars = 0, corrupt = 0, foreign = 0;
    uint64_t bytes = 0, sidecar_bytes = 0;
    std::string last_refusal;            // why a scanned file was not indexed
};

// The concise reason codes a reservation gives back (the metrics card reports these, never prompt contents).
enum class DiskRefusal : int { none = 0, quota, space, index, count, protected_, io };
const char* disk_refusal_name(DiskRefusal r);   // "quota", "free-space", "index", "count", "protected", "io"

// A priced write.  It holds the tier's space from reserve() until publish() (which consumes it) or release().
struct DiskReservation {
    uint64_t file_bytes = 0;         // the exact published size of the generation
    uint64_t sidecar_bytes = 0;      // the pair bodies the write packs
    uint64_t index_bytes = 0;        // the index entry it will take
    std::string path;                // the generation path the write publishes to
    uint64_t serial = 0;             // its serial
    DiskRefusal refusal = DiskRefusal::none;
    size_t evicted = 0;              // generations the reservation had to evict to fit
    bool held = false;
};

// The pictures below a conversation's length: the set a lookup must match exactly.
inline void disk_images_below(const std::vector<ConversationImageKey>& all, int64_t n,
                              std::vector<ConversationImageKey>& out) {
    out.clear();
    for (const auto& k : all)
        if (k.start < n) out.push_back(k);
}

// The two lists' pictures below `n`, compared in place: the same answer as comparing disk_images_below(a, n) with
// disk_images_below(b, n) - same entries, same order - without materialising either.  A lookup runs it once per
// candidate prefix of every indexed generation, so the tier never allocates on a request's path for it.
inline bool disk_images_below_equal(const std::vector<ConversationImageKey>& a,
                                    const std::vector<ConversationImageKey>& b, int64_t n) {
    size_t i = 0, j = 0;
    for (;;) {
        while (i < a.size() && a[i].start >= n) ++i;
        while (j < b.size() && b[j].start >= n) ++j;
        if (i == a.size() || j == b.size()) return i == a.size() && j == b.size();
        if (!(a[i] == b[j])) return false;
        ++i;
        ++j;
    }
}

// How many bytes of one region block land when a generation is mounted at a prefix SHORTER than its own length.
// `upto` is that region's size at the mounted prefix - the region's leading `upto` bytes are exactly its cells
// below the prefix in the identity layout, which is how conversation_kv_restore_prefix takes an image's prefix -
// and the restore walks the region in blocks, so the clip is asked per block: of the block [offset, offset+n),
// `disk_prefix_clip(upto, offset, n)` bytes land at `offset` and the rest of the block is consumed and dropped
// (0 when the block starts at or past the prefix).  A restore's KV clip is this arithmetic and nothing else.
inline size_t disk_prefix_clip(uint64_t upto, uint64_t offset, size_t n) {
    if (offset >= upto) return 0;
    return (size_t) std::min<uint64_t>(n, upto - offset);
}

// What the tier reports: the counters the /metrics card reads.  The caller feeds the per-phase transfer numbers
// (its streams know their bytes and times); the tier feeds the quota, index and decision counters.
struct DiskCacheStats {
    uint64_t index_high = 0, transfer_high = 0;
    bool direct_io = true;
    uint64_t write_bytes = 0, write_ms = 0;
    uint64_t validate_bytes = 0, validate_ms = 0;
    uint64_t transfer_bytes = 0, transfer_ms = 0;
    uint64_t flush_publish_ms = 0;
    uint64_t parks = 0, restores = 0, hits = 0, misses = 0, evictions = 0, superseded = 0;
    uint64_t refusals = 0, incompatible = 0, corrupt = 0, draft_pair_hits = 0, draft_rebuilds = 0,
             ring_retention_misses = 0;
    uint64_t reused_tokens = 0, evaluated_tokens = 0;
    // --conversation-cache-evict-page-cache: the tier's own page-cache releases (posix_fadvise DONTNEED after a
    // park write, a restore read, a sidecar capture/rewind).  The bytes are the file sizes it released.
    uint64_t pages_released = 0, pages_released_bytes = 0;
    uint64_t last_serial = 0;
    DiskRefusal last_refusal = DiskRefusal::none;
};

struct DiskCacheOptions {
    std::string dir;                 // the tier's private folder (created 0700)
    uint64_t budget = 0;             // --conversation-cache-disk-mib
    uint64_t index_cap = 0;          // the index's share of --conversation-cache-disk-ram-mib
    size_t max_entries = 0;          // 0: no count bound (the byte bounds still apply)
    uint64_t min_free_bytes = 0;     // --session-min-free-mib: the floor beside the free-space check
    bool direct_io = true;           // the requested transfer mode; a write reporting buffered turns it off
    // tests: the volume's free space (default: the real one) and injected step failures ("remove",
    // "remove_sidecar", "inventory_remove", "owner")
    std::function<bool(uint64_t& avail)> free_space;
    std::function<int(const char* step)> fault;
};

class DiskCache {
public:
    explicit DiskCache(DiskCacheOptions options) : opt_(std::move(options)) {}
    ~DiskCache();

    // The owner lock and the restart inventory: the index is rebuilt from committed manifests ONLY, and the folder's
    // orphan temporaries and unreferenced sidecars are reconciled.  false: the tier cannot run in this folder
    // (another engine owns it, the folder cannot be created, ...); `why` says which.  `want` is the engine's own
    // layout requirement: a generation whose manifest does not match it is not indexed.
    bool open(const SessionFileIdentity& id, const SessionV2Requirement& want, const SessionReadV2Limits& limits,
              std::string& why);

    const std::string& dir() const { return opt_.dir; }
    const DiskCacheInventory& inventory() const { return inv_; }
    DiskCacheStats& stats() { return stats_; }
    const DiskCacheStats& stats() const { return stats_; }
    void set_io_mode(bool direct) { stats_.direct_io = direct; }
    uint64_t budget() const { return opt_.budget; }
    uint64_t used() const { return used_; }
    uint64_t reserved() const { return reserved_; }
    uint64_t index_bytes() const { return index_bytes_; }
    size_t entries() const { return gens_.size(); }
    bool enabled() const { return opt_.budget > 0; }

    // ---- lookup (6.1) ----
    // The deepest prefix of this prompt a restore can mount.  Over every published generation with this cvec, each
    // STRICTLY eligible candidate - the generation's full length, and every checkpoint prefix that generation really
    // publishes - must (a) be this prompt's own first L tokens, (b) lie strictly under the prompt, since a resume
    // leaves at least one token to read (the rule the full-length lookup always had, kept for the checkpoint
    // candidates too), (c) be within the generation's stored token count, and (d) have exactly these pictures below
    // it.  The deepest candidate wins; between two generations at the same depth the newer use stamp wins,
    // compared EXPLICITLY - gens_'s order is the LRU's convenience, not the tie-break - and equal stamps go to the
    // higher serial, so the answer never depends on the folder's read order.  A checkpoint's answer does NOT depend
    // on `anchor` or `pair_prefix`: an eligible checkpoint is found even when both scalars are longer than the
    // prompt (the deep prompt inside a longer published generation) or when a deeper checkpoint owns the pair.
    // `selected` is 0 on no match.  No LRU effect: use() stamps the entry a restore mounts.
    template<class Token>
    DiskBestMatch best(const std::vector<Token>& prompt, const std::vector<ConversationImageKey>& images,
                       bool cvec) const {
        DiskBestMatch out;
        const int64_t n = (int64_t) prompt.size();
        for (const auto& g : gens_) {
            if (g.cvec != cvec) continue;
            const int64_t T = (int64_t) g.tokens.size();
            if (T < 1) continue;
            // A picture at or past the stored tokens is not this generation's own: the file is corrupt (the restore
            // refuses it for the same reason), so none of its candidates is eligible.
            bool own_pictures = true;
            for (const auto& k : g.images)
                if (k.start >= T) { own_pictures = false; break; }
            if (!own_pictures) continue;
            // How far this generation's tokens ARE this prompt's.  Its candidates are nested prefixes of them, so one
            // walk answers the token test for all of them; the old lookup's single comparison cost the same.
            int64_t shared = 0;
            const int64_t cap = std::min(T, n);
            while (shared < cap && (int64_t) g.tokens[(size_t) shared] == (int64_t) prompt[(size_t) shared]) ++shared;
            // The generation's own best candidate.  The full length first (the ordinary resume, unchanged), then any
            // deeper checkpoint that clears the bounds; each is judged on its own picture horizon, so a picture
            // re-encoded after a checkpoint costs only the depth above it - the same fallback the RAM tier runs.
            int64_t L = 0;
            const auto eligible = [&](int64_t p) {
                return p > L && p >= 1 && p <= T && p < n && p <= shared &&
                       disk_images_below_equal(g.images, images, p);
            };
            if (eligible(T)) L = T;
            for (const int64_t p : g.ckpt_prefixes)
                if (eligible(p)) L = p;
            if (L == 0) continue;
            if (out.gen == nullptr || L > out.selected ||
                (L == out.selected && (g.used > out.gen->used ||
                                       (g.used == out.gen->used && g.serial > out.gen->serial)))) {
                out.gen = &g;
                out.selected = L;
            }
        }
        return out;
    }
    // the deepest published generation of THIS conversation - the one a publish of these tokens now supersedes - in
    // tokens: 0 when none.  It is `retire_superseded`'s own rule (the generation's tokens are a PREFIX of these, its
    // pictures below that length exactly these, the same cvec), so it answers "what would a publish now replace",
    // which is what --conversation-cache-park-gain compares against.
    int64_t parked_prefix_tokens(const std::vector<int32_t>& ids, const std::vector<ConversationImageKey>& images,
                                 bool cvec) const;
    // --conversation-cache-park-gain for the DISK tier: false when publishing these tokens now would supersede a
    // generation this conversation advanced fewer than `gain` tokens past.  The published file already covers that
    // prefix and a restart re-prefills only the unpublished tail, so rewriting the whole image buys nothing.
    // Nothing published yet, or gain <= 0: the generation publishes, as before (the rule the RAM park uses).
    bool publish_worth(const std::vector<int32_t>& ids, const std::vector<ConversationImageKey>& images,
                       bool cvec, int64_t gain) const {
        const int64_t held = parked_prefix_tokens(ids, images, cvec);
        return gain <= 0 || held == 0 || (int64_t) ids.size() >= held + gain;
    }
    void use(uint64_t serial);
    void use_stamp(uint64_t stamp) { clock_ = std::max(clock_, stamp); }
    uint64_t clock() const { return clock_; }
    // The generation a restore is mounting (the incoming donor) and any generation whose file is open: eviction
    // and retirement never take a guarded or referenced one.
    void guard(uint64_t serial);
    void unguard(uint64_t serial);
    void reference(uint64_t serial);
    void unreference(uint64_t serial);
    const DiskGeneration* find(uint64_t serial) const;

    // ---- quota (6.3): the complete planned write is priced before any file is opened ----
    // `file_bytes` is session_file_v2_size's exact size, `sidecar_bytes` the pair bodies the write packs, `index_bytes`
    // the entry it will take.  Evicts the oldest eligible unpinned generation as needed; a guarded, referenced, or
    // pinned generation is never evicted, and when nothing eligible is left the reservation is refused with
    // DiskRefusal::protected_ (the write is skipped, inference goes on).
    bool reserve(uint64_t file_bytes, uint64_t sidecar_bytes, uint64_t index_bytes, DiskReservation& out,
                 std::string& why);
    void release(DiskReservation& res);   // the write failed: the reservation goes back
    // The write published at `res.path`: record the entry and take the reservation.  `dir_flush_pending` is the
    // writer's "published, the folder flush failed" case: the file is at its name, the entry is recorded.
    bool publish(DiskReservation& res, DiskGeneration meta, bool dir_flush_pending, std::string& why);

    // #342 on disk: the generations of this conversation the published one outgrew.  Called AFTER the replacement
    // publishes; a guarded or referenced generation stays, and `published` (the serial of the generation that was
    // just written, 0 when the caller names none) is never its own victim.  Returns how many files were removed.
    size_t retire_superseded(const std::vector<int32_t>& ids, const std::vector<ConversationImageKey>& images,
                             bool cvec, uint64_t published = 0);

    // ---- the pair sidecars (lane 2b's captures) ----
    std::string sidecar_path(uint64_t id) const { return opt_.dir + "/ring-" + std::to_string(id) + ".dring"; }
    std::string generation_path(uint64_t serial) const { return opt_.dir + "/conv-" + std::to_string(serial) + ".sess"; }
    void note_sidecar(uint64_t id, uint64_t bytes);
    bool sidecar_bytes(uint64_t id, uint64_t& bytes) const;
    // a capture the disk budget can still take (a capture that would not fit is skipped: its checkpoint stays
    // target-only, said at the capture, and nothing is written)
    bool can_capture_sidecar(uint64_t bytes, std::string& why) const;
    // a sidecar no live checkpoint and no published generation references any more
    size_t reclaim_sidecars(const std::function<bool(uint64_t id)>& referenced, uint64_t& freed);

private:
    bool take_owner(std::string& why);
    void scan(const SessionFileIdentity& id, const SessionV2Requirement& want, const SessionReadV2Limits& limits);
    bool erase(size_t i, std::string& why);   // removes its file and frees its bytes
    void note_refusal(DiskRefusal r, DiskReservation& out, std::string& why);

    DiskCacheOptions opt_;
    std::vector<DiskGeneration> gens_;   // least recently active first
    uint64_t used_ = 0, reserved_ = 0, index_bytes_ = 0, next_serial_ = 1, clock_ = 0, sidecar_bytes_ = 0;
    bool owner_ = false;
    DiskCacheInventory inv_;
    DiskCacheStats stats_;
    std::vector<std::pair<uint64_t, uint64_t>> sidecars_;   // capture id -> ring bytes
};

} // namespace strata::core

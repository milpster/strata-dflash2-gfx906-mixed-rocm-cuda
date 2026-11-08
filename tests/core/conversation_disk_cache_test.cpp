// CPU-only tests for the automatic disk tier's host policy
// (include/strata/core/conversation_disk_cache.hpp): the bounded index and its lookup, the reserve-before-write
// quota with its protections, the publication order, the pair-sidecar reclaim, and the restart inventory that
// rebuilds the index from committed manifests and reconciles the folder's own trash.
// Built with -DSTRATA_BUILD_CONVERSATION_TESTS=ON; no CUDA, no model, no GPU.
#include "strata/core/conversation_disk_cache.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#ifndef _WIN32
#include <unistd.h>
#endif

using namespace strata::core;
namespace fs = std::filesystem;

namespace {
int checks = 0;
void check(bool ok, const char* label) {
    ++checks;
    if (!ok) { std::fprintf(stderr, "FAIL: %s\n", label); std::exit(1); }
}

fs::path g_tmp;
fs::path dir(const char* name) { return g_tmp / name; }

const SessionFileIdentity kId{0x1111222233334444ull, 0x5555666677778888ull};
const SessionFileIdentity kOther{0x9999888877776666ull, 0x5555444433332222ull};

SessionLayoutIdentity layout() {
    SessionLayoutIdentity l;
    for (size_t i = 0; i < l.geometry.size(); ++i) l.geometry[i] = int64_t(100 + i);
    l.stages = {{0, 2, 2}};   // one stage, two K/V layers
    return l;
}
SessionV2Requirement want() {
    SessionV2Requirement w;
    w.check_layout = true;
    w.check_draft = false;   // the tier restores a generation's ring horizon, it never compares it
    w.layout = layout();
    return w;
}
SessionReadV2Limits limits() {
    SessionReadV2Limits l;
    l.max_manifest_bytes = 8u << 20;
    l.max_tokens = 1u << 20;
    l.max_images = 1u << 20;
    l.max_checkpoints = 16;
    l.max_stages = 4;
    l.max_regions = 512;
    l.admit = [](uint64_t, std::string&) { return true; };
    return l;
}

std::vector<uint8_t> data_of(size_t n, uint8_t seed) {
    std::vector<uint8_t> v(n);
    for (size_t i = 0; i < n; ++i) v[i] = uint8_t(i * 131u + seed);
    return v;
}

// One fixture generation: `n` tokens from its own tag's range, optional pictures, the drafter's ring and a
// checkpoint PAIR on demand, a use stamp (the index's LRU order) and a pin.
struct Spec {
    int64_t n = 100;
    int64_t tag = 1;
    std::vector<ConversationImageKey> images;
    bool cvec = true;
    bool draft = false;
    bool pair = false;
    uint64_t pair_bytes = 1u << 13;
    uint64_t used = 1;
    bool pinned = false;
    size_t kv_bytes = 0;   // 0: the small fixture region; a big one fills the writer's block (see 6.3's faults)
};
std::vector<int32_t> tokens_of(const Spec& s) {
    std::vector<int32_t> v((size_t) s.n);
    for (int64_t i = 0; i < s.n; ++i) v[(size_t) i] = int32_t(s.tag * 1000 + i + 1);
    return v;
}
// a prompt that STARTS with this conversation (one token more: a resume has something left to read)
std::vector<int32_t> prompt_of(const Spec& s) {
    std::vector<int32_t> v = tokens_of(s);
    v.push_back(int32_t(s.tag * 1000 + s.n + 1));
    return v;
}

SessionManifestV2 build(const Spec& s, std::vector<std::vector<uint8_t>>& data) {
    SessionManifestV2 m;
    m.layout = layout();
    m.cvec = s.cvec;
    m.tokens = tokens_of(s);
    m.images = s.images;
    const auto L = int32_t(SessionRegionKind::live_state), K = int32_t(SessionRegionKind::kv),
             C = int32_t(SessionRegionKind::checkpoint_state), D = int32_t(SessionRegionKind::dflash),
             P = int32_t(SessionRegionKind::dflash_pair);
    const auto add = [&](int32_t kind, int64_t stage, int64_t index, int64_t part, size_t n, uint8_t seed) {
        SessionRegion r;
        r.kind = kind; r.stage = stage; r.index = index; r.part = part; r.bytes = n;
        m.regions.push_back(r);
        data.push_back(data_of(n, seed));
    };
    const std::array<size_t, 5> live{4096, 64, 128, 32, 8};
    const std::array<size_t, 5> kv{s.kv_bytes ? s.kv_bytes : 512, 64, 16, 16, 128};
    const std::array<size_t, 5> ck{100, 10, 10, 4, 4};
    for (int p = 0; p < 5; ++p) add(L, 0, 0, p, live[p], uint8_t(11 + p));
    for (int layer = 0; layer < 2; ++layer)
        for (int p = 0; p < 5; ++p) add(K, 0, layer, p, kv[p], uint8_t(21 + layer * 5 + p));
    m.checkpoints = {{uint64_t(s.n / 2), s.used, s.pinned, 0, {}}, {uint64_t(s.n), s.used, s.pinned, 0, {}}};
    for (size_t i = 0; i < m.checkpoints.size(); ++i)
        for (int p = 0; p < 5; ++p) add(C, 0, int64_t(i), p, ck[p], uint8_t(41 + i * 5 + p));
    if (s.draft) {
        m.draft = {2, 2112, 2048, 8192, s.n, s.n + 500, 0x5a5a5a5aull};
        add(D, 0, 0, 0, 1u << 12, 77);
    }
    if (s.pair) {
        add(P, 0, 1, 0, s.pair_bytes, 131);
        auto& c = m.checkpoints[1];
        c.ring_id = 0xC0FFEE;
        c.ring_bytes = s.pair_bytes;
        c.ring_checksum = session_hash64(data.back().data(), data.back().size(), 0);
        c.ring_cells = m.draft.ring_cells;
        c.ring_window = m.draft.window;
        c.ring_capacity = m.draft.capacity;
        c.ring_committed_end = s.n;
        c.ring_appended_through = m.draft.appended_through;
    }
    return m;
}

std::vector<SessionRegionSource> sources_of(const SessionManifestV2& m, const std::vector<std::vector<uint8_t>>& data) {
    std::vector<SessionRegionSource> out;
    for (size_t i = 0; i < m.regions.size(); ++i) {
        const auto& r = m.regions[i];
        const std::vector<uint8_t>* d = &data[i];
        SessionRegionSource s;
        s.kind = r.kind; s.stage = r.stage; s.index = r.index; s.part = r.part; s.size = d->size();
        s.read = [d](size_t off, void* dst, size_t n) {
            if (off + n > d->size()) return false;
            std::memcpy(dst, d->data() + off, n);
            return true;
        };
        s.error = [] { return std::string("fixture source"); };
        out.push_back(std::move(s));
    }
    return out;
}

struct Parked {
    std::string path;
    uint64_t serial = 0;
    uint64_t bytes = 0;
};

// The park leg of plan 6.1-6.3 for one fixture conversation: plan -> the exact size -> reserve -> write ->
// publish.  `publish == false` stops after the rename (the crash between publication and the index).
bool park(DiskCache& c, const Spec& s, std::string& why, Parked* out = nullptr,
          const SessionWriteOptions& wo = {}, bool publish = true) {
    std::vector<std::vector<uint8_t>> data;
    const SessionManifestV2 m = build(s, data);
    uint64_t exact = 0;
    if (!session_file_v2_size(m, exact, why)) return false;
    DiskGeneration meta;
    meta.tokens = tokens_of(s);
    meta.images = s.images;
    meta.cvec = s.cvec;
    meta.bytes = exact;
    meta.pinned = s.pinned;
    for (const auto& cm : m.checkpoints) {
        meta.anchor = std::max<int64_t>(meta.anchor, (int64_t) cm.prefix);
        if (cm.ring_id) {
            meta.pair_prefix = std::max<int64_t>(meta.pair_prefix, (int64_t) cm.prefix);
            meta.ring_ids.push_back(cm.ring_id);
        }
    }
    meta.tail_draft = s.draft;
    DiskReservation res;
    if (!c.reserve(exact, 0, disk_generation_index_bytes(meta), res, why)) return false;
    size_t bytes = 0;
    SessionStatus st;
    const bool wrote = session_file_write_v2(res.path, kId, m, sources_of(m, data), bytes, why, wo, &st);
    const bool dir_pending = !wrote && st.published;
    if (!wrote && !dir_pending) {
        c.release(res);
        return false;
    }
    if (out) {
        out->path = res.path;
        out->serial = res.serial;
        out->bytes = wrote ? (uint64_t) bytes : exact;
    }
    if (!publish) return true;
    meta.bytes = wrote ? (uint64_t) bytes : exact;
    return c.publish(res, std::move(meta), dir_pending, why);
}

// A capture's sidecar: the file the checkpoint-boundary capture wrote, plus the tier's booking of it
void note_sidecar_file(DiskCache& c, uint64_t id, size_t bytes) {
    std::ofstream f(c.sidecar_path(id), std::ios::binary | std::ios::trunc);
    const std::vector<uint8_t> d = data_of(bytes, uint8_t(id));
    f.write((const char*) d.data(), (std::streamsize) d.size());
    f.close();
    c.note_sidecar(id, bytes);
}

DiskCacheOptions opts(const fs::path& d, uint64_t budget, uint64_t index_cap = 4u << 20, size_t max_entries = 0,
                      uint64_t min_free = 0) {
    DiskCacheOptions o;
    o.dir = d.string();
    o.budget = budget;
    o.index_cap = index_cap;
    o.max_entries = max_entries;
    o.min_free_bytes = min_free;
    o.direct_io = true;
    return o;
}
bool open(DiskCache& c, std::string& why) { return c.open(kId, want(), limits(), why); }
bool is_refused(DiskRefusal r, const char* name) { return std::strcmp(disk_refusal_name(r), name) == 0; }

// ============ 6.1: the index, the lookup, the tie and the supersession order ============
void test_lookup_and_supersession() {
    const fs::path d = dir("lookup");
    std::string why;
    DiskCache c(opts(d, 256u << 20));
    check(open(c, why), "lookup: open");
    const Spec a{100, 1, {}, true, false, false, 0, 1, false};
    const Spec b{150, 1, {}, true, false, false, 0, 2, false};
    Parked pa, pb, pa2;
    check(park(c, a, why, &pa), "lookup: park A");
    check(park(c, b, why, &pb), "lookup: park B");
    check(c.entries() == 2 && c.used() == pa.bytes + pb.bytes, "lookup: both generations are charged");
    const DiskGeneration* g = c.best(prompt_of(b), {}, true);
    check(g != nullptr && g->serial == pb.serial, "lookup: the deeper generation wins");
    g = c.best(prompt_of(a), {}, true);
    check(g != nullptr && g->serial == pa.serial, "lookup: A answers its own prompt");
    check(c.best(tokens_of(a), {}, true) == nullptr, "lookup: a prompt equal to a generation is no resume");
    check(c.best(prompt_of(b), {}, false) == nullptr, "lookup: cvec must match");
    const Spec ai{100, 2, {{10, 0x1111ull}}, true, false, false, 0, 3, false};
    Parked pai;
    check(park(c, ai, why, &pai), "lookup: park a conversation with a picture");
    check(c.best(prompt_of(ai), {{10, 0x2222ull}}, true) == nullptr, "lookup: a different picture is no match");
    check(c.best(prompt_of(ai), {{10, 0x1111ull}}, true)->serial == pai.serial, "lookup: the picture matches");
    check(c.best(prompt_of(ai), {{10, 0x1111ull}, {150, 0x3333ull}}, true) != nullptr,
          "lookup: a picture past its length is not part of the match");
    check(c.best(prompt_of(ai), {{10, 0x1111ull}, {100, 0x3333ull}}, true) != nullptr,
          "lookup: a picture at its length is not below it");
    // parking A again retires the generation it outgrew - only AFTER the replacement published (plan 6.3)
    check(park(c, a, why, &pa2), "lookup: park A again");
    check(c.entries() == 4 && fs::exists(pa.path), "lookup: the older generation is still published");
    check(c.retire_superseded(tokens_of(a), {}, true, pa2.serial) == 1, "lookup: the superseded generation retires");
    check(c.retire_superseded(tokens_of(a), {}, true, pa2.serial) == 0, "lookup: the published generation is never its own victim");
    check(!fs::exists(pa.path) && fs::exists(pa2.path) && fs::exists(pb.path), "lookup: only its file goes");
    check(c.entries() == 3 && c.stats().superseded == 1, "lookup: the index holds the live generations");
    check(c.best(prompt_of(a), {}, true)->serial == pa2.serial, "lookup: the newest copy answers");
    check(c.parked_prefix_tokens(tokens_of(a), {}, true) == 100, "lookup: this conversation's parked prefix");
    check(c.parked_prefix_tokens(tokens_of(b), {}, true) == 150, "lookup: the longer conversation's prefix");
    check(c.parked_prefix_tokens(tokens_of(ai), {}, true) == 0, "lookup: a conversation with pictures is not this one");
    check(c.find(pb.serial) != nullptr && c.find(pa.serial) == nullptr, "lookup: the retired serial is gone");
}

// A folder is one engine's: the owner lock keeps a second out and a dead owner's is taken over.
void test_owner_lock() {
    const fs::path d = dir("owner");
    std::string why;
    {
        DiskCache a(opts(d, 64u << 20));
        DiskCache b(opts(d, 64u << 20));
        check(open(a, why), "owner: the first engine opens");
        check(fs::exists(d / "owner.lock"), "owner: the lock is in the folder");
        check(!open(b, why) && why.find("another engine") != std::string::npos, "owner: a live owner refuses a second");
        check(b.entries() == 0, "owner: the refused cache indexed nothing");
    }   // a.~DiskCache released the lock
    DiskCache c(opts(d, 64u << 20));
    check(open(c, why), "owner: a released lock is taken");
}

// ============ 6.3: the quota, the eviction order and the protections ============
// Three published generations in their own folder, oldest first by use stamp; `pin` marks the OLDEST pinned.
struct Fixtures {
    uint64_t per = 0;
    std::string p1, p2, p3;
    uint64_t s1 = 0, s2 = 0, s3 = 0;
};
Fixtures fixtures(const fs::path& d, std::string& why, bool pin_oldest) {
    Fixtures f;
    DiskCache c(opts(d, 256u << 20));
    check(open(c, why), "quota: the fixture cache opens");
    Parked a, b, e;
    check(park(c, Spec{100, 1, {}, true, false, false, 0, 1, pin_oldest}, why, &a), "quota: park the first");
    check(park(c, Spec{100, 2, {}, true, false, false, 0, 2, false}, why, &b), "quota: park the second");
    check(park(c, Spec{100, 3, {}, true, false, false, 0, 3, false}, why, &e), "quota: park the third");
    f.per = a.bytes;
    f.p1 = a.path; f.p2 = b.path; f.p3 = e.path;
    f.s1 = a.serial; f.s2 = b.serial; f.s3 = e.serial;
    check(f.per > 0 && c.used() == 3 * f.per, "quota: the three files are charged");
    return f;
}
void test_quota_and_protections() {
    std::string why;
    {
        // the reservation prices the complete write (the published file and the writer's temporary), so a
        // budget with room for the three files has no room for a fourth write until the oldest go
        const fs::path d = dir("quota-evict");
        const Fixtures f = fixtures(d, why, false);
        DiskCache c(opts(d, 3 * f.per));
        check(open(c, why), "quota: the tight cache indexes all three");
        check(c.entries() == 3 && c.used() == 3 * f.per, "quota: the restart charges the files it found");
        DiskReservation res;
        check(c.reserve(f.per, 0, 64, res, why), "quota: the write fits only by evicting");
        check(res.evicted == 2, "quota: the oldest generations went to fit");
        check(!fs::exists(f.p1) && !fs::exists(f.p2) && fs::exists(f.p3), "quota: the OLDEST eligible files went");
        check(c.entries() == 1 && c.used() == f.per, "quota: their bytes came back to the quota");
        check(c.stats().evictions == 2, "quota: the evictions are counted");
        c.release(res);
        check(c.reserved() == 0, "quota: a released reservation is gone");
    }
    {
        // every generation protected - the incoming donor guarded, a file open - refuses the write, and
        // nothing on disk changes
        const fs::path d = dir("quota-protect");
        const Fixtures f = fixtures(d, why, false);
        DiskCache c(opts(d, 3 * f.per));
        check(open(c, why), "protect: the cache indexes the three");
        c.guard(f.s1);
        c.guard(f.s2);
        c.reference(f.s3);
        DiskReservation r;
        check(!c.reserve(f.per, 0, 64, r, why) && is_refused(r.refusal, "protected"),
              "protect: a guarded and referenced index refuses the write");
        check(c.entries() == 3 && fs::exists(f.p1) && fs::exists(f.p2) && fs::exists(f.p3),
              "protect: protection evicted nothing");
        check(c.reserved() == 0 && c.stats().last_refusal == DiskRefusal::protected_,
              "protect: nothing is held and the refusal is reported");
        // use() reorders the LRU: the generation a request mounted is the last to go
        c.unguard(f.s1);
        c.unguard(f.s2);
        c.unreference(f.s3);
        c.use(f.s1);
        DiskReservation r2;
        check(c.reserve(f.per, 0, 64, r2, why), "lru: a write fits after the LRU moves");
        check(!fs::exists(f.p2) && fs::exists(f.p1), "lru: the freshly mounted generation stayed");
        r2.held = false;
    }
    {
        // the pinned root is never the victim: the unpinned ones go around it
        const fs::path d = dir("quota-pin");
        const Fixtures f = fixtures(d, why, true);
        DiskCache c(opts(d, 3 * f.per));
        check(open(c, why), "pin: the cache with a pinned root opens");
        check(c.find(f.s1) != nullptr && c.find(f.s1)->pinned, "pin: the pin travels with the published file");
        DiskReservation r;
        check(c.reserve(f.per, 0, 64, r, why), "pin: the write fits once the unpinned ones go");
        check(fs::exists(f.p1) && !fs::exists(f.p2) && !fs::exists(f.p3),
              "pin: the pinned root stayed, the unpinned ones went");
        r.held = false;
    }
    {
        // retirement respects the same protections
        const fs::path d = dir("quota-retire");
        const Fixtures f = fixtures(d, why, false);
        DiskCache c(opts(d, 256u << 20));
        check(open(c, why), "retire: the cache opens");
        c.guard(f.s1);
        check(c.retire_superseded(tokens_of(Spec{100, 1, {}, true, false, false, 0, 1, false}), {}, true) == 0,
              "retire: the guarded donor is never retired");
        check(fs::exists(f.p1), "retire: its file stays");
        c.unguard(f.s1);
        check(c.retire_superseded(tokens_of(Spec{100, 1, {}, true, false, false, 0, 1, false}), {}, true) == 1,
              "retire: once released it retires");
        check(!fs::exists(f.p1), "retire: its file goes");
        check(c.retire_superseded(tokens_of(Spec{100, 4, {}, true, false, false, 0, 9, false}), {}, true) == 0,
              "retire: an unrelated conversation retires nothing");
    }
}

// ============ the volume's free space, the index cap and the entry count ============
// One folder per scenario: each block parks its own fixture generation so it starts from a known index.
void test_space_index_and_count() {
    std::string why;
    const Spec s{100, 1, {}, true, false, false, 0, 1, false};
    {
        const fs::path d = dir("space-ok");
        DiskCacheOptions o = opts(d, 256u << 20, 4u << 20, 0, 4096u << 20);
        o.free_space = [](uint64_t& avail) { avail = 3000u << 20; return true; };
        DiskCache c(o);
        check(open(c, why), "space: open");
        Parked p;
        check(park(c, s, why, &p), "space: a write the volume can take");
    }
    const fs::path full = dir("space-floor");
    {
        DiskCacheOptions o = opts(full, 256u << 20, 4u << 20, 0, 4096u << 20);
        o.free_space = [](uint64_t& avail) { avail = 3000u << 20; return true; };
        DiskCache c(o);
        check(open(c, why), "space: the folder is prepared with one generation");
        Parked p;
        check(park(c, s, why, &p), "space: park the fixture");
    }
    {
        DiskCacheOptions o = opts(full, 256u << 20, 4u << 20, 0, 4096u << 20);
        o.free_space = [](uint64_t& avail) { avail = 1u << 20; return true; };
        DiskCache c(o);
        check(open(c, why), "space: reopen with a full volume");
        DiskReservation r;
        check(!c.reserve(64u << 20, 0, 64, r, why) && is_refused(r.refusal, "free-space"),
              "space: the volume's own free space refuses");
        check(why.find("floor") != std::string::npos, "space: the refusal names the floor");
        check(c.reserved() == 0 && c.entries() == 1, "space: nothing was reserved or evicted");
    }
    {
        DiskCacheOptions o = opts(full, 256u << 20, 4u << 20, 0, 4096u << 20);
        o.free_space = [](uint64_t&) { return false; };
        DiskCache c(o);
        check(open(c, why), "space: reopen with an unreadable volume");
        DiskReservation r;
        check(!c.reserve(1024, 0, 64, r, why) && is_refused(r.refusal, "io"), "space: an unreadable volume refuses");
    }
    const fs::path idx = dir("index-cap");
    {
        DiskCache c(opts(idx, 256u << 20));
        check(open(c, why), "index: the folder is prepared with one generation");
        Parked p;
        check(park(c, s, why, &p), "index: park the fixture");
    }
    {
        // the cap is the index's own bound: an entry that would take it past the cap evicts an eligible
        // generation, and with every entry protected the same write is refused with "index"
        DiskCache c(opts(idx, 256u << 20, 4u << 20));
        check(open(c, why), "index: open");
        check(c.entries() == 1, "index: the earlier fixture is indexed");
        const uint64_t first = c.best(prompt_of(s), {}, true)->serial;
        c.guard(first);
        DiskReservation r;
        check(!c.reserve(1u << 20, 0, (4u << 20) + 1, r, why) && is_refused(r.refusal, "index"),
              "index: over the cap with nothing eligible refuses");
        c.unguard(first);
        DiskReservation r2;
        check(c.reserve(1u << 20, 0, 4u << 20, r2, why) && r2.evicted == 1,
              "index: over the cap evicts an eligible generation");
        check(c.index_bytes() == 0, "index: the evicted entry's bytes came back");
        c.release(r2);
    }
    const fs::path cnt = dir("count-cap");
    {
        DiskCache c(opts(cnt, 256u << 20));
        check(open(c, why), "count: the folder is prepared with one generation");
        Parked p;
        check(park(c, s, why, &p), "count: park the fixture");
    }
    {
        DiskCache c(opts(cnt, 256u << 20, 4u << 20, 1));
        check(open(c, why), "count: open with one entry of room");
        const uint64_t first = c.best(prompt_of(s), {}, true)->serial;
        c.guard(first);
        DiskReservation r;
        check(!c.reserve(1024, 0, 64, r, why) && is_refused(r.refusal, "count"),
              "count: the count bound refuses with the entry protected");
        c.unguard(first);
        DiskReservation r2;
        check(c.reserve(1024, 0, 64, r2, why) && r2.evicted == 1, "count: the bound evicts an eligible entry");
        c.release(r2);
    }
}

// ============ 6.3: a failed write never publishes; the folder flush after the rename does ============
void test_publication_and_faults() {
    const fs::path d = dir("publish");
    std::string why;
    const auto files_of = [&](const fs::path& p) {
        size_t n = 0;
        for (const auto& e : fs::directory_iterator(p))
            if (e.path().filename().string() != "owner.lock") ++n;
        return n;
    };
    Parked pending, crash;
    // the writer injects its "write" fault when a full block reaches the OS, so the fixture is block-sized
    const size_t kBig = 20u << 20;
    const Spec s{100, 1, {}, true, false, false, 0, 1, false, kBig};
    {
        DiskCache c(opts(d, 256u << 20));
        check(open(c, why), "publish: open");
        uint64_t exact = 0;
        {
        std::vector<std::vector<uint8_t>> data;
        const SessionManifestV2 m = build(s, data);
        check(session_file_v2_size(m, exact, why), "publish: the planned size is known");
        check(exact > (uint64_t) kBig, "publish: the planned size fills a writer block");
    }
    // ENOSPC at the first block: the reservation goes back, the folder keeps only the lock
    SessionWriteOptions wo;
    wo.fault = [](const char* step) { return std::strcmp(step, "write") == 0 ? ENOSPC : 0; };
    check(!park(c, s, why, nullptr, wo), "publish: ENOSPC fails the write");
    check(c.entries() == 0 && c.used() == 0 && c.reserved() == 0, "publish: the quota is back after ENOSPC");
    check(files_of(d) == 0, "publish: a failed write leaves no file of ours");
    wo.fault = [](const char* step) { return std::strcmp(step, "file_flush") == 0 ? EIO : 0; };
    check(!park(c, Spec{100, 2, {}, true, false, false, 0, 2, false, kBig}, why, nullptr, wo), "publish: the flush fails");
    check(c.entries() == 0 && c.reserved() == 0 && files_of(d) == 0, "publish: a flush failure publishes nothing");
    wo.fault = [](const char* step) { return std::strcmp(step, "rename") == 0 ? ENOSPC : 0; };
    check(!park(c, Spec{100, 3, {}, true, false, false, 0, 3, false, kBig}, why, nullptr, wo), "publish: the rename fails");
    check(c.entries() == 0 && c.reserved() == 0 && files_of(d) == 0, "publish: a rename failure publishes nothing");
    // the folder flush AFTER the rename: the file is at its name, so the generation IS published, marked
    // EINVAL is lane 1's "this filesystem cannot flush a folder" and succeeds; EIO is the real failure
    wo.fault = [](const char* step) { return std::strcmp(step, "dir_flush") == 0 ? EIO : 0; };
    check(park(c, Spec{100, 4, {}, true, false, false, 0, 4, false, kBig}, why, &pending, wo),
          "publish: the folder-flush failure publishes");
    const DiskGeneration* g = c.find(pending.serial);
    check(g != nullptr && g->dir_flush_pending, "publish: the pending folder flush is recorded");
    check(fs::exists(pending.path) && c.entries() == 1 && c.used() == pending.bytes,
          "publish: the published file is charged once");
    check(files_of(d) == 1, "publish: only the published file is in the folder");
    // a crash between the rename and the index: the file is on the volume, the index does not know it
    check(park(c, Spec{100, 5, {}, true, false, false, 0, 5, false, kBig}, why, &crash, {}, false),
          "crash: written, not indexed");
    check(c.entries() == 1 && c.used() == pending.bytes, "crash: the index is untouched");
    check(fs::exists(crash.path), "crash: the file is on the volume");
    // an interrupted publication's temporary and an orphan sidecar are the folder's own trash
    std::ofstream(d / ".conv-999999.sess.tmp") << "half a file";
    std::ofstream(d / "ring-999998.dring") << "half a pair";
    }   // c.~DiskCache releases the owner lock: the restart below opens the same folder
    {
        DiskCache r(opts(d, 256u << 20));
        check(open(r, why), "crash: the restart opens");
        const DiskCacheInventory& inv = r.inventory();
        check(inv.generations == 2, "crash: the restart indexes both committed generations");
        check(inv.orphan_temps == 1 && !fs::exists(d / ".conv-999999.sess.tmp"),
              "crash: the interrupted publication's temporary is reconciled");
        check(inv.orphan_sidecars == 1 && !fs::exists(d / "ring-999998.dring"), "crash: the orphan sidecar is reconciled");
        check(r.used() == pending.bytes + crash.bytes, "crash: the restart charges exactly the committed files");
        // the folder flush is a runtime fact, not a durable one: the restarted file is committed and clean
        check(r.find(pending.serial) != nullptr && !r.find(pending.serial)->dir_flush_pending,
              "crash: the folder-flush marker does not survive the restart");
        check(r.best(prompt_of(Spec{100, 4, {}, true, false, false, 0, 4, false, kBig}), {}, true) != nullptr,
              "crash: the restarted index answers lookups");
    }
    // an eviction whose removal fails refuses the write and keeps the index whole
    DiskCacheOptions fo = opts(d, pending.bytes);   // room for exactly one generation
    fo.fault = [](const char* step) { return std::strcmp(step, "remove") == 0 ? EACCES : 0; };
    DiskCache f(fo);
    check(open(f, why), "evict-fault: the cache opens");
    check(f.entries() == 2, "evict-fault: two generations indexed");
    DiskReservation res;
    check(!f.reserve(pending.bytes, 0, 64, res, why) && is_refused(res.refusal, "io"),
          "evict-fault: a removal that fails refuses the write");
    check(f.entries() == 2 && fs::exists(crash.path) && fs::exists(pending.path),
          "evict-fault: nothing on disk changed");
    check(f.reserved() == 0, "evict-fault: nothing is held");
}

// ============ 6.4: the restart inventory, the tie, corrupt and foreign files ============
void test_inventory_and_tie() {
    const fs::path d = dir("tie");
    std::error_code ec;
    fs::create_directories(d, ec);
    std::string why;
    // two committed files with the SAME tokens and pictures, from an engine that died between them
    const Spec s{100, 1, {}, true, false, false, 0, 1, false};
    for (int i = 0; i < 2; ++i) {
        std::vector<std::vector<uint8_t>> data;
        SessionManifestV2 m = build(Spec{100, 1, {}, true, false, false, 0, uint64_t(i == 0 ? 5 : 9), false}, data);
        size_t bytes = 0;
        const std::string path = (d / ("conv-" + std::to_string(10 + i) + ".sess")).string();
        check(session_file_write_v2(path, kId, m, sources_of(m, data), bytes, why), "tie: write the fixture file");
    }
    std::ofstream(d / "conv-77.sess") << "not a session file at all";
    {
        std::vector<std::vector<uint8_t>> data;
        const SessionManifestV2 m = build(Spec{100, 2, {}, true, false, false, 0, 1, false}, data);
        size_t bytes = 0;
        check(session_file_write_v2((d / "conv-78.sess").string(), kOther, m, sources_of(m, data), bytes, why),
              "inventory: write a file from another engine");
    }
    DiskCache c(opts(d, 256u << 20));
    check(open(c, why), "inventory: open over the folder");
    const DiskCacheInventory& inv = c.inventory();
    check(inv.generations == 2, "inventory: only the committed generations are indexed");
    check(inv.corrupt == 1, "inventory: the corrupt file of ours is counted");
    check(inv.foreign == 1, "inventory: the foreign file is counted");
    check(!fs::exists(d / "conv-77.sess") && !fs::exists(d / "conv-78.sess"),
          "inventory: unpublishable files of this namespace are removed");
    check(c.stats().incompatible == 1 && c.stats().corrupt == 1, "inventory: both are reported");
    check(c.entries() == 2, "inventory: the index holds the two usable generations");
    check(c.best(prompt_of(s), {}, true)->serial == 11, "tie: the most recently used generation wins");
    check(c.parked_prefix_tokens(tokens_of(s), {}, true) == 100, "inventory: the parked prefix is known");
    check(c.find(10) != nullptr && c.find(11) != nullptr, "inventory: both serials are findable");
    check(c.used() > 0 && c.index_bytes() > 0, "inventory: the restart priced what it indexed");
    // a folder of only trash is a usable empty cache
    DiskCache e(opts(dir("empty"), 256u << 20));
    check(open(e, why), "inventory: an absent folder is created");
    check(e.entries() == 0 && e.used() == 0, "inventory: nothing to index");
    check(fs::is_directory(dir("empty")), "inventory: the tier's folder exists");
}

// ============ the pair sidecars: the capture bound, the reclaim, the restart ============
void test_sidecars() {
    const fs::path d = dir("sidecar");
    std::string why;
    {
        DiskCache c(opts(d, 256u << 20));
        check(open(c, why), "sidecar: open");
        check(c.can_capture_sidecar(1u << 20, why), "sidecar: a capture the budget can take");
        note_sidecar_file(c, 7, 4096);
        note_sidecar_file(c, 8, 4096);
        uint64_t sz = 0;
        check(c.sidecar_bytes(7, sz) && sz == 4096, "sidecar: the capture is booked with its bytes");
        check(!c.sidecar_bytes(9, sz), "sidecar: an unknown id says so");
        // a paired generation advertises its sidecar; the unreferenced captures go, its pair stays
        const Spec paired{100, 1, {}, true, true, true, 1u << 13, 1, false};
        Parked p;
        check(park(c, paired, why, &p), "sidecar: park a paired generation");
        note_sidecar_file(c, 0xC0FFEE, 1u << 13);
        uint64_t freed = 0;
        check(c.reclaim_sidecars([](uint64_t) { return false; }, freed) == 2 && freed == 8192,
              "sidecar: the unreferenced captures go with their bytes");
        check(!fs::exists(c.sidecar_path(7)) && !fs::exists(c.sidecar_path(8)), "sidecar: their files are removed");
        check(fs::exists(c.sidecar_path(0xC0FFEE)), "sidecar: a published generation keeps its pair");
        const DiskGeneration* g = c.find(p.serial);
        check(g != nullptr && g->ring_ids.size() == 1 && g->ring_ids[0] == 0xC0FFEE, "sidecar: the index names the pair");
        check(g->pair_prefix == 100 && g->anchor == 100 && g->tail_draft, "sidecar: the generation advertises its draft");
        note_sidecar_file(c, 11, 2048);
        check(c.reclaim_sidecars([](uint64_t id) { return id == 11; }, freed) == 0 && fs::exists(c.sidecar_path(11)),
              "sidecar: a live checkpoint's reference keeps the file");
        check(c.reclaim_sidecars([](uint64_t) { return false; }, freed) == 1 && !fs::exists(c.sidecar_path(11)),
              "sidecar: with no reference at all it goes");
        check(fs::exists(c.sidecar_path(0xC0FFEE)), "sidecar: the advertised pair survives every reclaim");
    }   // c releases the folder's lock
    std::ofstream(d / "ring-12345.dring") << "a capture no generation advertises";
    {
        // the budget prices the captures too, and the restart reconciles what no generation advertises
        DiskCache small(opts(d, 16u << 10));
        check(open(small, why), "sidecar: the small cache opens with the captures");
        check(small.inventory().sidecars == 2 && small.inventory().orphan_sidecars == 1 &&
                  fs::exists(small.sidecar_path(0xC0FFEE)) && !fs::exists(small.sidecar_path(12345)),
              "sidecar: the restart keeps the advertised pair and reconciles the rest");
        check(small.inventory().sidecar_bytes == (1u << 13), "sidecar: only the referenced pair is charged");
        check(!small.can_capture_sidecar(16u << 10, why) && why.find("no room") != std::string::npos,
              "sidecar: a capture over the budget is refused at the capture");
        check(!fs::exists(small.sidecar_path(2)), "sidecar: the refused capture wrote nothing");
    }
}

// ============ the reported counters and the index's own bound ============
void test_stats_and_index_cost() {
    const fs::path d = dir("stats");
    std::string why;
    DiskGeneration g;
    check(disk_generation_index_bytes(g) > 0, "stats: an empty entry has a cost");
    g.tokens.assign(1000, 1);
    g.images.assign(3, {1, 2});
    g.ring_ids.assign(2, 7);
    g.path = "conv-1.sess";
    const uint64_t with = disk_generation_index_bytes(g);
    g.tokens.assign(2000, 1);
    check(disk_generation_index_bytes(g) > with, "stats: the index cost follows the metadata it holds");
    DiskCache c(opts(d, 256u << 20));
    check(open(c, why), "stats: open");
    Parked p;
    check(park(c, Spec{100, 1, {}, true, false, false, 0, 1, false}, why, &p), "stats: park");
    check(c.stats().parks == 1 && c.stats().last_serial == p.serial, "stats: the park is counted");
    check(c.index_bytes() == disk_generation_index_bytes(*c.find(p.serial)), "stats: the index holds the entry's bytes");
    check(c.stats().index_high >= c.index_bytes(), "stats: the index high-water mark");
    check(c.budget() == 256u << 20 && c.enabled(), "stats: the budget");
    c.set_io_mode(false);
    check(!c.stats().direct_io, "stats: the transfer mode is reported");
    DiskCache off(opts(d, 0));
    check(!off.enabled(), "stats: a zero budget is off");
    check(std::strcmp(disk_refusal_name(DiskRefusal::quota), "quota") == 0 &&
              std::strcmp(disk_refusal_name(DiskRefusal::space), "free-space") == 0 &&
              std::strcmp(disk_refusal_name(DiskRefusal::index), "index") == 0 &&
              std::strcmp(disk_refusal_name(DiskRefusal::count), "count") == 0 &&
              std::strcmp(disk_refusal_name(DiskRefusal::protected_), "protected") == 0 &&
              std::strcmp(disk_refusal_name(DiskRefusal::io), "io") == 0,
          "stats: the refusal codes are the reported words");
}

// ============ 6.4: the whole index rebuilt, lookups and retirement after a restart ============
void test_restart_round_trip() {
    const fs::path d = dir("roundtrip");
    std::string why;
    const Spec a{100, 1, {{10, 0x1111ull}}, true, true, true, 1u << 13, 1, false};
    const Spec b{200, 2, {{10, 0x1111ull}}, false, false, false, 0, 2, false};   // its own conversation, not a's tail
    uint64_t used = 0;
    {
        DiskCache c(opts(d, 256u << 20));
        check(open(c, why), "roundtrip: open");
        Parked pa, pb;
        check(park(c, a, why, &pa), "roundtrip: park the paired generation");
        note_sidecar_file(c, 0xC0FFEE, 1u << 13);
        check(park(c, b, why, &pb), "roundtrip: park the second");
        used = c.used();
        check(c.entries() == 2, "roundtrip: two generations");
    }
    DiskCache r(opts(d, 256u << 20));
    check(open(r, why), "roundtrip: the restart opens");
    check(r.entries() == 2 && r.used() == used, "roundtrip: the index is rebuilt with the same bytes");
    check(r.inventory().sidecars == 1 && r.inventory().sidecar_bytes == (1u << 13) && r.inventory().orphan_sidecars == 0,
          "roundtrip: the referenced pair survives the restart");
    const DiskGeneration* g = r.best(prompt_of(a), {{10, 0x1111ull}}, true);
    check(g != nullptr && g->pair_prefix == 100 && g->anchor == 100, "roundtrip: the advertised pair survived");
    check(r.best(prompt_of(b), {{10, 0x1111ull}}, false) != nullptr, "roundtrip: the cvec is restored per generation");
    check(r.best(prompt_of(b), {{10, 0x1111ull}}, true) == nullptr, "roundtrip: a cvec mismatch is no match");
    const std::string retired = r.best(prompt_of(b), {{10, 0x1111ull}}, false)->path;
    check(r.retire_superseded(tokens_of(b), {{10, 0x1111ull}}, false) == 1, "roundtrip: retirement works after a restart");
    check(!fs::exists(retired), "roundtrip: the retired file is gone");
    check(r.used() < used, "roundtrip: its bytes came back to the quota");
}

}   // namespace

int main() {
    g_tmp = fs::temp_directory_path() / "strata_disk_cache_test";
    std::error_code ec;
    fs::remove_all(g_tmp, ec);
    fs::create_directories(g_tmp, ec);
    test_lookup_and_supersession();
    test_owner_lock();
    test_quota_and_protections();
    test_space_index_and_count();
    test_publication_and_faults();
    test_inventory_and_tie();
    test_sidecars();
    test_stats_and_index_cost();
    test_restart_round_trip();
    fs::remove_all(g_tmp, ec);
    std::printf("conversation_disk_cache_test: %d checks passed\n", checks);
    return 0;
}

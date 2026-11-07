// CPU-only tests for the split-aware streaming session format v2
// (include/strata/core/conversation_file.hpp, "format v2").
// Built with -DSTRATA_BUILD_CONVERSATION_TESTS=ON; no CUDA, no model, no GPU.
#include "strata/core/conversation_file.hpp"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <string>
#include <utility>
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

void set_env(const char* name, const char* value) {
#ifdef _WIN32
    _putenv_s(name, value ? value : "");
#else
    if (value) setenv(name, value, 1); else unsetenv(name);
#endif
}

std::vector<char> slurp(const fs::path& p) {
    std::ifstream f(p, std::ios::binary);
    return std::vector<char>((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}
void spit(const fs::path& p, const std::vector<char>& d) {
    std::ofstream f(p, std::ios::binary | std::ios::trunc);
    f.write(d.data(), (std::streamsize) d.size());
}
bool no_temp(const fs::path& dir) {
    for (const auto& e : fs::directory_iterator(dir)) {
        const std::string n = e.path().filename().string();
        if (n.size() > 4 && n.compare(n.size() - 4, 4, ".tmp") == 0) return false;
    }
    return true;
}

std::vector<uint8_t> bytes_of(size_t n, uint8_t seed) {
    std::vector<uint8_t> v(n);
    for (size_t i = 0; i < n; ++i) v[i] = uint8_t(i * 131u + seed);
    return v;
}

std::string key(const SessionRegion& r) {
    return std::string(session_region_kind_name(r.kind)) + ":" + std::to_string(r.stage) + ":" +
           std::to_string(r.index) + ":" + std::to_string(r.part);
}
bool canonical(const SessionRegion& a, const SessionRegion& b) {
    if (a.kind != b.kind) return a.kind < b.kind;
    if (a.stage != b.stage) return a.stage < b.stage;
    if (a.index != b.index) return a.index < b.index;
    return a.part < b.part;
}

// a v2 conversation: the regions it declares plus the bytes its sources expose, in the SAME (shuffled) order, so
// the write's canonicalization and the read's per-region byte equality are both under test
struct Sample {
    SessionManifestV2 manifest;
    std::vector<std::vector<uint8_t>> data;   // data[i] is the bytes of manifest.regions[i]
};

void add(Sample& s, int32_t kind, int64_t stage, int64_t index, int64_t part, size_t n, uint8_t seed) {
    SessionRegion r;
    r.kind = kind; r.stage = stage; r.index = index; r.part = part; r.bytes = n;
    s.manifest.regions.push_back(r);
    s.data.push_back(bytes_of(n, seed));
}

// two logical stages, layer-split checkpoint parts, an MTP region and a DFlash ring: the shape v1 refuses
Sample split_sample() {
    Sample s;
    auto& m = s.manifest;
    for (size_t i = 0; i < m.layout.geometry.size(); ++i) m.layout.geometry[i] = int64_t(100 + i);
    m.layout.stages = {{0, 3, 2}, {3, 6, 2}};
    m.draft = {7, 2112, 2048, 8192, 1234, 1400, 0xa5a5a5a5a5a5a5a5ull};   // appended_through > committed_end
    m.cvec = false;
    for (size_t i = 0; i < 1000; ++i) m.tokens.push_back(int32_t(1000 + i));
    m.images = {{0, 0x1111ull}, {120, 0x2222ull}, {600, 0x3333ull}};
    m.checkpoints = {{10, 3, true, 1, {4096, 0, 100, 0, 0.5, true, false}},
                     {500, 42, false, 1, {2048, 100, 500, 7, 1.2345678901234567, false, true}},
                     {1000, 99, false, 1, {0, -1, -1, 0, 3.141592653589793, false, false}}};
    const auto K = int32_t(SessionRegionKind::kv), L = int32_t(SessionRegionKind::live_state),
               C = int32_t(SessionRegionKind::checkpoint_state), M = int32_t(SessionRegionKind::mtp),
               D = int32_t(SessionRegionKind::dflash);
    // deliberately not in canonical order: the format must not depend on the caller's order
    add(s, D, 0, 0, 0, 2u << 20, 7);
    add(s, C, 1, 2, 0, 2048, 61); add(s, C, 1, 2, 1, 64, 62); add(s, C, 1, 2, 2, 128, 63);
    add(s, C, 1, 2, 3, 32, 64); add(s, C, 1, 2, 4, 8, 65);
    add(s, C, 0, 2, 0, 4099, 51); add(s, C, 0, 2, 1, 77, 52); add(s, C, 0, 2, 2, 301, 53);
    add(s, C, 0, 2, 3, 64, 54); add(s, C, 0, 2, 4, 8, 55);
    add(s, C, 1, 1, 0, 2048, 41); add(s, C, 1, 1, 1, 64, 42); add(s, C, 1, 1, 2, 128, 43);
    add(s, C, 1, 1, 3, 32, 44); add(s, C, 1, 1, 4, 8, 45);
    add(s, C, 0, 1, 0, 4099, 31); add(s, C, 0, 1, 1, 77, 32); add(s, C, 0, 1, 2, 301, 33);
    add(s, C, 0, 1, 3, 64, 34); add(s, C, 0, 1, 4, 8, 35);
    add(s, C, 1, 0, 0, 50, 26); add(s, C, 1, 0, 1, 5, 27); add(s, C, 1, 0, 2, 5, 28);
    add(s, C, 1, 0, 3, 2, 29); add(s, C, 1, 0, 4, 2, 30);
    add(s, C, 0, 0, 0, 100, 21); add(s, C, 0, 0, 1, 10, 22); add(s, C, 0, 0, 2, 10, 23);
    add(s, C, 0, 0, 3, 4, 24); add(s, C, 0, 0, 4, 4, 25);
    add(s, M, 0, 0, 0, 1024, 71); add(s, M, 0, 0, 1, 512, 72); add(s, M, 0, 0, 2, 128, 73);
    add(s, M, 0, 0, 3, 128, 74); add(s, M, 0, 0, 4, 256, 75);
    add(s, K, 1, 0, 0, (1u << 20) + 7, 81); add(s, K, 1, 0, 1, 3000, 82); add(s, K, 1, 0, 2, 256, 83);
    add(s, K, 1, 0, 3, 256, 84); add(s, K, 1, 0, 4, 1024, 85);
    add(s, K, 0, 1, 0, 4096, 11); add(s, K, 0, 1, 1, 4096, 12); add(s, K, 0, 1, 2, 0, 13);
    add(s, K, 0, 1, 3, 512, 14); add(s, K, 0, 1, 4, 4608, 15);
    add(s, K, 0, 0, 0, (17u << 20) + 5, 1); add(s, K, 0, 0, 1, 4096, 2); add(s, K, 0, 0, 2, 512, 3);
    add(s, K, 0, 0, 3, 512, 4); add(s, K, 0, 0, 4, 9 * 128 * 4, 5);
    add(s, L, 1, 0, 0, 2048, 91); add(s, L, 1, 0, 1, 64, 92); add(s, L, 1, 0, 2, 128, 93);
    add(s, L, 1, 0, 3, 32, 94); add(s, L, 1, 0, 4, 8, 95);
    add(s, L, 0, 0, 0, 4099, 101); add(s, L, 0, 0, 1, 77, 102); add(s, L, 0, 0, 2, 301, 103);
    add(s, L, 0, 0, 3, 64, 104); add(s, L, 0, 0, 4, 8, 105);
    return s;
}

void drop(Sample& s, const std::function<bool(const SessionRegion&)>& what) {
    std::vector<std::vector<uint8_t>> data;
    std::vector<SessionRegion> regions;
    for (size_t i = 0; i < s.manifest.regions.size(); ++i) {
        if (what(s.manifest.regions[i])) continue;
        regions.push_back(s.manifest.regions[i]);
        data.push_back(std::move(s.data[i]));
    }
    s.manifest.regions = std::move(regions);
    s.data = std::move(data);
}

// one stage, no split parts, no MTP, no draft: the plain shape
Sample plain_sample() {
    Sample s = split_sample();
    s.manifest.layout.stages = {{0, 3, 2}};
    s.manifest.draft = {};
    for (auto& c : s.manifest.checkpoints) c.stage_parts = 0;
    drop(s, [](const SessionRegion& r) {
        return r.stage == 1 || r.kind == int32_t(SessionRegionKind::mtp) ||
               r.kind == int32_t(SessionRegionKind::dflash) ||
               (r.kind == int32_t(SessionRegionKind::checkpoint_state) && r.index == 2);
    });
    return s;
}

// a PAIRED checkpoint: checkpoint 2 (prefix 1000) carries the ring image captured AT its boundary,
// on the file's own draft layout, with the exact counters the capture took. The pair region is one
// more streamed region; its meta names it.
Sample paired_sample() {
    Sample s = split_sample();
    add(s, int32_t(SessionRegionKind::dflash_pair), 0, 2, 0, 3u << 17, 131);
    auto& c = s.manifest.checkpoints[2];
    c.ring_id = 0xC0FFEE;
    c.ring_bytes = s.data.back().size();
    c.ring_checksum = session_hash64(s.data.back().data(), s.data.back().size(), 0);
    c.ring_cells = s.manifest.draft.ring_cells;
    c.ring_window = s.manifest.draft.window;
    c.ring_capacity = s.manifest.draft.capacity;
    c.ring_committed_end = int64_t(c.prefix);
    c.ring_appended_through = 1400;
    return s;
}

std::vector<SessionRegionSource> sources_of(const Sample& s) {
    std::vector<SessionRegionSource> out;
    for (size_t i = 0; i < s.manifest.regions.size(); ++i) {
        const auto& r = s.manifest.regions[i];
        const std::vector<uint8_t>* d = &s.data[i];
        SessionRegionSource src;
        src.kind = r.kind; src.stage = r.stage; src.index = r.index; src.part = r.part; src.size = d->size();
        src.read = [d](size_t offset, void* dst, size_t n) {
            if (offset + n > d->size()) return false;
            std::memcpy(dst, d->data() + offset, n);
            return true;
        };
        src.error = [] { return std::string("fixture source"); };
        out.push_back(std::move(src));
    }
    return out;
}

// a sink that keeps every region's bytes, so the round trip is compared region by region
struct Collector {
    std::map<std::string, std::vector<uint8_t>> got;
    std::vector<std::string> validated;
    uint64_t writes = 0, finishes = 0, max_block = 0;
    bool accept_all = true;
    std::string refuse_key;
};
SessionRegionSink collector_sink(Collector& c) {
    SessionRegionSink s;
    s.validate_region = [&c](const SessionRegion& r, std::string& why) {
        c.validated.push_back(key(r));
        c.got[key(r)] = {};
        if (!c.accept_all && key(r) == c.refuse_key) { why = "the destination is full"; return false; }
        return true;
    };
    s.write = [&c](const SessionRegion& r, uint64_t offset, const void* p, size_t n, std::string&) {
        ++c.writes;
        c.max_block = std::max(c.max_block, n);
        auto& v = c.got[key(r)];
        if (v.size() != offset) return false;
        v.insert(v.end(), (const uint8_t*) p, (const uint8_t*) p + n);
        return true;
    };
    s.finish = [&c](std::string&) { ++c.finishes; return true; };
    return s;
}

SessionV2Requirement want_of(const Sample& s) {
    SessionV2Requirement w;
    w.layout = s.manifest.layout;
    w.draft = s.manifest.draft;
    return w;
}

bool region_bytes_match(const Sample& s, const Collector& c) {
    for (size_t i = 0; i < s.manifest.regions.size(); ++i) {
        auto it = c.got.find(key(s.manifest.regions[i]));
        if (it == c.got.end() || it->second != s.data[i]) return false;
    }
    return c.got.size() == s.manifest.regions.size();
}

// read with a collecting sink and report the refusal, the sink's write count being the mutation witness
bool rejects(const fs::path& p, const SessionV2Requirement& want, const SessionReadV2Limits& limits,
             const SessionFileIdentity& id, Collector& col, std::string& error) {
    SessionManifestV2 out;
    size_t bytes = 0;
    SessionStatus st;
    SessionRegionSink sink = collector_sink(col);
    SessionRegionSink* use = &sink;
    return !session_file_read_v2(p.string(), id, want, out, bytes, error, limits, use, &st) && !error.empty();
}

// the manifest's section frames, so a test can patch one field and recompute the file's own checksums
size_t section_frame(const std::vector<char>& f, uint32_t tag) {
    size_t at = 64;
    while (at + 12 <= f.size()) {
        uint32_t t = 0;
        uint64_t len = 0;
        std::memcpy(&t, f.data() + at, 4);
        std::memcpy(&len, f.data() + at + 4, 8);
        if (t == 0) return SIZE_MAX;
        if (t == tag) return at;
        at += 12 + (size_t) len;
    }
    return SIZE_MAX;
}
uint64_t field64(const std::vector<char>& f, size_t at) {
    uint64_t v = 0;
    std::memcpy(&v, f.data() + at, 8);
    return v;
}
void put64(std::vector<char>& f, size_t at, uint64_t v) { std::memcpy(f.data() + at, &v, 8); }

// recompute the file's own trailer checksums after the manifest bytes changed (the region bytes did not)
void reseal(std::vector<char>& f) {
    const uint64_t payload = field64(f, 32), manifest = field64(f, 40);
    const uint64_t table = f.size() - 64 - payload - 32;
    put64(f, f.size() - 32, session_hash64(f.data() + 64, payload, 0));
    put64(f, f.size() - 24, session_hash64(f.data() + 64, manifest, 0));
    put64(f, f.size() - 16, session_hash64(f.data() + 64 + payload, table, 0));
}

// recompute a ring sidecar's header hash after its fields changed (the payload bytes did not)
void reseal_sidecar(std::vector<char>& h) {
    const uint64_t hh = session_hash64(h.data(), 80, 0);
    std::memcpy(h.data() + 80, &hh, 8);
}
} // namespace

int main() {
    const fs::path dir = fs::temp_directory_path() / ("strata-session-v2-test-" + std::to_string(
        std::chrono::steady_clock::now().time_since_epoch().count()));
    fs::create_directories(dir);
    const SessionFileIdentity id{0x1111222233334444ull, 0x5555666677778888ull};
    const Sample split = split_sample();
    const SessionV2Requirement want = want_of(split);
    const fs::path good = dir / "split.bin";
    size_t written = 0;
    std::string error;

    // round trip: write from region sources, stream-read back, region-by-region byte equality
    check(session_file_write_v2(good.string(), id, split.manifest, sources_of(split), written, error), "v2 write succeeds");
    check(error.empty(), "v2 write leaves no error");
    check(written == fs::file_size(good), "v2 write reports the file size");
    const size_t good_size = written;   // later writes reuse `written`, so the split file's size is kept here
    check(no_temp(dir), "no temporary left behind");
    {
        const std::vector<char> f = slurp(good);
        check(f.size() >= 96 && std::memcmp(f.data(), "STRSESS\x02", 8) == 0, "v2 magic");
        uint32_t version = 0;
        std::memcpy(&version, f.data() + 8, 4);
        check(version == 2, "v2 version");
        check(field64(f, 48) == 0, "v2 reserved header field is zero");
        check(std::memcmp(f.data() + f.size() - 8, "STRSEND\x02", 8) == 0, "v2 committed marker");
    }
    {
        SessionManifestV2 back;
        Collector col;
        SessionRegionSink sink = collector_sink(col);
        SessionRegionSink* use = &sink;
        size_t read = 0;
        std::vector<std::pair<uint64_t, uint64_t>> seen;
        SessionReadV2Limits limits;
        limits.progress = [&](uint64_t d, uint64_t t) { seen.push_back({d, t}); };
        check(session_file_read_v2(good.string(), id, want, back, read, error, limits, use), "v2 read succeeds");
        check(read == good_size, "v2 read reports the file size");
        check(col.finishes == 1, "v2 sink finished once");
        check(region_bytes_match(split, col), "v2 round trip: every region's bytes are identical");
        check(col.validated.size() == split.manifest.regions.size(), "v2 sink validated every region exactly once");
        check(col.max_block <= (16u << 20), "v2 blocks are at most 16 MiB");
        check(col.writes >= split.manifest.regions.size(), "v2 sink wrote every region");
        uint64_t region_total = 0;
        for (const auto& r : split.manifest.regions) region_total += r.bytes;
        const uint64_t manifest_at = field64(slurp(good), 40);
        bool monotone = seen.size() >= 2 && seen.front().first == 64 + manifest_at &&
                        seen.back().first == seen.back().second && seen.back().second == good_size + region_total;
        for (size_t i = 1; i < seen.size(); ++i)
            monotone = monotone && seen[i].first >= seen[i - 1].first && seen[i].first - seen[i - 1].first <= (16u << 20);
        check(monotone, "v2 read reports every block, at most 16 MiB apart, ending at the bytes it moves (both passes)");
        seen.clear();
        limits.progress = [&](uint64_t d, uint64_t t) { seen.push_back({d, t}); };
        check(session_file_read_v2(good.string(), id, want, back, read, error, limits, nullptr) &&
              seen.back().first == seen.back().second && seen.back().second == good_size,
              "v2 validation-only read reports out of the file size");
        // the manifest round-trips: the identities, the single token vector, the prefix references, the plans
        check(back.layout == split.manifest.layout, "v2 layout identity round-trips (stages, ranges, geometry, kv format)");
        check(back.draft == split.manifest.draft, "v2 draft identity round-trips (type, artifact, ring, exact counters)");
        check(back.draft.appended_through > back.draft.committed_end, "v2 keeps appended_through above committed_end");
        check(back.cvec == split.manifest.cvec, "v2 cvec round-trips");
        check(back.tokens == split.manifest.tokens, "v2 stores the token ids once");
        check(back.images == split.manifest.images, "v2 stores the image keys once");
        check(back.checkpoints.size() == split.manifest.checkpoints.size(), "v2 checkpoint count round-trips");
        bool metas = true;
        for (size_t i = 0; i < back.checkpoints.size(); ++i) {
            const auto& a = back.checkpoints[i];
            const auto& b = split.manifest.checkpoints[i];
            metas = metas && a.prefix == b.prefix && a.used == b.used && a.pinned == b.pinned &&
                    a.stage_parts == b.stage_parts && a.plan.chunk == b.plan.chunk && a.plan.from == b.plan.from &&
                    a.plan.to == b.plan.to && a.plan.lend == b.plan.lend && a.plan.pipe == b.plan.pipe &&
                    a.plan.single == b.plan.single && a.plan.chunked == b.plan.chunked &&
                    a.ring_id == b.ring_id && a.ring_bytes == b.ring_bytes && a.ring_checksum == b.ring_checksum &&
                    a.ring_cells == b.ring_cells && a.ring_window == b.ring_window && a.ring_capacity == b.ring_capacity &&
                    a.ring_committed_end == b.ring_committed_end && a.ring_appended_through == b.ring_appended_through;
        }
        check(metas, "v2 checkpoint prefix references, pin, LRU stamp, prompt plan and pair meta round-trip");
        bool target_only = true;
        for (const auto& c : back.checkpoints)
            target_only = target_only && c.ring_id == 0 && c.ring_bytes == 0 && c.ring_checksum == 0 &&
                          c.ring_cells == 0 && c.ring_window == 0 && c.ring_capacity == 0 &&
                          c.ring_committed_end == -1 && c.ring_appended_through == -1;
        check(target_only, "v2 keeps an unpaired checkpoint target-only with every pair field zero");
        // the descriptors: canonical order, tiled offsets, and the checksums the write computed
        uint64_t at = 0;
        bool tiled = back.regions.size() == split.manifest.regions.size();
        std::map<std::string, size_t> bytes_of_key;
        for (size_t i = 0; i < split.manifest.regions.size(); ++i) bytes_of_key[key(split.manifest.regions[i])] = split.data[i].size();
        for (size_t i = 0; i < back.regions.size(); ++i) {
            const auto& r = back.regions[i];
            const auto known = bytes_of_key.find(key(r));
            tiled = tiled && known != bytes_of_key.end() && r.offset == at && r.bytes == known->second &&
                    (i == 0 || canonical(back.regions[i - 1], r));
            at += r.bytes;
        }
        check(tiled, "v2 region directory is canonical, tiled and names every declared region");
        // the checksums are the regions' own bytes hashed
        bool sums = true;
        for (const auto& r : back.regions) {
            const auto& bytes = split.data[std::find_if(split.manifest.regions.begin(), split.manifest.regions.end(),
                                                        [&](const SessionRegion& x) { return key(x) == key(r); })
                                          - split.manifest.regions.begin()];
            sums = sums && r.checksum == session_hash64(bytes.data(), bytes.size(), 0);
        }
        check(sums, "v2 per-region checksums hash exactly that region's bytes");
        const std::vector<char> f = slurp(good);
        check(at == field64(f, 32) - field64(f, 40), "v2 region bytes fill the declared payload");
    }
    // a verification-only read (no sink) leaves nothing written anywhere
    {
        SessionManifestV2 back;
        size_t read = 0;
        check(session_file_read_v2(good.string(), id, want, back, read, error), "v2 read without a sink verifies the file");
        check(back.regions.size() == split.manifest.regions.size(), "v2 sink-less read returns the directory");
    }
    // format v2 is deterministic, and replaces an existing file
    {
        const fs::path again = dir / "again.bin";
        check(session_file_write_v2(again.string(), id, split.manifest, sources_of(split), written, error), "v2 second write");
        check(slurp(again) == slurp(good), "v2 format is deterministic");
        const fs::path over = dir / "over.bin";
        spit(over, std::vector<char>(100, 'o'));
        check(session_file_write_v2(over.string(), id, split.manifest, sources_of(split), written, error), "v2 write over an existing file");
        check(slurp(over) == slurp(good), "v2 replaced the existing file");
        check(no_temp(dir), "no temporary left behind after replace");
    }
    // plain shape: one stage, no split parts, no draft
    {
        const Sample plain = plain_sample();
        const fs::path p = dir / "plain.bin";
        check(session_file_write_v2(p.string(), id, plain.manifest, sources_of(plain), written, error), "v2 single-stage write");
        SessionManifestV2 back;
        Collector col;
        SessionRegionSink sink = collector_sink(col);
        SessionRegionSink* use = &sink;
        size_t read = 0;
        check(session_file_read_v2(p.string(), id, want_of(plain), back, read, error, {}, use), "v2 single-stage read");
        check(region_bytes_match(plain, col), "v2 single-stage round trip");
        check(back.draft.type == 0 && back.draft.committed_end == -1, "v2 keeps an absent draft absent");
    }
    // paired checkpoint: the pair streams as its own region and the meta names it exactly
    {
        const Sample paired = paired_sample();
        const fs::path p = dir / "pair.bin";
        check(session_file_write_v2(p.string(), id, paired.manifest, sources_of(paired), written, error), "v2 paired write");
        SessionManifestV2 back;
        Collector col;
        SessionRegionSink sink = collector_sink(col);
        SessionRegionSink* use = &sink;
        size_t read = 0;
        check(session_file_read_v2(p.string(), id, want_of(paired), back, read, error, {}, use) &&
              region_bytes_match(paired, col), "v2 paired round trip: the pair region's bytes stream like every region");
        const auto& a = back.checkpoints[2];
        const auto& b = paired.manifest.checkpoints[2];
        check(a.ring_id == b.ring_id && a.ring_bytes == b.ring_bytes && a.ring_checksum == b.ring_checksum &&
              a.ring_cells == b.ring_cells && a.ring_window == b.ring_window && a.ring_capacity == b.ring_capacity &&
              a.ring_committed_end == b.ring_committed_end && a.ring_appended_through == b.ring_appended_through,
              "v2 pair meta round-trips (id, extent, checksum, layout, exact counters)");
        check(a.ring_committed_end == int64_t(a.prefix), "the read pair's captured end sits at the checkpoint position");
        bool pair_region = false;
        for (const auto& r : back.regions)
            if (r.kind == int32_t(SessionRegionKind::dflash_pair) && r.index == 2)
                pair_region = r.bytes == a.ring_bytes && r.checksum == a.ring_checksum && r.stage == 0 && r.part == 0;
        check(pair_region, "the pair region is in the directory with the meta's extent and checksum");
        check(back.checkpoints[0].ring_id == 0 && back.checkpoints[1].ring_id == 0,
              "v2 unpaired checkpoints read target-only beside a pair");
    }
    // v1 refuses layer-split state; v2 takes it.  v1's golden test in conversation_file_test is untouched.
    {
        SavedConversation split_image;
        split_image.layer_lo = 0; split_image.layer_hi = 3;
        split_image.live.stage_parts.push_back(ConversationCheckpoint{});
        const fs::path p = dir / "v1-split.bin";
        check(!session_file_write(p.string(), split_image, id, written, error) &&
              error.find("layer-split") != std::string::npos, "v1 still refuses layer-split state");
        check(!fs::exists(p) && no_temp(dir), "refused v1 write leaves no file");
    }
    // the two formats refuse each other
    {
        SavedConversation v1_image;
        const fs::path v1file = dir / "v1.bin";
        size_t n = 0;
        check(session_file_write(v1file.string(), v1_image, id, n, error), "v1 write for the cross-read");
        SessionManifestV2 back;
        Collector col;
        SessionRegionSink sink = collector_sink(col);
        SessionRegionSink* use = &sink;
        check(!session_file_read_v2(v1file.string(), id, want, back, n, error, {}, use) &&
              error.find("magic") != std::string::npos && col.writes == 0,
              "the v2 reader refuses a v1 file (magic) with the sink untouched");
        check(!session_file_read(good.string(), id, v1_image, n, error) && error.find("magic") != std::string::npos,
              "the v1 reader refuses a v2 file (magic)");
    }
    // truncation everywhere: refusal, the sink untouched
    {
        const std::vector<char> f = slurp(good);
        const size_t region_start = 64 + (size_t) field64(f, 40);
        for (size_t cut : {size_t(0), size_t(7), size_t(63), size_t(64), size_t(65), region_start - 1, region_start + 1,
                           f.size() / 2, f.size() - 33, f.size() - 32, f.size() - 24, f.size() - 8, f.size() - 1}) {
            const fs::path p = dir / "cut.bin";
            spit(p, std::vector<char>(f.begin(), f.begin() + (std::ptrdiff_t) std::min(cut, f.size())));
            Collector col;
            std::string err;
            check(rejects(p, want, {}, id, col, err) && col.writes == 0, "v2 truncated file refused, sink untouched");
        }
        auto tail = f;
        tail.push_back('x');
        const fs::path p = dir / "tail.bin";
        spit(p, tail);
        Collector col;
        std::string err;
        check(rejects(p, want, {}, id, col, err) && col.writes == 0 && err.find("size") != std::string::npos,
              "v2 file with trailing bytes refused (size), sink untouched");
    }
    // corruption: every category refused before the sink is written, naming what was damaged
    {
        const std::vector<char> f = slurp(good);
        const uint64_t payload = field64(f, 32), manifest = field64(f, 40);
        const size_t region_start = 64 + (size_t) manifest;
        struct Flip { size_t at; const char* want; };
        const std::vector<Flip> flips = {
            {64 + 300, "manifest"},                                                 // a manifest byte
            {region_start + 100, "region"},                                         // the FIRST region's bytes
            {region_start + (size_t) payload - (size_t) manifest - 100, "region"},  // the LAST region's bytes
            {region_start + (size_t) payload / 4, "region"},                        // a middle region's bytes
            {f.size() - 8, "committed marker"},                                     // the committed marker
            {f.size() - 40, "region"},                                              // a region byte before the table
            {20, "header checksum"},                                                // a header byte
        };
        for (const auto& fl : flips) {
            auto d = f;
            d[fl.at] ^= 0x40;
            const fs::path p = dir / "flip.bin";
            spit(p, d);
            Collector col;
            std::string err;
            const bool refused = rejects(p, want, {}, id, col, err);
            if (!refused || col.writes != 0 || err.find(fl.want) == std::string::npos)
                std::fprintf(stderr, "  v2 corruption at %zu: got '%s' wanted '%s'\n", fl.at, err.c_str(), fl.want);
            check(refused && col.writes == 0 && err.find(fl.want) != std::string::npos,
                  "v2 corrupted byte refused with the sink untouched");
        }
        // a tampered region checksum (the bytes are intact, the table lies): refused, naming the region
        auto d = f;
        put64(d, 64 + (size_t) payload + 16, field64(d, 64 + (size_t) payload + 16) ^ 1);
        reseal(d);
        const fs::path p = dir / "cksum.bin";
        spit(p, d);
        Collector col;
        std::string err;
        check(rejects(p, want, {}, id, col, err) && col.writes == 0 && err.find("checksum") != std::string::npos,
              "v2 tampered region checksum refused, sink untouched");
    }
    // unknown, missing and repeated sections of the required manifest are refusals
    {
        const std::vector<char> f = slurp(good);
        const std::vector<uint32_t> tags = {0x5459414Cu, 0x54465244u, 0x4E4B4F54u, 0x47414D49u, 0x54535043u,
                                            0x4B534452u};
        for (uint32_t tag : tags) {
            auto d = f;
            const size_t at = section_frame(d, tag);
            check(at != SIZE_MAX, "v2 manifest section found for the patch");
            const uint32_t bogus = tag ^ 0x1010u;
            std::memcpy(d.data() + at, &bogus, 4);
            reseal(d);
            const fs::path p = dir / "section.bin";
            spit(p, d);
            Collector col;
            std::string err;
            check(rejects(p, want, {}, id, col, err) && col.writes == 0 &&
                  (err.find("unknown manifest section") != std::string::npos ||
                   err.find("missing a required section") != std::string::npos),
                  "v2 unknown section refused, sink untouched");
        }
        // a required section dropped: the terminator arrives early
        auto d = f;
        const size_t at = section_frame(d, 0x4B534452u);
        const uint32_t end = 0;
        std::memcpy(d.data() + at, &end, 4);
        reseal(d);
        const fs::path p = dir / "missing.bin";
        spit(p, d);
        Collector col;
        std::string err;
        check(rejects(p, want, {}, id, col, err) && col.writes == 0 &&
              (err.find("terminator") != std::string::npos || err.find("unparsed") != std::string::npos),
              "v2 missing required section refused, sink untouched");
        // a repeated section: a second draft frame over the region directory
        d = f;
        const size_t draft_at = section_frame(d, 0x54465244u);
        const size_t region_at = section_frame(d, 0x4B534452u);
        check(draft_at != SIZE_MAX && region_at != SIZE_MAX && field64(d, draft_at + 4) == 56, "v2 draft section is 56 bytes");
        std::memcpy(d.data() + region_at - 68, d.data() + draft_at, 68);
        reseal(d);
        const fs::path q = dir / "repeat.bin";
        spit(q, d);
        Collector col2;
        std::string err2;
        check(rejects(q, want, {}, id, col2, err2) && col2.writes == 0, "v2 repeated section refused, sink untouched");
    }
    // offset and length arithmetic is checked before any allocation or transfer
    {
        const std::vector<char> f = slurp(good);
        const size_t dir_at = section_frame(f, 0x4B534452u) + 12 + 8;
        const size_t region_index = 3;
        auto patch = [&](size_t which, uint64_t value, const char* file) {
            auto d = f;
            put64(d, dir_at + 56 * region_index + which, value);
            reseal(d);
            const fs::path p = dir / file;
            spit(p, d);
            Collector col;
            std::string err;
            const bool refused = rejects(p, want, {}, id, col, err);
            return std::make_pair(refused && col.writes == 0, err);
        };
        check(patch(32, 0x7fffffffffffffffull, "off1.bin").first, "v2 region offset overflow refused, sink untouched");
        check(patch(40, 0x7fffffffffffffffull, "len1.bin").first, "v2 region length overflow refused, sink untouched");
        auto moved = patch(32, field64(f, dir_at + 56 * region_index + 32) + 1, "gap.bin");
        check(moved.first && moved.second.find("expected") != std::string::npos, "v2 region offset gap refused, sink untouched");
        // region lengths that do not add up to the file's payload
        // region lengths that do not add up to the file's payload: the last region's length, where the tiling
        // stays consistent and only the total is short
        auto d = f;
        const size_t dir_at2 = section_frame(d, 0x4B534452u) + 12 + 8;
        const size_t last = split.manifest.regions.size() - 1;
        put64(d, dir_at2 + 56 * last + 40, field64(d, dir_at2 + 56 * last + 40) + 1);
        reseal(d);
        const fs::path p = dir / "toolong.bin";
        spit(p, d);
        Collector col;
        std::string err;
        check(rejects(p, want, {}, id, col, err) && col.writes == 0 && err.find("size") != std::string::npos,
              "v2 region lengths not matching the file size refused, sink untouched");
    }
    // the read's own manifest validation enforces the pair rules on a file the write published
    {
        const Sample paired = paired_sample();
        const fs::path q = dir / "pairread.bin";
        check(session_file_write_v2(q.string(), id, paired.manifest, sources_of(paired), written, error), "v2 paired write for the patch tests");
        const std::vector<char> f = slurp(q);
        const size_t ckpt = section_frame(f, 0x54535043u) + 12 + 8 + 152 * 2;   // checkpoint 2's record (19 fields x 8)
        auto patch = [&](size_t field, uint64_t value, const char* expect, const char* label) {
            auto d = f;
            put64(d, ckpt + 8 * field, value);
            reseal(d);
            const fs::path p = dir / "pairpatch.bin";
            spit(p, d);
            Collector col;
            std::string err;
            check(rejects(p, want, {}, id, col, err) && col.writes == 0 && err.find(expect) != std::string::npos, label);
        };
        patch(17, 999, "pairs the ring at position", "v2 read refuses a pair captured off the checkpoint position");
        patch(11, 0, "target-only with pair bytes", "v2 read refuses a target-only checkpoint with pair bytes");
        patch(12, field64(f, ckpt + 8 * 12) + 1, "its meta declares", "v2 read refuses a pair meta/region size mismatch");
        patch(14, 1056, "not the file's draft", "v2 read refuses a pair on a foreign ring layout");
        patch(18, paired.manifest.draft.capacity + 1, "outside the ring capacity", "v2 read refuses pair counters over the capacity");
    }
    // the reader's bounds, all of them before anything is allocated
    {
        SessionReadV2Limits l;
        l.max_regions = 10;
        Collector col;
        std::string err;
        check(rejects(good, want, l, id, col, err) && err.find("limit") != std::string::npos && col.writes == 0,
              "v2 region count over the limit refused");
        l = {};
        for (const auto& r : split.manifest.regions) l.max_region_bytes = std::min(l.max_region_bytes, r.bytes - 1);
        check(rejects(good, want, l, id, col, err) && err.find("limit") != std::string::npos && col.writes == 0,
              "v2 region over the runtime byte limit refused");
        l = {};
        l.max_tokens = 999;
        check(rejects(good, want, l, id, col, err) && err.find("limit") != std::string::npos, "v2 token count over the limit refused");
        l = {};
        l.max_checkpoints = 2;
        check(rejects(good, want, l, id, col, err) && err.find("limit") != std::string::npos,
              "v2 checkpoint count over the limit refused");
        l = {};
        l.max_stages = 1;
        check(rejects(good, want, l, id, col, err) && err.find("limit") != std::string::npos, "v2 stage count over the limit refused");
        l = {};
        l.max_manifest_bytes = field64(slurp(good), 40) - 1;
        check(rejects(good, want, l, id, col, err) && err.find("manifest") != std::string::npos,
              "v2 manifest over the limit refused");
        l = {};
        l.max_file_bytes = good_size - 1;
        check(rejects(good, want, l, id, col, err) && err.find("size") != std::string::npos, "v2 file over the size limit refused");
        l = {};
        check(rejects(dir / "absent.bin", want, l, id, col, err) && col.writes == 0, "v2 missing file refused");
        // the RAM preflight: asked once with a payload-independent peak, and it can refuse the file
        SessionManifestV2 back;
        SessionStatus st;
        size_t read = 0;
        l = {};
        l.max_regions = 64;
        uint64_t asked = 0;
        l.admit = [&](uint64_t n, std::string& why) { asked = n; why = "no room for it"; return false; };
        const uint64_t peak = session_read_v2_peak_bytes(field64(slurp(good), 40), l.max_regions);
        check(!session_file_read_v2(good.string(), id, want, back, read, error, l, nullptr, &st) &&
              st.error == SessionError::memory && asked == peak && peak < (40u << 20),
              "v2 admission is asked for a payload-independent peak and can refuse");
        check(session_read_v2_peak_bytes(4096, 40) < session_read_v2_peak_bytes(4096, 80), "v2 peak counts the region directory");
        check(session_read_v2_peak_bytes(1u << 20, 40) - session_read_v2_peak_bytes(4096, 40) == (1u << 20) - 4096,
              "v2 peak grows with the manifest, never with the payload");
        // a foreign identity is refused before the sink sees a byte
        SessionV2Requirement other = want;
        other.layout.stages[1].layer_hi = 9;
        Collector col2;
        SessionRegionSink sink2 = collector_sink(col2);
        SessionRegionSink* use2 = &sink2;
        check(!session_file_read_v2(good.string(), id, other, back, read, error, {}, use2, &st) &&
              st.error == SessionError::invalid && col2.writes == 0 && error.find("layout") != std::string::npos,
              "v2 split layout mismatch refused, sink untouched");
        other = want;
        other.draft.committed_end = 999;
        Collector col3;
        SessionRegionSink sink3 = collector_sink(col3);
        SessionRegionSink* use3 = &sink3;
        check(!session_file_read_v2(good.string(), id, other, back, read, error, {}, use3, &st) && col3.writes == 0 &&
              error.find("draft") != std::string::npos, "v2 draft identity mismatch refused, sink untouched");
        // the checks a caller leaves off are left off
        other = want;
        other.check_layout = false;
        other.check_draft = false;
        Collector col4;
        SessionRegionSink sink4 = collector_sink(col4);
        SessionRegionSink* use4 = &sink4;
        check(session_file_read_v2(good.string(), id, other, back, read, error, {}, use4) && read == good_size &&
              col4.finishes == 1, "v2 read with the identity checks off reads");
        // a destination that refuses: nothing is written
        Collector full;
        full.accept_all = false;
        for (const auto& r : split.manifest.regions)
            if (r.kind == int32_t(SessionRegionKind::live_state) && r.stage == 0) full.refuse_key = key(r);
        SessionRegionSink sink5 = collector_sink(full);
        SessionRegionSink* use5 = &sink5;
        check(!session_file_read_v2(good.string(), id, want, back, read, error, {}, use5) && full.writes == 0 &&
              error.find("destination") != std::string::npos, "v2 sink refusal leaves nothing written");
        // an incomplete sink is refused
        SessionRegionSink half;
        half.validate_region = [](const SessionRegion&, std::string&) { return true; };
        check(!session_file_read_v2(good.string(), id, want, back, read, error, {}, &half) &&
              error.find("sink") != std::string::npos, "v2 incomplete sink refused");
    }
    // the write's own refusals, before any disk access
    {
        const auto write_refuse = [&](const Sample& s, const char* expect, const char* label) {
            const fs::path p = dir / "refuse.bin";
            size_t n = 0;
            std::string err;
            SessionStatus st;
            const bool ok = session_file_write_v2(p.string(), id, s.manifest, sources_of(s), n, err, {}, &st);
            check(!ok && st.error == SessionError::invalid && !err.empty() && err.find(expect) != std::string::npos &&
                  !fs::exists(p) && no_temp(dir), label);
        };
        const auto pair_with = [&](std::function<void(Sample&)> tweak, const char* expect, const char* label) {
            Sample s = paired_sample();
            tweak(s);
            write_refuse(s, expect, label);
        };
        {   // a stage's live state saved in part
            Sample s = split_sample();
            drop(s, [](const SessionRegion& r) { return r.stage == 1 && r.part == 4; });
            write_refuse(s, "live state", "v2 partial live state refused, no file");
        }
        {   // a duplicated K/V part
            Sample s = split_sample();
            const auto it = std::find_if(s.manifest.regions.begin(), s.manifest.regions.end(),
                                          [](const SessionRegion& r) { return r.kind == int32_t(SessionRegionKind::kv) && r.stage == 0 && r.index == 1; });
            check(it != s.manifest.regions.end(), "v2 K/V region found");
            const size_t i = (size_t) (it - s.manifest.regions.begin());
            s.manifest.regions.push_back(s.manifest.regions[i]);
            s.data.push_back(s.data[i]);
            write_refuse(s, "declared twice", "v2 duplicated K/V part refused, no file");
        }
        {   // a layer outside its stage's carve
            Sample s = split_sample();
            add(s, int32_t(SessionRegionKind::kv), 0, 9, 0, 8, 1);
            write_refuse(s, "outside stage", "v2 K/V layer outside the stage's range refused, no file");
        }
        {   // a checkpoint part of a stage it does not cover
            Sample s = split_sample();
            s.manifest.checkpoints[0].stage_parts = 0;
            write_refuse(s, "it does not cover", "v2 checkpoint part without coverage refused, no file");
        }
        {   // a partial checkpoint coverage
            Sample s = split_sample();
            s.manifest.layout.stages.push_back({6, 9, 2});
            write_refuse(s, "later stages", "v2 checkpoint coverage not matching the stage count refused, no file");
        }
        {   // a prefix past the stored tokens
            Sample s = split_sample();
            s.manifest.checkpoints[2].prefix = 1001;
            write_refuse(s, "past the stored tokens", "v2 checkpoint prefix past the tokens refused, no file");
        }
        {   // a draft region with no draft
            Sample s = split_sample();
            s.manifest.draft = {};
            write_refuse(s, "no draft", "v2 dflash region with no draft refused, no file");
        }
        {   // counters outside the capacity, and appended below committed
            Sample s = split_sample();
            s.manifest.draft.committed_end = s.manifest.draft.capacity + 1;
            write_refuse(s, "capacity", "v2 draft counters over the capacity refused, no file");
            s = split_sample();
            s.manifest.draft.appended_through = 10;
            write_refuse(s, "capacity", "v2 draft counters below committed_end refused, no file");
        }
        {   // the paired capture's manifest rules
            pair_with([](Sample& s) { s.manifest.checkpoints[2].ring_committed_end = 999; },
                      "pairs the ring at position", "v2 pair captured off the checkpoint position refused, no file");
            pair_with([](Sample& s) { s.manifest.checkpoints[2].ring_cells = 1056; },
                      "not the file's draft", "v2 pair on a foreign ring layout refused, no file");
            pair_with([](Sample& s) { s.manifest.checkpoints[2].ring_appended_through = s.manifest.draft.capacity + 1; },
                      "outside the ring capacity", "v2 pair counters over the capacity refused, no file");
            pair_with([](Sample& s) { s.manifest.checkpoints[2].ring_appended_through = 999; },
                      "outside the ring capacity", "v2 pair appended below committed refused, no file");
            pair_with([](Sample& s) { s.manifest.checkpoints[2].ring_bytes = 1000; },
                      "its meta declares", "v2 pair region/meta size mismatch refused, no file");
            pair_with([](Sample& s) {
                          s.manifest.checkpoints[2].ring_bytes = 0;
                          for (size_t i = 0; i < s.manifest.regions.size(); ++i)
                              if (s.manifest.regions[i].kind == int32_t(SessionRegionKind::dflash_pair)) {
                                  s.manifest.regions[i].bytes = 0;
                                  s.data[i].clear();
                              }
                      },
                      "pair of no bytes", "v2 pair of no bytes refused, no file");
            pair_with([](Sample& s) { s.manifest.draft = {}; },
                      "pair with no draft", "v2 checkpoint pair with no draft in the file refused, no file");
            pair_with([](Sample& s) {
                          for (auto& r : s.manifest.regions)
                              if (r.kind == int32_t(SessionRegionKind::dflash_pair)) r.checksum = 1;
                      },
                      "checksum differs", "v2 pair region checksum differing from its meta refused, no file");
            pair_with([](Sample& s) {
                          for (auto& r : s.manifest.regions)
                              if (r.kind == int32_t(SessionRegionKind::dflash_pair)) r.stage = 1;
                      },
                      "names a stage or part", "v2 pair region naming a stage refused, no file");
            pair_with([](Sample& s) {
                          for (auto& r : s.manifest.regions)
                              if (r.kind == int32_t(SessionRegionKind::dflash_pair)) r.index = 5;
                      },
                      "names checkpoint 5", "v2 pair region naming a missing checkpoint refused, no file");
            {   // pair meta with no region, and a region with no pair meta
                Sample s = split_sample();
                auto& c = s.manifest.checkpoints[2];
                c.ring_id = 7; c.ring_bytes = 262144; c.ring_cells = s.manifest.draft.ring_cells;
                c.ring_window = s.manifest.draft.window; c.ring_capacity = s.manifest.draft.capacity;
                c.ring_committed_end = 1000; c.ring_appended_through = 1400;
                write_refuse(s, "pair with no region", "v2 pair meta with no region refused, no file");
                s = split_sample();
                add(s, int32_t(SessionRegionKind::dflash_pair), 0, 1, 0, 0, 132);
                write_refuse(s, "pair regions for 0 paired", "v2 pair region with no pair meta refused, no file");
            }
            {   // a target-only checkpoint carrying pair debris
                Sample s = split_sample();
                s.manifest.checkpoints[0].ring_bytes = 5;
                write_refuse(s, "target-only with pair bytes", "v2 target-only checkpoint with pair bytes refused, no file");
                s = split_sample();
                s.manifest.checkpoints[0].ring_window = 2048;
                write_refuse(s, "target-only with pair counters",
                             "v2 target-only checkpoint with pair counters refused, no file");
            }
        }
        {   // an image key out of order
            Sample s = split_sample();
            s.manifest.images[1].start = 0;
            write_refuse(s, "ascend", "v2 image keys not ascending refused, no file");
        }
        {   // a stage range that cannot name its layers
            Sample s = split_sample();
            s.manifest.layout.stages[1].layer_hi = 2;
            write_refuse(s, "layer range", "v2 inverted stage layer range refused, no file");
        }
        {   // a source that does not match its region
            Sample s = split_sample();
            auto src = sources_of(s);
            src[0].size += 1;
            const fs::path p = dir / "mismatch.bin";
            size_t n = 0;
            SessionStatus st;
            check(!session_file_write_v2(p.string(), id, s.manifest, src, n, error, {}, &st) &&
                  st.error == SessionError::invalid && error.find("bytes") != std::string::npos && !fs::exists(p) &&
                  no_temp(dir), "v2 source size mismatch refused, no file");
            src = sources_of(s);
            src[2].kind = int32_t(SessionRegionKind::mtp);
            check(!session_file_write_v2(p.string(), id, s.manifest, src, n, error, {}, &st) &&
                  error.find("names another region") != std::string::npos, "v2 source/region identity mismatch refused");
            src = sources_of(s);
            src.pop_back();
            check(!session_file_write_v2(p.string(), id, s.manifest, src, n, error, {}, &st) &&
                  error.find("sources given") != std::string::npos, "v2 missing source refused");
            src = sources_of(s);
            src.push_back(src.front());
            check(!session_file_write_v2(p.string(), id, s.manifest, src, n, error, {}, &st) &&
                  error.find("sources given") != std::string::npos, "v2 extra source refused");
        }
        {   // a failing source: no file, no temporary, the previous file intact
            Sample s = split_sample();
            auto src = sources_of(s);
            const fs::path p = dir / "keep.bin";
            check(session_file_write_v2(p.string(), id, s.manifest, src, written, error), "v2 keep: first write");
            const std::vector<char> image = slurp(p);
            src[0].read = [](size_t, void*, size_t) { return false; };
            src[0].error = [] { return std::string("device gone"); };
            SessionStatus st;
            check(!session_file_write_v2(p.string(), id, s.manifest, src, written, error, {}, &st) &&
                  st.error == SessionError::io && error.find("device gone") != std::string::npos,
                  "v2 failing source fails the write and says why");
            check(slurp(p) == image && no_temp(dir), "v2 failed streamed write keeps the previous file, no temporary");
        }
        // an impossible free-space reserve
        SessionWriteOptions opt;
        opt.min_free_bytes = UINT64_MAX / 2;
        SessionStatus st;
        const fs::path p = dir / "reserve.bin";
        check(!session_file_write_v2(p.string(), id, split.manifest, sources_of(split), written, error, opt, &st) &&
              st.error == SessionError::storage && error.find("disk space") != std::string::npos && !fs::exists(p) &&
              no_temp(dir), "v2 free-space reserve refused, no file");
        check(!session_file_write_v2((dir / "nope" / "x.bin").string(), id, split.manifest, sources_of(split), written, error),
              "v2 write into a missing directory fails");
    }
    // injected write faults: kind, publication, the old file, no temporary left
    {
        const fs::path p = dir / "fault.bin";
        const std::vector<char> old(10, 'q');
        struct Case { const char* step; int err; SessionError kind; bool published; };
        for (const Case c : {Case{"write", ENOSPC, SessionError::storage, false},
                             Case{"write", EIO, SessionError::io, false},
                             Case{"file_flush", EIO, SessionError::io, false},
                             Case{"rename", EACCES, SessionError::io, false},
                             Case{"dir_flush", EIO, SessionError::io, true}}) {
            spit(p, old);
            SessionWriteOptions opt;
            const std::string want_step = c.step;
            const int code = c.err;
            opt.fault = [want_step, code](const char* step) { return want_step == step ? code : 0; };
            SessionStatus st;
            const bool ok = session_file_write_v2(p.string(), id, split.manifest, sources_of(split), written, error, opt, &st);
            check(!ok && st.error == c.kind && st.published == c.published, "v2 fault: the failure has its kind and publication");
            if (c.published) check(slurp(p).size() == written && error.find("replaced") != std::string::npos,
                                   "v2 fault: a published failure leaves the new file and says so");
            else check(slurp(p) == old, "v2 fault: a failure before the rename keeps the old file");
            check(no_temp(dir), "v2 fault: no temporary left behind");
        }
    }
    // progress on the write: the start, every block, the last partial one
    {
        const fs::path p = dir / "progress.bin";
        std::vector<std::pair<uint64_t, uint64_t>> seen;
        std::vector<std::string> phases;
        SessionWriteOptions opt;
        opt.progress = [&](uint64_t d, uint64_t t) { seen.push_back({d, t}); };
        opt.phase = [&](const char* ph, uint64_t b) { phases.push_back(std::string(ph) + ":" + std::to_string(b)); };
        check(session_file_write_v2(p.string(), id, split.manifest, sources_of(split), written, error, opt), "v2 progress write");
        bool steps = seen.size() >= 2 && seen.front().first == 0 && seen.back().first == written;
        for (size_t i = 1; i < seen.size(); ++i)
            steps = steps && seen[i].first > seen[i - 1].first && seen[i].first - seen[i - 1].first <= (16u << 20);
        check(steps, "v2 write reports every block, at most 16 MiB apart, ending at the file size");
        check(phases.size() == 2 && phases[0].rfind("flush:", 0) == 0 && phases[1].rfind("publish:", 0) == 0,
              "v2 write announces flush and publish");
    }
    // direct I/O is attempted and the buffered path is honored: both write the same bytes and read back alike
    {
        const char* inherited = std::getenv("STRATA_SESSION_BUFFERED");
        const std::string kept = inherited ? inherited : "";
        set_env("STRATA_SESSION_BUFFERED", "1");
        const fs::path p = dir / "buffered.bin";
        check(session_file_write_v2(p.string(), id, split.manifest, sources_of(split), written, error), "v2 buffered write");
        check(slurp(p) == slurp(good), "v2 buffered write equals the default write");
        SessionManifestV2 back;
        Collector col;
        SessionRegionSink sink = collector_sink(col);
        SessionRegionSink* use = &sink;
        size_t read = 0;
        check(session_file_read_v2(p.string(), id, want, back, read, error, {}, use) && region_bytes_match(split, col),
              "v2 buffered read streams every region");
        set_env("STRATA_SESSION_BUFFERED", inherited ? kept.c_str() : nullptr);
    }
    // the checkpoint-boundary ring sidecar: paired capture bytes with the exact counters, fail-closed
    {
        const DFlashSnapshotMeta meta{1000, 1400, 2112, 2048, 8192};
        const std::vector<uint8_t> payload = bytes_of(4096, 5);
        const fs::path p = dir / "ring.dring";
        check(session_ring_sidecar_write(p.string(), meta, 1000, payload.data(), payload.size(), error), "ring sidecar write");
        std::vector<uint8_t> out;
        check(session_ring_sidecar_read(p.string(), meta, 1000, out, error) && out == payload, "ring sidecar round trip");
        const fs::path z = dir / "ring-empty.dring";
        check(session_ring_sidecar_write(z.string(), meta, 0, nullptr, 0, error), "ring sidecar write of an empty ring");
        check(session_ring_sidecar_read(z.string(), meta, 0, out, error) && out.empty(), "ring sidecar round trip of an empty ring");
        // an over-cap write is refused before the file is created
        std::vector<uint8_t> huge((size_t(64) << 20) + 1, 1);
        const fs::path hp = dir / "ring-huge.dring";
        check(!session_ring_sidecar_write(hp.string(), meta, 10, huge.data(), uint64_t(huge.size()), error) &&
              error.find("cap") != std::string::npos && !fs::exists(hp), "ring sidecar write over the 64 MiB cap refused, no file");
        huge.clear();
        // refusals: `out` keeps its sentinel every time
        const std::vector<uint8_t> pristine(3, 0xAB);
        auto refuse = [&](const fs::path& q, const DFlashSnapshotMeta& want_meta, int64_t tokens, const char* label) {
            std::vector<uint8_t> kept = pristine;
            std::string e;
            check(!session_ring_sidecar_read(q.string(), want_meta, tokens, kept, e) && !e.empty() &&
                  kept == pristine, label);
        };
        refuse(dir / "ring-gone.dring", meta, 1000, "ring sidecar: missing file refused");
        const std::vector<char> f = slurp(p);
        auto variant = [&](const std::function<void(std::vector<char>&)>& twist, const char* file) {
            auto d = f;
            twist(d);
            const fs::path q = dir / file;
            spit(q, d);
            return q;
        };
        refuse(variant([](std::vector<char>& d) { d.resize(80); }, "ring-cut.dring"), meta, 1000,
               "ring sidecar: header truncated refused");
        refuse(variant([](std::vector<char>& d) { d[0] = 'X'; reseal_sidecar(d); }, "ring-magic.dring"), meta, 1000,
               "ring sidecar: foreign magic refused");
        refuse(variant([](std::vector<char>& d) { d[8] = 3; reseal_sidecar(d); }, "ring-ver.dring"), meta, 1000,
               "ring sidecar: foreign version refused");
        refuse(variant([](std::vector<char>& d) { d[40] ^= 0x40; }, "ring-head.dring"), meta, 1000,
               "ring sidecar: corrupt header refused (header hash)");
        refuse(variant([](std::vector<char>& d) { put64(d, 16, 999); reseal_sidecar(d); }, "ring-tok.dring"), meta, 1000,
               "ring sidecar: token extent mismatch refused");
        refuse(variant([](std::vector<char>& d) { put64(d, 24, 999); reseal_sidecar(d); }, "ring-cnt.dring"), meta, 1000,
               "ring sidecar: counters that are not the pair's refused");
        refuse(variant([](std::vector<char>& d) { d.resize(d.size() - 100); }, "ring-trunc.dring"), meta, 1000,
               "ring sidecar: payload truncated refused");
        refuse(variant([](std::vector<char>& d) { d.push_back('x'); }, "ring-tail.dring"), meta, 1000,
               "ring sidecar: trailing bytes refused");
        refuse(variant([](std::vector<char>& d) { d[88 + 10] ^= 0x40; }, "ring-data.dring"), meta, 1000,
               "ring sidecar: payload checksum mismatch refused");
        refuse(variant([](std::vector<char>& d) { put64(d, 64, (uint64_t(64) << 20) + 1); reseal_sidecar(d); },
                        "ring-cap.dring"), meta, 1000, "ring sidecar: byte count over the cap refused");
        refuse(p, DFlashSnapshotMeta{1000, 1400, 1056, 2048, 8192}, 1000, "ring sidecar: foreign ring layout refused");
        refuse(p, meta, 999, "ring sidecar: resume extent mismatch refused");
    }
    // v1 keeps its own format: the v1 API writes and reads what it always did, with its own magic
    {
        SavedConversation image;
        image.layer_lo = 0; image.layer_hi = 1;
        image.live.ids = {1, 2, 3};
        image.live.gdn = bytes_of(64, 9);
        const fs::path p = dir / "v1-parity.bin";
        size_t n = 0;
        check(session_file_write(p.string(), image, id, n, error), "v1 write still works");
        SavedConversation back;
        check(session_file_read(p.string(), id, back, n, error) && back.live.ids == image.live.ids &&
              back.live.gdn == image.live.gdn, "v1 round trip still works");
        check(std::memcmp(slurp(p).data(), "STRSESS\x01", 8) == 0, "v1 keeps its own magic");
    }

    fs::remove_all(dir);
    std::printf("conversation_file_v2_test: %d checks passed\n", checks);
    return 0;
}
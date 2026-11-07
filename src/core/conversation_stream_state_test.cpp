// CPU-only tests for the caller-side streaming layer of format v2
// (include/strata/core/conversation_stream_state.hpp): the region list for one parked conversation, the region
// sources, the sink dispatcher, and their round trip through the real v2 writer/reader.
// Built with -DSTRATA_BUILD_CONVERSATION_TESTS=ON; no CUDA, no model, no GPU.
#include "strata/core/conversation_stream_state.hpp"
#include "strata/core/conversation_file.hpp"

#include <algorithm>
#include <array>
#include <cstdio>
#include <ranges>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <string>
#include <utility>
#include <vector>

using namespace strata::core;
namespace fs = std::filesystem;

namespace {
int checks = 0;
void check(bool ok, const char* label) {
    if (!ok) { std::fprintf(stderr, "FAIL: %s\n", label); std::exit(1); }
    ++checks;
}

std::string key(const SessionRegion& r) {
    return std::string(session_region_kind_name(r.kind)) + ":" + std::to_string(r.stage) + ":" +
           std::to_string(r.index) + ":" + std::to_string(r.part);
}
bool canonical_sorted(const std::vector<SessionRegion>& rs) {
    for (size_t i = 1; i < rs.size(); ++i) {
        const auto& a = rs[i - 1];
        const auto& b = rs[i];
        if (a.kind > b.kind) return false;
        if (a.kind == b.kind && a.stage > b.stage) return false;
        if (a.kind == b.kind && a.stage == b.stage && a.index > b.index) return false;
        if (a.kind == b.kind && a.stage == b.stage && a.index == b.index && a.part >= b.part) return false;
    }
    return true;
}
std::vector<uint8_t> payload(size_t n, uint8_t seed) {
    std::vector<uint8_t> v(n);
    for (size_t i = 0; i < n; ++i) v[i] = uint8_t(i * 197u + seed * 31u + 7u);
    return v;
}

// a synthetic conversation: two logical stages, split checkpoint parts, the drafter's layer on the last stage, a ring
SessionStreamSpec spec() {
    SessionStreamSpec s;
    for (size_t i = 0; i < s.geometry.size(); ++i) s.geometry[i] = int64_t(8 + i);
    SessionStreamStage a;
    a.layout = {0, 3, 2};
    a.live = {1000, 64, 24, 12, 6};
    a.kv.assign(3, {512, 512, 64, 64, 256});
    s.stages.push_back(a);
    SessionStreamStage b;
    b.layout = {3, 6, 2};
    b.live = {500, 0, 24, 12, 6};
    b.kv.assign(3, {512, 512, 0, 0, 128});
    b.has_mtp = true;
    b.mtp = {1024, 1024, 0, 0, 128};
    s.stages.push_back(b);
    s.draft = {1, 2048, 512, 4096, 180, 200, 0x5eed5eed5eed5eedull};
    s.has_dflash = true;
    s.dflash_bytes = 4096;
    s.cvec = true;
    for (size_t i = 0; i < 200; ++i) s.tokens.push_back(int32_t(500 + i));
    s.images = {{10, 0x1234ull}, {80, 0x2345ull}};
    SessionStreamCheckpoint c0;
    c0.meta = {10, 3, true, 1, {4096, 0, 100, 0, 0.5, true, false}};
    c0.state = {{1000, 64, 24, 12, 6}, {500, 0, 24, 12, 6}};
    SessionStreamCheckpoint c1;
    c1.meta = {200, 42, false, 1, {0, -1, -1, 0, 3.25, false, false}};
    c1.state = {{1000, 64, 24, 12, 6}, {500, 0, 24, 12, 6}};
    s.checkpoints = {c0, c1};
    return s;
}

// the bytes of one region, keyed by what it is
struct Payload {
    std::map<std::string, std::vector<uint8_t>> by_key;
    uint8_t seed = 1;
    const std::vector<uint8_t>& of(const SessionRegion& r) {
        auto it = by_key.find(key(r));
        if (it == by_key.end()) it = by_key.emplace(key(r), payload(r.bytes, seed++)).first;
        return it->second;
    }
};

std::vector<SessionRegionSource> sources_for(const SessionManifestV2& m, Payload& p) {
    std::vector<SessionRegionSource> out;
    SessionStreamReader read = [&p](const SessionRegion& r, uint64_t offset, void* dst, size_t n) {
        const std::vector<uint8_t>& d = p.of(r);
        if (offset > d.size() || n > d.size() - offset) return false;
        std::memcpy(dst, d.data() + offset, n);
        return true;
    };
    std::string built;
    check(session_stream_sources(out, m, read, built) && built.empty(), "sources built");
    return out;
}

// the destinations of a restore: everything the spec declares, keyed and collected
struct Collector {
    std::map<std::string, std::vector<uint8_t>> got;
    std::vector<std::string> validated;
    std::vector<std::string> finished;
    uint64_t max_block = 0;
    std::string refuse_key;
};
std::vector<SessionStreamDestination> dests_for(const SessionManifestV2& m, Collector& c) {
    std::vector<SessionStreamDestination> out;
    auto mk = [&](int32_t kind, int64_t stage, int64_t index, std::string tag) {
        SessionStreamDestination d;
        d.kind = kind; d.stage = stage; d.index = index;
        d.validate = [&c](const SessionRegion& r, std::string& why) {
            c.validated.push_back(key(r));
            c.got[key(r)] = {};
            if (!c.refuse_key.empty() && key(r) == c.refuse_key) { why = "the destination is full"; return false; }
            return true;
        };
        d.write = [&c](const SessionRegion& r, uint64_t offset, const void* p, size_t n, std::string&) {
            c.max_block = std::max(c.max_block, uint64_t(n));
            auto& v = c.got[key(r)];
            if (v.size() != offset) return false;
            v.insert(v.end(), (const uint8_t*) p, (const uint8_t*) p + n);
            return true;
        };
        d.finish = [&c, tag](std::string&) { c.finished.push_back(tag); return true; };
        out.push_back(std::move(d));
    };
    for (int64_t s = 0; s < int64_t(m.layout.stages.size()); ++s) {
        mk(int32_t(SessionRegionKind::live_state), s, -1, "live" + std::to_string(s));
        mk(int32_t(SessionRegionKind::kv), s, -1, "kv" + std::to_string(s));
        mk(int32_t(SessionRegionKind::mtp), s, -1, "mtp" + std::to_string(s));
    }
    mk(int32_t(SessionRegionKind::checkpoint_state), -1, -1, "checks");
    mk(int32_t(SessionRegionKind::dflash), 0, 0, "dflash");
    return out;
}
SessionRegionSink sink_of(std::vector<SessionStreamDestination>& dests, std::string& why) {
    SessionRegionSink s;
    check(session_stream_sink(std::move(dests), s, why), "sink built");
    return s;
}

std::vector<char> slurp(const fs::path& p) {
    std::ifstream f(p, std::ios::binary);
    return std::vector<char>((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}
void spit(const fs::path& p, const std::vector<char>& d) {
    std::ofstream f(p, std::ios::binary | std::ios::trunc);
    f.write(d.data(), (std::streamsize) d.size());
}
uint64_t field64(const std::vector<char>& f, size_t at) {
    uint64_t v = 0;
    std::memcpy(&v, f.data() + at, 8);
    return v;
}

void test_builder() {
    const SessionStreamSpec s = spec();
    SessionManifestV2 m;
    std::string error;
    check(session_stream_manifest(s, m, error), "builder accepts the spec");
    check(error.empty(), "builder reports nothing");
    check(canonical_sorted(m.regions), "the region directory is in canonical order");
    check(m.layout.geometry == s.geometry, "geometry travels");
    check(m.layout.stages.size() == 2 && m.layout.stages[1].layer_lo == 3, "stages travel");
    check(m.draft == s.draft, "draft identity travels");
    check(m.tokens == s.tokens && m.images == s.images, "token/image directory travels");
    check(m.checkpoints.size() == 2 && m.checkpoints[0].pinned && m.checkpoints[1].prefix == 200, "checkpoint meta travels");
    check(m.checkpoints[0].plan.pipe == 0.5 && m.checkpoints[1].plan.to == -1, "plan bits travel");
    size_t live = 0, kv = 0, cps = 0, mtp = 0, dfl = 0;
    for (const auto& r : m.regions) {
        check(r.offset == 0 && r.checksum == 0, "the writer fills offsets and checksums");
        if (r.kind == int32_t(SessionRegionKind::live_state)) ++live;
        else if (r.kind == int32_t(SessionRegionKind::kv)) ++kv;
        else if (r.kind == int32_t(SessionRegionKind::checkpoint_state)) ++cps;
        else if (r.kind == int32_t(SessionRegionKind::mtp)) ++mtp;
        else if (r.kind == int32_t(SessionRegionKind::dflash)) ++dfl;
        else check(false, "unknown kind from the builder");
    }
    check(live == 10, "five live-state parts per stage");
    check(kv == 30, "five regions per owned layer");
    check(cps == 20, "a checkpoint body per stage it covers");
    check(mtp == 5, "the drafter's layer once");
    check(dfl == 1, "the ring once");
}

void test_builder_refusals() {
    std::string error;
    SessionManifestV2 m;
    SessionStreamSpec s = spec();
    s.stages.clear();
    check(!session_stream_manifest(s, m, error) && !error.empty(), "no stage is refused");

    s = spec();
    s.stages[0].layout.layer_hi = 4;   // the range names 4 layers, the rows name 3
    check(!session_stream_manifest(s, m, error) && !error.empty(), "layer rows disagreeing with the range are refused");

    s = spec();
    s.stages[0].layout.layer_hi = 4;   // the range names 4 layers, the rows name 3
    check(!session_stream_manifest(s, m, error) && !error.empty(), "layer rows disagreeing with the range are refused");

    s = spec();
    s.checkpoints[0].meta.stage_parts = 0;
    s.checkpoints[1].meta.stage_parts = 0;
    check(session_stream_manifest(s, m, error), "a checkpoint covering no later stage is legal");
    check(std::ranges::count_if(m.regions, [](const SessionRegion& r) {
              return r.kind == int32_t(SessionRegionKind::checkpoint_state);
          }) == 10,
          "a checkpoint covering no later stage saves only its first stage's body");

    s = spec();
    s.checkpoints[0].meta.stage_parts = 3;
    check(!session_stream_manifest(s, m, error) && !error.empty(),
          "a checkpoint covering a number of later stages the layout does not have is refused");

    s = spec();
    s.checkpoints[0].meta.prefix = 400;
    check(!session_stream_manifest(s, m, error) && !error.empty(), "a prefix past the tokens is refused");

    s = spec();
    s.stages[0].has_mtp = true;
    check(!session_stream_manifest(s, m, error) && !error.empty(), "two stages owning the drafter are refused");

    s = spec();
    s.has_dflash = false;
    s.dflash_bytes = 0;
    check(!session_stream_manifest(s, m, error) && !error.empty(), "a draft with no ring region is refused");

    s = spec();
    s.draft = {};
    check(!session_stream_manifest(s, m, error) && !error.empty(), "a ring region with no draft is refused");

    s = spec();
    s.images.back().start = 400;
    check(!session_stream_manifest(s, m, error) && !error.empty(), "an image key past the tokens is refused");

    s = spec();
    s.images = {{80, 1}, {10, 2}};
    check(!session_stream_manifest(s, m, error) && !error.empty(), "descending image keys are refused");

    s = spec();
    s.checkpoints[0].state[1][0] = 5000;   // bigger than stage 1's live gdn
    check(!session_stream_manifest(s, m, error) && !error.empty(), "a checkpoint body larger than the stage is refused");

    s = spec();
    s.checkpoints[0].state.resize(1);
    check(!session_stream_manifest(s, m, error) && !error.empty(), "a checkpoint of the wrong stage count is refused");

    s = spec();
    s.stages[1].live[1] = 0;
    s.checkpoints[1].state[1][1] = 64;   // the stage has no PLE history
    check(!session_stream_manifest(s, m, error) && !error.empty(), "a PLE body a stage cannot hold is refused");
}

void test_round_trip(const fs::path& dir) {
    const SessionStreamSpec s = spec();
    SessionManifestV2 m;
    std::string error;
    check(session_stream_manifest(s, m, error), "manifest for the round trip");

    Payload p;
    std::map<std::string, std::vector<uint8_t>> expected;
    for (const auto& r : m.regions) expected[key(r)] = p.of(r);

    const SessionFileIdentity id{0xfeedull, 0xbeefull};
    const std::vector<SessionRegionSource> sources = sources_for(m, p);
    check(sources.size() == m.regions.size(), "one source per region");
    size_t bytes = 0;
    const fs::path file = dir / "generation.strata";
    SessionStatus st;
    check(session_file_write_v2(file.string(), id, m, sources, bytes, error, {}, &st), "the streamed write lands");
    check(st.error == SessionError::none && bytes > 0, "the write reports its size");
    check(m.regions.front().checksum == 0, "the caller's manifest is not rewritten");

    // pass 1 as the engine runs it: sinkless validation, then the manifest drives the destinations
    SessionManifestV2 got;
    size_t rb = 0;
    check(session_file_read_v2(file.string(), id, {true, true, m.layout, m.draft}, got, rb, error), "validation-only read");
    check(got.regions.size() == m.regions.size(), "every region came back");
    uint64_t region_bytes = 0;
    for (size_t i = 0; i < got.regions.size(); ++i) {
        check(got.regions[i].bytes == m.regions[i].bytes, "region sizes round-trip");
        check(got.regions[i].checksum == session_hash64(expected[key(m.regions[i])].data(),
                                                         expected[key(m.regions[i])].size(), 0),
              "the reader reports the checksum of each region's bytes");
        region_bytes += got.regions[i].bytes;
    }
    check(got.layout == m.layout && got.draft == m.draft && got.tokens == m.tokens && got.images == m.images &&
              got.checkpoints.size() == m.checkpoints.size() && got.cvec == m.cvec,
          "the manifest a restore reads matches the spec");

    // pass 2 through the sink dispatcher
    Collector c;
    std::string why;
    std::vector<SessionStreamDestination> dests = dests_for(got, c);
    SessionRegionSink sink = sink_of(dests, why);
    SessionManifestV2 out;
    size_t tb = 0;
    check(session_file_read_v2(file.string(), id, {true, true, m.layout, m.draft}, out, tb, error, {}, &sink),
          "the streamed restore lands");
    check(c.got.size() == m.regions.size(), "every destination took its region");
    bool same = true;
    for (const auto& [k, v] : expected) {
        auto it = c.got.find(k);
        if (it == c.got.end() || it->second != v) same = false;
    }
    check(same, "every region's bytes arrived unchanged, in canonical order");
    check(c.validated.size() == m.regions.size(), "validate_region was asked for every region");
    check(c.finished.size() == 3 * m.layout.stages.size() + 2 && c.finished.front() == "live0",
          "each destination finished once, in order");
    check(c.max_block <= (16u << 20), "blocks stay bounded");
    (void) region_bytes;

    // a destination that refuses one region leaves every destination untouched
    Collector bad;
    bad.refuse_key = key(m.regions.front());
    std::vector<SessionStreamDestination> bad_dests = dests_for(got, bad);
    SessionRegionSink bad_sink = sink_of(bad_dests, why);
    SessionManifestV2 none;
    error.clear();
    check(!session_file_read_v2(file.string(), id, {true, true, m.layout, m.draft}, none, rb, error, {}, &bad_sink),
          "a refused region refuses the whole restore");
    check(bad.got.empty() || std::all_of(bad.got.begin(), bad.got.end(),
                                         [](const auto& e) { return e.second.empty(); }),
          "a refused restore wrote nothing");

    // a damaged region: the region checksum finds it and the sink sees no byte
    std::vector<char> f = slurp(file);
    const uint64_t base = 64 + field64(f, 40);
    f[size_t(base) + 5] = uint8_t(f[size_t(base) + 5] ^ 0xa5);
    spit(file, f);
    Collector dmg;
    std::vector<SessionStreamDestination> dmg_dests = dests_for(got, dmg);
    SessionRegionSink dmg_sink = sink_of(dmg_dests, why);
    error.clear();
    check(!session_file_read_v2(file.string(), id, {true, true, m.layout, m.draft}, out, rb, error, {}, &dmg_sink),
          "a damaged region is refused");
    check(error.find("checksum") != std::string::npos, "the refusal names the region's checksum");
    check(std::all_of(dmg.got.begin(), dmg.got.end(), [](const auto& e) { return e.second.empty(); }),
          "a damaged region leaves every destination untouched");
}

void test_source_helpers() {
    std::string error;
    // a host view: the checkpoint body stays where it is
    std::vector<uint8_t> body = payload(1000, 3);
    SessionRegionSource host = session_stream_host_source(int32_t(SessionRegionKind::checkpoint_state), 0, 1, 0,
                                                          body.data(), body.size());
    check(host.size == 1000, "the host view knows its extent");
    uint8_t out[16] = {};
    check(host.read(10, out, 16) && std::memcmp(out, body.data() + 10, 16) == 0, "the host view reads its bytes");
    check(!host.read(990, out, 16), "a host view refuses a read past its end");

    // a concatenated region: the dflash ring's states in order
    std::vector<SessionStreamPiece> pieces;
    std::vector<uint8_t> a = payload(10, 11), b = payload(20, 12);
    pieces.push_back({a.size(), [&a](uint64_t off, void* dst, size_t n) {
                          if (off > a.size() || n > a.size() - off) return false;
                          std::memcpy(dst, a.data() + off, n);
                          return true;
                      }});
    pieces.push_back({b.size(), [&b](uint64_t off, void* dst, size_t n) {
                          if (off > b.size() || n > b.size() - off) return false;
                          std::memcpy(dst, b.data() + off, n);
                          return true;
                      }});
    SessionRegionSource cat;
    check(session_stream_concat_source(cat, int32_t(SessionRegionKind::dflash), 0, 0, 0, pieces, error), "pieces concatenate");
    check(cat.size == 30, "the concatenated region is the sum of its pieces");
    uint8_t span[12] = {};
    check(cat.read(6, span, 12) && std::memcmp(span, a.data() + 6, 4) == 0 &&
              std::memcmp(span + 4, b.data(), 8) == 0, "a read spanning the pieces crosses them in order");
    check(!cat.read(24, span, 8), "the concatenated region refuses a read past its end");
    SessionRegionSource none;
    check(!session_stream_concat_source(none, int32_t(SessionRegionKind::dflash), 0, 0, 0, {{10, {}}}, error),
          "a piece that cannot be read is refused");

    // a K/V layer's streamed source becomes its five region sources
    SessionKvSource kv;
    kv.sizes = {100, 100, 16, 16, 64};
    auto store = std::make_shared<std::vector<uint8_t>>(340, 0x5a);
    auto sizes = std::make_shared<std::array<size_t, 5>>(std::array<size_t, 5>{100, 100, 16, 16, 64});
    kv.read = [store, sizes](size_t part, size_t offset, void* dst, size_t n) {
        static const size_t first[5] = {0, 100, 200, 216, 232};
        const size_t at = first[part] + offset;
        if (offset > (*sizes)[part] || n > (*sizes)[part] - offset) return false;
        std::memcpy(dst, store->data() + at, n);
        return true;
    };
    kv.error = [] { return std::string("fixture pool"); };
    std::vector<SessionRegionSource> parts;
    check(session_stream_kv_sources(parts, int32_t(SessionRegionKind::kv), 1, 2, kv, error), "the layer's sources are built");
    check(parts.size() == 5 && parts[3].size == 16 && parts[3].stage == 1 && parts[3].index == 2 && parts[3].part == 3,
          "the parts are named by the region they feed");
    uint8_t v[8] = {};
    check(parts[1].read(0, v, 8) && v[0] == 0x5a, "a K/V region reads its part");
    check(!parts[0].read(96, v, 8), "a K/V region stays inside its part");
}

void test_reader_failure(const fs::path& dir) {
    const SessionStreamSpec s = spec();
    SessionManifestV2 m;
    std::string error;
    check(session_stream_manifest(s, m, error), "manifest for the failing reader");
    std::vector<SessionRegionSource> sources;
    int seen = 0;
    SessionStreamReader read = [&](const SessionRegion& r, uint64_t offset, void* dst, size_t n) {
        if (r.kind == int32_t(SessionRegionKind::dflash)) return false;   // the ring pool stops answering
        std::memset(dst, 0x33, n);
        ++seen;
        return true;
    };
    check(session_stream_sources(sources, m, read, error), "sources built for the failing reader");
    size_t bytes = 0;
    const fs::path file = dir / "failure.strata";
    check(!session_file_write_v2(file.string(), {1, 2}, m, sources, bytes, error), "a source that fails fails the write");
    check(error.find("dflash") != std::string::npos, "the failure names the region whose source failed");
    check(!fs::exists(file), "nothing was published");
    (void) seen;
}
} // namespace

int main() {
    const fs::path dir = fs::temp_directory_path() / "strata_stream_state_test";
    std::filesystem::remove_all(dir);
    std::filesystem::create_directories(dir);
    test_builder();
    test_builder_refusals();
    test_source_helpers();
    test_round_trip(dir);
    test_reader_failure(dir);
    std::filesystem::remove_all(dir);
    std::fprintf(stderr, "conversation_stream_state_test: %d checks passed\n", checks);
    return 0;
}

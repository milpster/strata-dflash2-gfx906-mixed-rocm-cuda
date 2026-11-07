#include "strata/core/conversation_stream_state.hpp"

#include <algorithm>
#include <cstring>
#include <limits>

namespace strata::core {
namespace {

constexpr uint64_t kMaxU64 = std::numeric_limits<uint64_t>::max();

bool sat_add(uint64_t a, uint64_t b, uint64_t& out) {
    if (a > kMaxU64 - b) return false;
    out = a + b;
    return true;
}

std::string region_name(const SessionRegion& r) {
    return std::string(" (") + session_region_kind_name(r.kind) + ", stage " + std::to_string(r.stage) +
           ", index " + std::to_string(r.index) + ", part " + std::to_string(r.part) + ")";
}

const SessionStreamDestination* find(const std::vector<SessionStreamDestination>& dests, const SessionRegion& r) {
    for (const auto& d : dests)
        if (d.kind == r.kind && (d.stage < 0 || d.stage == r.stage) && (d.index < 0 || d.index == r.index))
            return &d;
    return nullptr;
}

// one region's bytes, read from a host span: [offset, offset+n) must lie inside it
bool host_span_read(const uint8_t* base, uint64_t size, uint64_t offset, void* dst, size_t n) {
    if (offset > size || n > size - offset) return false;
    std::memcpy(dst, base + offset, n);
    return true;
}

} // namespace

bool session_stream_manifest(const SessionStreamSpec& spec, SessionManifestV2& out, std::string& error) {
    auto refuse = [&](const std::string& what) {
        if (error.empty()) error = "session stream: " + what;
        return false;
    };
    const int64_t stages = (int64_t) spec.stages.size();
    if (!stages) return refuse("no stage");
    if (stages > int64_t(kSessionMaxStages)) return refuse("the layout names " + std::to_string(stages) + " stages");
    for (size_t s = 0; s < spec.stages.size(); ++s) {
        const auto& st = spec.stages[s];
        if (st.layout.layer_lo < 0 || st.layout.layer_hi < st.layout.layer_lo)
            return refuse("stage " + std::to_string(s) + " layer range " + std::to_string(st.layout.layer_lo) + ".." +
                          std::to_string(st.layout.layer_hi));
        if (int64_t(st.kv.size()) != st.layout.layer_hi - st.layout.layer_lo)
            return refuse("stage " + std::to_string(s) + " names " + std::to_string(st.kv.size()) +
                          " K/V layers, its range holds " + std::to_string(st.layout.layer_hi - st.layout.layer_lo));
    }
    int mtp_stages = 0;
    for (const auto& st : spec.stages) mtp_stages += st.has_mtp ? 1 : 0;
    if (mtp_stages > 1) return refuse("the drafter's layer is owned by " + std::to_string(mtp_stages) + " stages");
    if (mtp_stages && spec.draft.type < 0) return refuse("the drafter's layer with a negative draft type");
    if (spec.draft.type < 0) return refuse("negative draft type");
    const bool ring = spec.draft.type > 0;
    if (ring != spec.has_dflash)
        return refuse(ring ? "a draft with no ring region" : "a ring region with no draft");
    if (spec.has_dflash && !spec.dflash_bytes) return refuse("a ring region of no bytes");
    for (size_t i = 0; i < spec.checkpoints.size(); ++i) {
        const auto& c = spec.checkpoints[i];
        if (c.state.size() != spec.stages.size())
            return refuse("checkpoint of " + std::to_string(c.state.size()) + " stages, the layout has " +
                          std::to_string(spec.stages.size()));
        if (c.meta.prefix > spec.tokens.size()) return refuse("a checkpoint prefix reaches past the stored tokens");
        if (c.meta.stage_parts != 0 && c.meta.stage_parts != stages - 1)
            return refuse("a checkpoint covers " + std::to_string(c.meta.stage_parts) + " of " +
                          std::to_string(stages - 1) + " later stages");
        // a checkpoint body is one stage's running state at a shorter prefix: never bigger than that stage's live state
        for (size_t s = 0; s < c.state.size(); ++s)
            for (size_t p = 0; p < kSessionStateParts; ++p)
                if (c.state[s][p] > spec.stages[s].live[p])
                    return refuse("checkpoint " + std::to_string(i) + " of stage " + std::to_string(s) + " part " +
                                  std::to_string(p) + " is " + std::to_string(c.state[s][p]) +
                                  " bytes, the stage's live state is " + std::to_string(spec.stages[s].live[p]));
    }
    for (size_t i = 0; i < spec.images.size(); ++i) {
        if (spec.images[i].start < 0 || (i && spec.images[i].start <= spec.images[i - 1].start))
            return refuse("image keys must start above zero and ascend");
        if (spec.images[i].start >= (int64_t) spec.tokens.size())
            return refuse("an image key starts past the stored tokens");
    }

    out = {};
    out.layout.geometry = spec.geometry;
    for (const auto& st : spec.stages) out.layout.stages.push_back(st.layout);
    out.draft = spec.draft;
    out.cvec = spec.cvec;
    out.tokens = spec.tokens;
    out.images = spec.images;
    auto add = [&](int32_t kind, int64_t stage, int64_t index, int64_t part, uint64_t bytes) {
        SessionRegion r;
        r.kind = kind; r.stage = stage; r.index = index; r.part = part; r.bytes = bytes;
        out.regions.push_back(r);
        return true;
    };
    const int32_t live = int32_t(SessionRegionKind::live_state), kv = int32_t(SessionRegionKind::kv),
                  cps = int32_t(SessionRegionKind::checkpoint_state), mtp = int32_t(SessionRegionKind::mtp),
                  dfl = int32_t(SessionRegionKind::dflash);
    // the canonical order format v2 requires: by kind, then stage, then index, then part
    for (int64_t s = 0; s < stages; ++s)
        for (int64_t p = 0; p < kSessionStateParts; ++p)
            add(live, s, 0, p, spec.stages[(size_t) s].live[(size_t) p]);
    for (int64_t s = 0; s < stages; ++s)
        for (size_t i = 0; i < spec.stages[(size_t) s].kv.size(); ++i)
            for (int64_t p = 0; p < kSessionKvParts; ++p)
                add(kv, s, int64_t(i), p, spec.stages[(size_t) s].kv[i][(size_t) p]);
    for (int64_t s = 0; s < stages; ++s)
        for (size_t i = 0; i < spec.checkpoints.size(); ++i) {
            const auto& c = spec.checkpoints[i];
            if (s > 0 && c.meta.stage_parts == 0) continue;   // a checkpoint covers no later stage
            for (int64_t p = 0; p < kSessionStateParts; ++p)
                add(cps, s, int64_t(i), p, c.state[(size_t) s][(size_t) p]);
        }
    for (int64_t s = 0; s < stages; ++s) {
        if (!spec.stages[(size_t) s].has_mtp) continue;
        for (int64_t p = 0; p < kSessionKvParts; ++p)
            add(mtp, s, 0, p, spec.stages[(size_t) s].mtp[(size_t) p]);
    }
    if (spec.has_dflash) add(dfl, 0, 0, 0, spec.dflash_bytes);

    out.checkpoints.reserve(spec.checkpoints.size());
    for (const auto& c : spec.checkpoints) out.checkpoints.push_back(c.meta);

    uint64_t total = 0;
    for (const auto& r : out.regions)
        if (!sat_add(total, r.bytes, total)) return refuse("the region byte count overflows");
    return true;
}

bool session_stream_sources(std::vector<SessionRegionSource>& out, const SessionManifestV2& manifest,
                            const SessionStreamReader& read, std::string& error) {
    if (!read) { error = "session stream: no region reader"; return false; }
    out.clear();
    out.reserve(manifest.regions.size());
    for (const auto& r : manifest.regions) {
        if (r.kind < int32_t(SessionRegionKind::live_state) || r.kind > int32_t(SessionRegionKind::dflash)) {
            error = "session stream: region" + region_name(r) + " has a kind format v2 does not define";
            return false;
        }
        SessionRegionSource s;
        s.kind = r.kind; s.stage = r.stage; s.index = r.index; s.part = r.part; s.size = r.bytes;
        const SessionRegion one = r;
        s.read = [read, one](size_t offset, void* dst, size_t n) {
            return read(one, uint64_t(offset), dst, n);
        };
        out.push_back(std::move(s));
    }
    return true;
}

SessionRegionSource session_stream_host_source(int32_t kind, int64_t stage, int64_t index, int64_t part,
                                               const void* bytes, uint64_t size) {
    SessionRegionSource s;
    s.kind = kind; s.stage = stage; s.index = index; s.part = part; s.size = size;
    const uint8_t* base = static_cast<const uint8_t*>(bytes);
    s.read = [base, size](size_t offset, void* dst, size_t n) {
        return host_span_read(base, size, uint64_t(offset), dst, n);
    };
    s.error = [] { return std::string("host source: read out of range"); };
    return s;
}

bool session_stream_concat_source(SessionRegionSource& out, int32_t kind, int64_t stage, int64_t index, int64_t part,
                                  std::vector<SessionStreamPiece> pieces, std::string& error) {
    uint64_t total = 0;
    for (const auto& p : pieces) {
        if (!p.read) { error = "session stream: a piece of a concatenated region cannot be read"; return false; }
        if (!sat_add(total, p.size, total)) { error = "session stream: a concatenated region overflows"; return false; }
    }
    SessionRegionSource s;
    s.kind = kind; s.stage = stage; s.index = index; s.part = part; s.size = total;
    auto why = std::make_shared<std::string>();
    auto table = std::make_shared<std::vector<SessionStreamPiece>>(std::move(pieces));
    s.read = [table, why](size_t offset, void* dst, size_t n) {
        uint8_t* d = static_cast<uint8_t*>(dst);
        uint64_t at = offset, left = n;
        for (const auto& p : *table) {
            if (at >= p.size) { at -= p.size; continue; }
            const size_t take = size_t(std::min<uint64_t>(p.size - at, left));
            if (!p.read(at, d, take)) { *why = "read at " + std::to_string(at); return false; }
            d += take;
            left -= take;
            at = 0;
            if (!left) return true;
        }
        *why = "the region ends at " + std::to_string(offset);
        return false;
    };
    s.error = [why] { return *why; };
    out = std::move(s);
    return true;
}

bool session_stream_kv_sources(std::vector<SessionRegionSource>& out, int32_t kind, int64_t stage, int64_t index,
                               const SessionKvSource& kv, std::string& error) {
    if (!kv.read) { error = "session stream: the K/V source cannot be read"; return false; }
    for (size_t part = 0; part < kSessionKvParts; ++part) {
        SessionRegionSource s;
        s.kind = kind; s.stage = stage; s.index = index; s.part = int64_t(part); s.size = kv.sizes[part];
        const size_t p = part;
        auto read = kv.read;
        auto why = kv.error;
        s.read = [read, why, p](size_t offset, void* dst, size_t n) { return read(p, offset, dst, n); };
        s.error = [why] { return why ? why() : std::string(); };
        out.push_back(std::move(s));
    }
    return true;
}

bool session_stream_sink(std::vector<SessionStreamDestination> dests, SessionRegionSink& sink, std::string& error) {
    for (const auto& d : dests) {
        if (!d.validate || !d.write || !d.finish) {
            error = "session stream: destination " + std::string(session_region_kind_name(d.kind)) + " is incomplete";
            return false;
        }
    }
    auto dests_shared = std::make_shared<std::vector<SessionStreamDestination>>(std::move(dests));
    sink.validate_region = [dests_shared](const SessionRegion& r, std::string& why) {
        const auto* d = find(*dests_shared, r);
        if (d == nullptr) {
            why = "nothing writes the bytes of region" + region_name(r);
            return false;
        }
        return d->validate(r, why);
    };
    sink.write = [dests_shared](const SessionRegion& r, uint64_t offset, const void* bytes, size_t n, std::string& why) {
        const auto* d = find(*dests_shared, r);
        if (d == nullptr) {
            why = "nothing writes the bytes of region" + region_name(r);
            return false;
        }
        return d->write(r, offset, bytes, n, why);
    };
    sink.finish = [dests_shared](std::string& why) {
        for (auto& d : *dests_shared)
            if (!d.finish(why)) return false;
        return true;
    };
    error.clear();
    return true;
}

} // namespace strata::core

// src/core/conversation_disk_cache.cpp - the automatic disk tier's index, quota, publication and restart inventory.
// Pure host code: no device call, no session, no payload.  The bytes are the files conversation_file writes and reads.
#include "strata/core/conversation_disk_cache.hpp"

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>

#ifndef _WIN32
#include <csignal>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#define O_BINARY 0
#else
#include <fcntl.h>
#include <io.h>
#include <windows.h>
#endif

namespace strata::core {
namespace {

// the fixed host cost of one index entry (the struct itself and the vectors' capacities)
constexpr uint64_t kEntryOverhead = 128;
constexpr size_t kReserveGuard = 64;   // the eviction loop's bound: one entry per pass

uint64_t sat_add(uint64_t a, uint64_t b) { return b > UINT64_MAX - a ? UINT64_MAX : a + b; }

bool is_ours(const std::string& name, const char* prefix, const char* suffix, uint64_t& id) {
    const size_t pl = std::strlen(prefix), sl = std::strlen(suffix);
    if (name.size() <= pl + sl || name.rfind(prefix, 0) != 0 || name.compare(name.size() - sl, sl, suffix) != 0)
        return false;
    const std::string digits = name.substr(pl, name.size() - pl - sl);
    if (digits.empty() || !std::all_of(digits.begin(), digits.end(), [](char c) { return c >= '0' && c <= '9'; }))
        return false;
    id = std::strtoull(digits.c_str(), nullptr, 10);
    return true;
}

#ifndef _WIN32
bool process_alive(uint64_t pid) { return pid != 0 && ::kill((pid_t) pid, 0) == 0; }   // EPERM: alive, not ours
#else
bool process_alive(uint64_t pid) {
    if (pid == 0) return false;
    const HANDLE h = ::OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, (DWORD) pid);
    if (h == nullptr) return false;
    ::CloseHandle(h);
    return true;
}
#endif

}   // namespace

uint64_t disk_generation_index_bytes(const DiskGeneration& g) {
    uint64_t n = kEntryOverhead + g.path.size() + g.tokens.size() * 4 + g.images.size() * sizeof(ConversationImageKey) +
                 g.ring_ids.size() * 8;
    return n == UINT64_MAX ? UINT64_MAX : n;
}

const char* disk_refusal_name(DiskRefusal r) {
    switch (r) {
        case DiskRefusal::none: return "none";
        case DiskRefusal::quota: return "quota";
        case DiskRefusal::space: return "free-space";
        case DiskRefusal::index: return "index";
        case DiskRefusal::count: return "count";
        case DiskRefusal::protected_: return "protected";
        case DiskRefusal::io: return "io";
    }
    return "io";
}

DiskCache::~DiskCache() {
    if (!owner_) return;
    std::error_code ec;
    std::filesystem::remove(std::filesystem::path(opt_.dir + "/owner.lock"), ec);
}

void DiskCache::note_refusal(DiskRefusal r, DiskReservation& out, std::string& why) {
    out.refusal = r;
    stats_.refusals++;
    stats_.last_refusal = r;
    why = std::string("disk cache: ") + disk_refusal_name(r) + " refusal";
}

bool DiskCache::take_owner(std::string& why) {
    const std::filesystem::path lock = std::filesystem::path(opt_.dir + "/owner.lock");
    const uint64_t me = (uint64_t) ::getpid();
    std::error_code ec;
    if (std::filesystem::exists(lock, ec)) {
        std::string content;
        std::ifstream f(lock);
        if (f) {
            long long pid = -1;
            f >> pid;
            content = (pid > 0) ? std::to_string(pid) : "";
        }
        const uint64_t pid = content.empty() ? 0 : (uint64_t) std::strtoull(content.c_str(), nullptr, 10);
        // Windows cannot tell a live process from a dead one by pid cheaply: a lock left by THIS machine's own
        // pid is ours to reclaim, any other is refused.
#ifdef _WIN32
        const bool stale = (pid == me);
#else
        const bool stale = !process_alive(pid);
#endif
        if (!stale) {
            why = "another engine holds this disk cache (pid " + std::to_string(pid) + ")";
            return false;
        }
        std::filesystem::remove(lock, ec);   // an interrupted run's lock: take it over
    }
    const int fd = ::open(lock.string().c_str(), O_CREAT | O_EXCL | O_WRONLY | O_BINARY,
#ifdef _WIN32
                          _S_IREAD | _S_IWRITE
#else
                          0600
#endif
    );
    if (fd < 0) {
        why = "cannot create the disk cache's owner lock in " + opt_.dir + ": " + std::strerror(errno);
        return false;
    }
    const std::string pid_txt = std::to_string(me) + "\n";
    const ssize_t w = ::write(fd, pid_txt.data(), pid_txt.size());
    ::close(fd);
    if (w != (ssize_t) pid_txt.size()) {
        why = "cannot write the disk cache's owner lock in " + opt_.dir;
        return false;
    }
    owner_ = true;
    return true;
}

void DiskCache::scan(const SessionFileIdentity& id, const SessionV2Requirement& want,
                     const SessionReadV2Limits& limits) {
    std::error_code ec;
    std::vector<std::pair<uint64_t, std::string>> found;   // sidecars: id -> path
    std::vector<std::pair<uint64_t, std::string>> temps;
    for (const auto& e : std::filesystem::directory_iterator(opt_.dir, ec)) {
        if (!e.is_regular_file()) continue;
        const std::string name = e.path().filename().string();
        uint64_t sid = 0;
        if (is_ours(name, "conv-", ".sess", sid)) found.emplace_back(sid, e.path().string());
        else if (is_ours(name, "ring-", ".dring", sid)) found.emplace_back(sid, e.path().string());   // tagged below
        else if (name.rfind(".conv-", 0) == 0 && name.size() > 4 && name.compare(name.size() - 4, 4, ".tmp") == 0)
            temps.emplace_back(0, e.path().string());   // an interrupted publication's temporary
    }
    for (const auto& t : temps) {
        std::filesystem::remove(std::filesystem::path(t.second), ec);
        if (!ec) inv_.orphan_temps++;
    }
    // the generations first: a sidecar is only orphaned once no published generation references it
    std::vector<uint64_t> keep_sidecars;
    std::sort(found.begin(), found.end(), [](const std::pair<uint64_t, std::string>& a,
                                             const std::pair<uint64_t, std::string>& b) { return a.first < b.first; });
    for (const auto& f : found) {
        const std::string name = std::filesystem::path(f.second).filename().string();
        if (name.rfind("ring-", 0) == 0) {
            std::error_code sz_ec;
            const auto sz = std::filesystem::file_size(f.second, sz_ec);
            if (sz_ec) continue;
            sidecars_.emplace_back(f.first, (uint64_t) sz);
            sidecar_bytes_ += (uint64_t) sz;
            inv_.sidecars++;
            continue;
        }
        SessionManifestV2 manifest;
        size_t bytes = 0;
        std::string why;
        SessionStatus st;
        if (!session_file_manifest_v2(f.second, id, want, manifest, bytes, why, limits, &st)) {
            const bool foreign = why.find("another model") != std::string::npos ||
                                 why.find("another engine configuration") != std::string::npos ||
                                 why.find("another split layout") != std::string::npos ||
                                 why.find("another draft") != std::string::npos;
            inv_.foreign += foreign ? 1 : 0;
            inv_.corrupt += foreign ? 0 : 1;
            stats_.incompatible += foreign ? 1 : 0;
            stats_.corrupt += foreign ? 0 : 1;
            inv_.last_refusal = why;
            std::filesystem::remove(std::filesystem::path(f.second), ec);   // unreadable: never usable, never priced
            continue;
        }
        DiskGeneration g;
        g.serial = f.first;
        g.path = f.second;
        g.tokens = std::move(manifest.tokens);
        g.images = std::move(manifest.images);
        g.cvec = manifest.cvec;
        g.bytes = bytes;
        for (const auto& cm : manifest.checkpoints) {
            g.anchor = std::max<int64_t>(g.anchor, (int64_t) cm.prefix);
            if (cm.ring_id) {
                g.pair_prefix = std::max<int64_t>(g.pair_prefix, (int64_t) cm.prefix);
                g.ring_ids.push_back(cm.ring_id);
                keep_sidecars.push_back(cm.ring_id);
            }
            g.pinned = g.pinned || cm.pinned;
            g.used = std::max(g.used, cm.used);
        }
        used_ += g.bytes;
        index_bytes_ = sat_add(index_bytes_, disk_generation_index_bytes(g));
        gens_.push_back(std::move(g));
        next_serial_ = std::max(next_serial_, g.serial + 1);
        clock_ = std::max(clock_, gens_.back().used);
        inv_.generations++;
        inv_.bytes += bytes;
    }
    std::sort(gens_.begin(), gens_.end(),
              [](const DiskGeneration& a, const DiskGeneration& b) { return a.used < b.used; });
    // a sidecar no published generation references cannot be read back by anyone: it is this namespace's own trash
    for (auto it = sidecars_.begin(); it != sidecars_.end();) {
        const bool referenced = std::find(keep_sidecars.begin(), keep_sidecars.end(), it->first) != keep_sidecars.end();
        if (referenced) {
            inv_.sidecar_bytes += it->second;
            used_ += it->second;   // a capture the index can read back is disk the quota must price, as note_sidecar books it
            ++it;
            continue;
        }
        std::filesystem::remove(std::filesystem::path(sidecar_path(it->first)), ec);
        if (ec) { ++it; continue; }
        sidecar_bytes_ -= it->second;
        it = sidecars_.erase(it);
        inv_.orphan_sidecars++;
    }
    stats_.index_high = std::max(stats_.index_high, index_bytes_);
}

bool DiskCache::open(const SessionFileIdentity& id, const SessionV2Requirement& want,
                     const SessionReadV2Limits& limits, std::string& why) {
    if (opt_.dir.empty()) { why = "the disk cache has no directory"; return false; }
    std::error_code ec;
    if (!std::filesystem::is_directory(opt_.dir, ec)) {
        std::filesystem::create_directories(opt_.dir, ec);
        if (ec) {
            why = "the disk cache cannot create " + opt_.dir + ": " + ec.message();
            return false;
        }
    }
#ifndef _WIN32
    ::chmod(opt_.dir.c_str(), 0700);   // the generations are the engine's own
#endif
    if (!take_owner(why)) return false;
    scan(id, want, limits);
    return true;
}

const DiskGeneration* DiskCache::find(uint64_t serial) const {
    for (const auto& g : gens_)
        if (g.serial == serial) return &g;
    return nullptr;
}

void DiskCache::guard(uint64_t serial) { if (auto* g = const_cast<DiskGeneration*>(find(serial))) g->guarded = true; }
void DiskCache::unguard(uint64_t serial) {
    if (auto* g = const_cast<DiskGeneration*>(find(serial))) g->guarded = false;
}
void DiskCache::reference(uint64_t serial) {
    if (auto* g = const_cast<DiskGeneration*>(find(serial))) g->referenced = true;
}
void DiskCache::unreference(uint64_t serial) {
    if (auto* g = const_cast<DiskGeneration*>(find(serial))) g->referenced = false;
}

void DiskCache::use(uint64_t serial) {
    auto* g = const_cast<DiskGeneration*>(find(serial));
    if (g == nullptr) return;
    g->used = ++clock_;
    std::sort(gens_.begin(), gens_.end(), [](const DiskGeneration& a, const DiskGeneration& b) { return a.used < b.used; });
}

int64_t DiskCache::parked_prefix_tokens(const std::vector<int32_t>& ids,
                                        const std::vector<ConversationImageKey>& images, bool cvec) const {
    int64_t longest = 0;
    for (const auto& g : gens_) {
        if (g.cvec != cvec) continue;
        if (g.tokens == ids && g.images == images) longest = std::max(longest, (int64_t) g.tokens.size());
    }
    return longest;
}

bool DiskCache::erase(size_t i, std::string& why) {
    std::error_code ec;
    if (opt_.fault) {
        const int e = opt_.fault("remove");
        if (e) {
            why = std::string("disk cache: removing ") + gens_[i].path + " failed: " + std::strerror(e);
            return false;
        }
    }
    std::filesystem::remove(std::filesystem::path(gens_[i].path), ec);
    if (ec) {
        why = "disk cache: removing " + gens_[i].path + " failed: " + ec.message();
        return false;
    }
    used_ -= gens_[i].bytes;
    index_bytes_ -= disk_generation_index_bytes(gens_[i]);
    gens_.erase(gens_.begin() + (std::ptrdiff_t) i);
    stats_.evictions++;
    return true;
}

namespace {
size_t pinned_count(const std::vector<DiskGeneration>& gens) {
    size_t n = 0;
    for (const auto& g : gens)
        if (g.pinned) ++n;
    return n;
}
// the oldest entry eviction may take (gens_ is kept in use-stamp order): never a guarded one (the incoming
// donor), never one whose file is open, never the ONLY pinned root - and a pinned entry is not the victim
// while it is the only pinned one, so the unpinned behind it go first
size_t oldest_eligible(const std::vector<DiskGeneration>& gens) {
    const size_t pinned = pinned_count(gens);
    for (size_t i = 0; i < gens.size(); ++i) {
        if (gens[i].guarded || gens[i].referenced) continue;
        if (gens[i].pinned && pinned <= 1) continue;
        return i;
    }
    return SIZE_MAX;
}
}   // namespace

bool DiskCache::reserve(uint64_t file_bytes, uint64_t sidecar_bytes, uint64_t index_bytes, DiskReservation& out,
                        std::string& why) {
    out = {};
    // the complete planned write, priced before anything is opened: the generation's exact file bytes, the pair
    // bodies it packs, and the atomic replacement's own temporary (the writer's temp is the same size as the file)
    const uint64_t need = sat_add(sat_add(file_bytes, sidecar_bytes), file_bytes);
    if (need == UINT64_MAX) { note_refusal(DiskRefusal::io, out, why); return false; }
    out.file_bytes = file_bytes;
    out.sidecar_bytes = sidecar_bytes;
    out.index_bytes = index_bytes;
    for (size_t pass = 0; pass < kReserveGuard + gens_.size() + 1; ++pass) {
        const uint64_t on_disk = sat_add(used_, reserved_);
        const bool over_disk = sat_add(on_disk, need) > opt_.budget;
        const bool over_count = opt_.max_entries > 0 && gens_.size() + 1 > opt_.max_entries;
        const bool over_index = sat_add(index_bytes_, index_bytes) > opt_.index_cap;
        if (!over_disk && !over_count && !over_index) {
            // the volume's own free space, with the floor the manual saves use
            uint64_t avail = 0;
            const bool known = opt_.free_space ? opt_.free_space(avail) : session_disk_free_space(opt_.dir, avail);
            if (!known) { note_refusal(DiskRefusal::io, out, why); return false; }
            if (sat_add(need, opt_.min_free_bytes) > avail) {
                why = std::string("disk cache: free-space refusal (") + std::to_string(need >> 20) +
                      " MiB plus a floor of " + std::to_string(opt_.min_free_bytes >> 20) + " MiB needed, " +
                      std::to_string(avail >> 20) + " MiB free)";
                out.refusal = DiskRefusal::space;
                stats_.refusals++;
                stats_.last_refusal = DiskRefusal::space;
                return false;
            }
            out.held = true;
            out.serial = next_serial_++;
            out.path = generation_path(out.serial);
            reserved_ = sat_add(reserved_, need);
            return true;
        }
        // evict the oldest eligible generation (the protections above say who that is)
        const size_t i = oldest_eligible(gens_);
        if (i == SIZE_MAX) {
            if (over_index) { note_refusal(DiskRefusal::index, out, why); return false; }
            if (over_count) { note_refusal(DiskRefusal::count, out, why); return false; }
            // an empty cache has nothing to protect: the budget itself is the reason there.  With entries
            // indexed, the reason the write cannot fit is that every one of them is protected.
            if (over_disk && gens_.empty()) { note_refusal(DiskRefusal::quota, out, why); return false; }
            note_refusal(DiskRefusal::protected_, out, why);
            return false;
        }
        std::string ewhy;
        if (!erase(i, ewhy)) {
            why = ewhy;
            out.refusal = DiskRefusal::io;
            stats_.refusals++;
            stats_.last_refusal = DiskRefusal::io;
            return false;
        }
        out.evicted++;
    }
    note_refusal(DiskRefusal::protected_, out, why);   // unreachable: one eviction per entry
    return false;
}

void DiskCache::release(DiskReservation& res) {
    if (!res.held) return;
    res.held = false;
    reserved_ -= res.file_bytes + res.sidecar_bytes + res.file_bytes;
}

bool DiskCache::publish(DiskReservation& res, DiskGeneration meta, bool dir_flush_pending, std::string& why) {
    if (!res.held) { why = "disk cache: the reservation is not held"; return false; }
    std::error_code ec;
    if (!std::filesystem::is_regular_file(std::filesystem::path(res.path), ec)) {
        why = "disk cache: " + res.path + " is not a published file";
        stats_.refusals++;
        stats_.last_refusal = DiskRefusal::io;
        return false;
    }
    meta.serial = res.serial;
    meta.path = res.path;
    meta.dir_flush_pending = dir_flush_pending;
    meta.used = ++clock_;
    const uint64_t ib = disk_generation_index_bytes(meta);
    res.held = false;
    reserved_ -= res.file_bytes + res.sidecar_bytes + res.file_bytes;
    used_ = sat_add(used_, res.file_bytes + res.sidecar_bytes);
    index_bytes_ = sat_add(index_bytes_, ib);
    stats_.index_high = std::max(stats_.index_high, index_bytes_);
    stats_.parks++;
    stats_.last_serial = meta.serial;
    gens_.push_back(std::move(meta));
    return true;
}

size_t DiskCache::retire_superseded(const std::vector<int32_t>& ids, const std::vector<ConversationImageKey>& images,
                                    bool cvec, uint64_t published) {
    size_t dropped = 0;
    for (size_t i = 0; i < gens_.size();) {
        const DiskGeneration& g = gens_[i];
        const int64_t L = (int64_t) g.tokens.size();
        if (g.serial == published) { ++i; continue; }   // the generation that was just published is never its own victim
        const bool pre = L <= (int64_t) ids.size() && std::equal(g.tokens.begin(), g.tokens.end(), ids.begin());
        std::vector<ConversationImageKey> under;
        disk_images_below(images, L, under);
        if (pre && under == g.images && g.cvec == cvec && !g.guarded && !g.referenced) {
            std::string why;
            if (erase(i, why)) {
                ++dropped;
                stats_.superseded++;
                continue;
            }
            stats_.refusals++;
            stats_.last_refusal = DiskRefusal::io;
        }
        ++i;
    }
    return dropped;
}

void DiskCache::note_sidecar(uint64_t id, uint64_t bytes) {
    sidecars_.emplace_back(id, bytes);
    sidecar_bytes_ += bytes;
    used_ += bytes;
}

bool DiskCache::sidecar_bytes(uint64_t id, uint64_t& bytes) const {
    for (const auto& s : sidecars_)
        if (s.first == id) {
            bytes = s.second;
            return true;
        }
    return false;
}

bool DiskCache::can_capture_sidecar(uint64_t bytes, std::string& why) const {
    if (sat_add(sat_add(used_, reserved_), bytes) > opt_.budget) {
        why = "the disk tier has no room for a " + std::to_string(bytes >> 20) + " MiB pair capture (" +
              std::to_string(opt_.budget >> 20) + " MiB budget, " + std::to_string((used_ + reserved_) >> 20) +
              " MiB held)";
        return false;
    }
    return true;
}

size_t DiskCache::reclaim_sidecars(const std::function<bool(uint64_t id)>& referenced, uint64_t& freed) {
    freed = 0;
    size_t n = 0;
    for (auto it = sidecars_.begin(); it != sidecars_.end();) {
        // a published generation carries this sidecar's id in its checkpoint metas: a rewind to that
        // checkpoint reads it back, so the file stays with the generation that advertises it
        bool live = referenced(it->first);
        for (const auto& g : gens_) {
            if (live) break;
            for (uint64_t rid : g.ring_ids)
                if (rid == it->first) { live = true; break; }
        }
        if (live) { ++it; continue; }
        std::error_code ec;
        if (opt_.fault && opt_.fault("remove_sidecar")) { ++it; continue; }
        std::filesystem::remove(std::filesystem::path(sidecar_path(it->first)), ec);
        if (ec) { ++it; continue; }
        sidecar_bytes_ -= it->second;
        used_ -= it->second;
        freed += it->second;
        it = sidecars_.erase(it);
        ++n;
    }
    return n;
}

} // namespace strata::core

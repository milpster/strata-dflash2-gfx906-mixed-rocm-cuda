// include/strata/core/arena_pin_ranges.hpp - pure host arithmetic over the arena's sorted
// pin-range tables (pin_start_/pin_end_ in ArenaExpertSource).  helper-ipc-diet round 8.
//
// The tables' invariants (register_complement/register_after_fill build them at boot,
// release_pair/refill_pair maintain them at runtime):
//   - strictly increasing pin_start_, matching pin_end_, no two entries overlap or touch
//     (contiguous spans are one entry),
//   - a pair's blob is DMA-able from the arena only when it lies WHOLLY INSIDE ONE entry
//     (pin_range_of's rule): the runtime's direct copy path needs the whole source span
//     inside a single registered region, and a copy that crosses a registration boundary
//     dies with "invalid argument" (the V4-r3 and round-7 lessons).
//
// Round-8's promote (refill_pair) uses these helpers to register exactly the UNREGISTERED
// pages of the pair's outward-rounded span and then coalesce the touched neighborhood back
// into ONE region, so the runtime never sees an overlapping cudaHostRegister from the
// re-rank (the churn class behind the round-7 rerank-arm fatal) and every resident pair
// keeps its one-region guarantee.
#ifndef STRATA_CORE_ARENA_PIN_RANGES_H
#define STRATA_CORE_ARENA_PIN_RANGES_H

#include <algorithm>
#include <cstdint>
#include <utility>
#include <vector>

namespace strata::core {

/// The UNREGISTERED subranges of [lo, hi) against the sorted pin tables.  Returns them
/// sorted, clipped to [lo, hi); an empty result means the span is already covered.
inline std::vector<std::pair<uint64_t, uint64_t>> pin_gaps(uint64_t lo, uint64_t hi,
                                                           const std::vector<uint64_t>& pin_start,
                                                           const std::vector<uint64_t>& pin_end) {
    std::vector<std::pair<uint64_t, uint64_t>> gaps;
    if (hi <= lo || pin_start.empty()) {
        if (hi > lo) gaps.emplace_back(lo, hi);
        return gaps;
    }
    // walk the entries that intersect [lo, hi), emitting the space between them
    uint64_t cur = lo;
    const size_t n = pin_start.size();
    size_t i = (size_t) (std::upper_bound(pin_start.begin(), pin_start.end(), lo) - pin_start.begin());
    if (i > 0) i -= 1;   // the last entry starting at or before lo may reach into the span
    for (; i < n && cur < hi; ++i) {
        if (pin_end[i] <= cur) continue;   // an entry entirely before the span (the i-- case)
        if (pin_start[i] >= hi) break;     // entirely after
        if (pin_start[i] > cur) gaps.emplace_back(cur, pin_start[i]);
        cur = std::max(cur, pin_end[i]);
    }
    if (cur < hi) gaps.emplace_back(cur, hi);
    return gaps;
}

/// The span re-registering [lo, hi) must restore as ONE region so no pair inside it ends up
/// straddling a registration boundary: every entry that overlaps [lo, hi) is unregistered and
/// re-registered together with the span's gaps.  Returns the union (lo, hi) when no entry
/// overlaps (a fresh registration, exactly as before).  `first`/`last` report the overlapping
/// entries' index range as [first, last); last < first means none.
inline std::pair<uint64_t, uint64_t> pin_union(uint64_t lo, uint64_t hi,
                                               const std::vector<uint64_t>& pin_start,
                                               const std::vector<uint64_t>& pin_end, size_t& first,
                                               size_t& last) {
    first = 0;
    last = (size_t) -1;
    if (hi <= lo || pin_start.empty()) return {lo, hi};
    const size_t n = pin_start.size();
    size_t i = (size_t) (std::upper_bound(pin_start.begin(), pin_start.end(), lo) - pin_start.begin());
    if (i > 0) i -= 1;
    while (i < n && pin_end[i] <= lo) ++i;   // skip entries ending at or before the span
    if (i >= n || pin_start[i] >= hi) return {lo, hi};   // no overlap: a fresh region
    first = i;
    last = i;
    uint64_t u_lo = std::min(lo, pin_start[i]);
    uint64_t u_hi = std::max(hi, pin_end[i]);
    while (last + 1 < n && pin_start[last + 1] < hi) {
        ++last;
        u_lo = std::min(u_lo, pin_start[last]);
        u_hi = std::max(u_hi, pin_end[last]);
    }
    return {u_lo, u_hi};
}

}   // namespace strata::core

#endif   // STRATA_CORE_ARENA_PIN_RANGES_H

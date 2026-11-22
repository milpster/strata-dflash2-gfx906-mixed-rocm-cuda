// tests/core/arena_pin_ranges_test.cpp - CPU checks for the pin-range arithmetic the round-8
// promote uses (include/strata/core/arena_pin_ranges.hpp): the unregistered gaps of a span
// against the sorted pin tables, and the union a coalescing re-registration must cover.
// Pure host arithmetic, no device, no files.
#include "strata/core/arena_pin_ranges.hpp"

#include <cstdint>
#include <cstdio>
#include <vector>

using namespace strata::core;

static int failures = 0;
static void check(bool ok, const char* what) {
    if (!ok) {
        std::printf("FAIL: %s\n", what);
        ++failures;
    }
}

namespace {
constexpr uint64_t K = 4096;

using Tables = std::pair<std::vector<uint64_t>, std::vector<uint64_t>>;

/// sorted, non-touching tables as the arena's invariants require
Tables tables(std::vector<std::pair<uint64_t, uint64_t>> rs) {
    Tables t;
    for (const auto& r : rs) {
        t.first.push_back(r.first);
        t.second.push_back(r.first + r.second);
    }
    return t;
}

bool same(const std::vector<std::pair<uint64_t, uint64_t>>& got,
          const std::vector<std::pair<uint64_t, uint64_t>>& want) {
    return got == want;
}
}   // namespace

int main() {
    // 1. No tables: the whole span is one gap; the union is the span itself (a fresh region).
    {
        const auto g = pin_gaps(100 * K, 110 * K, {}, {});
        check(same(g, {{100 * K, 110 * K}}), "empty tables: the whole span is one gap");
        size_t f = 99, l = 0;
        const auto u = pin_union(100 * K, 110 * K, {}, {}, f, l);
        check(u == std::pair<uint64_t, uint64_t>(100 * K, 110 * K) && l == (size_t) -1,
              "empty tables: the union is the span, no entries touched");
    }

    // 2. Fully covered: no gaps; the union covers the overlapping entries.
    {
        const auto t = tables({{90 * K, 30 * K}});
        const auto g = pin_gaps(100 * K, 110 * K, t.first, t.second);
        check(g.empty(), "a span inside one entry has no gaps");
        size_t f = 99, l = 0;
        const auto u = pin_union(100 * K, 110 * K, t.first, t.second, f, l);
        check(u == std::pair<uint64_t, uint64_t>(90 * K, 120 * K) && f == 0 && l == 0,
              "the union grows to the covering entry's full extent");
    }

    // 3. The round-8 promote case: a demoted pair's interior is the only gap, its edge pages
    //    live in the split remainders, and the union restores the pre-demote region whole.
    //    Boot range [90K, 200K); the demote split it into [90K, 120K) + [150K, 200K) and gave
    //    [120K, 150K) back; the re-promote spans [119K, 151K) (outward-rounded).
    {
        const auto t = tables({{90 * K, 30 * K}, {150 * K, 50 * K}});
        const auto g = pin_gaps(119 * K, 151 * K, t.first, t.second);
        check(same(g, {{120 * K, 150 * K}}), "only the demoted interior is a gap: the edge pages stayed registered");
        size_t f = 99, l = 0;
        const auto u = pin_union(119 * K, 151 * K, t.first, t.second, f, l);
        check(u == std::pair<uint64_t, uint64_t>(90 * K, 200 * K) && f == 0 && l == 1,
              "the union is the pre-demote region whole: both remainders coalesce");
    }

    // 4. A span before every entry, after every entry, and between two entries: the union does
    //    not touch what the span does not overlap.
    {
        const auto t = tables({{100 * K, 10 * K}, {200 * K, 10 * K}});
        check(pin_gaps(80 * K, 90 * K, t.first, t.second).size() == 1, "before the tables: one gap");
        size_t f = 99, l = 0;
        check(pin_union(80 * K, 90 * K, t.first, t.second, f, l) == std::pair<uint64_t, uint64_t>(80 * K, 90 * K) &&
                  l == (size_t) -1,
              "before the tables: a fresh region, nothing coalesced");
        f = 99, l = 0;
        check(pin_union(300 * K, 310 * K, t.first, t.second, f, l) == std::pair<uint64_t, uint64_t>(300 * K, 310 * K),
              "after the tables: a fresh region");
        const auto g = pin_gaps(120 * K, 190 * K, t.first, t.second);
        check(same(g, {{120 * K, 190 * K}}), "between two entries: the whole span is the gap, clipped to it");
        f = 99, l = 0;
        const auto u = pin_union(120 * K, 190 * K, t.first, t.second, f, l);
        check(u == std::pair<uint64_t, uint64_t>(120 * K, 190 * K) && l == (size_t) -1,
              "a span in a hole overlaps nothing: a fresh region, the neighbours stay as they are");
    }

    // 5. An entry STARTING inside the span splits the gaps around it.
    {
        const auto t = tables({{0, 10 * K}, {104 * K, 4 * K}, {150 * K, 10 * K}});
        const auto g = pin_gaps(100 * K, 140 * K, t.first, t.second);
        check(same(g, {{100 * K, 104 * K}, {108 * K, 140 * K}}),
              "an entry inside the span splits the gap in two");
        size_t f = 99, l = 0;
        const auto u = pin_union(100 * K, 140 * K, t.first, t.second, f, l);
        check(u == std::pair<uint64_t, uint64_t>(100 * K, 140 * K) && f == 1 && l == 1,
              "the union only swallows the overlapping entry (its start is inside)");
    }

    // 6. Abutting entries (the invariant forbids them, but the arithmetic must not wedge): a
    //    gap walk over touching entries treats them as contiguous cover.
    {
        const auto t = tables({{100 * K, 20 * K}, {120 * K, 20 * K}});
        check(pin_gaps(105 * K, 125 * K, t.first, t.second).empty(), "abutting entries cover the span");
        size_t f = 99, l = 0;
        const auto u = pin_union(105 * K, 125 * K, t.first, t.second, f, l);
        check(u == std::pair<uint64_t, uint64_t>(100 * K, 140 * K) && f == 0 && l == 1,
              "the union swallows both abutting entries");
    }

    // 7. Degenerate span: empty, nothing to do.
    {
        const auto t = tables({{100 * K, 10 * K}});
        check(pin_gaps(100 * K, 100 * K, t.first, t.second).empty(), "an empty span has no gaps");
        size_t f = 99, l = 0;
        check(pin_union(100 * K, 100 * K, t.first, t.second, f, l).first >= pin_union(100 * K, 100 * K, t.first, t.second, f, l).second,
              "an empty span reports an empty union");
    }

    // 8. The exact prod neighbourhood: a boot range spanning several 1.9 MiB-scale pairs, one
    //    pair demoted, then re-promoted.  The promote must register exactly the dropped
    //    interior and coalesce the range back to its boot shape.
    {
        const uint64_t pair = 1992192;   // the measured IQ3_S pair bytes
        const uint64_t boot_lo = 0, boot_hi = 7 * pair;
        const uint64_t x_off = 2 * pair;                          // pair X inside the boot range
        const uint64_t lo_d = (x_off + K - 1) & ~(K - 1);         // the demote's wholly-interior rule
        const uint64_t hi_d = (x_off + pair) & ~(K - 1);
        const auto t = tables({{boot_lo, lo_d - boot_lo}, {hi_d, boot_hi - hi_d}});   // the split remainders
        const uint64_t lo_p = x_off & ~(K - 1);                   // the promote's outward rounding
        const uint64_t hi_p = (x_off + pair + K - 1) & ~(K - 1);
        const auto g = pin_gaps(lo_p, hi_p, t.first, t.second);
        check(g.size() == 1 && g[0].first == lo_d && g[0].second == hi_d,
              "the prod shape: the promote's only gap is the demoted interior, page-exact");
        size_t f = 99, l = 0;
        const auto u = pin_union(lo_p, hi_p, t.first, t.second, f, l);
        check(u == std::pair<uint64_t, uint64_t>(boot_lo, boot_hi),
              "the prod shape: the union restores the boot range whole");
    }

    if (failures) {
        std::printf("arena_pin_ranges_test: %d FAILURES\n", failures);
        return 1;
    }
    std::printf("arena_pin_ranges_test: all checks passed\n");
    return 0;
}

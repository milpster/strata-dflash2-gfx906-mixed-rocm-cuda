// tests/core/dflash_tap_plan_test.cpp - CPU proof of the production tap-ownership model
// (include/strata/core/dflash_tap_plan.hpp) used by the cross-GPU collector and the CLI
// gate (docs/DFLASH2.md capture contract: taps [5,15,25,35,45] captured at boundary
// tap+1).  No GPU headers, no link - the same build() the engine runs at drafter upload.
#include <strata/core/dflash_tap_plan.hpp>

#include <cstdio>
#include <string>

using strata::core::DFlashTapPlan;

namespace {

int failures = 0;

void check(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) ++failures;
}

// split32 on the 48-layer target: GPU0 layers [0,32), GPU1 [32,48); the drafter on GPU1.
// Expected owners: taps 0..2 (boundaries 6,16,26) -> stage 0, taps 3..4 (36,46) -> stage 1.
bool build_split32(DFlashTapPlan& p, std::string& err) {
    const int bounds[5] = {6, 16, 26, 36, 46};
    const int lbs[2] = {0, 32}, les[2] = {32, 48}, devs[2] = {0, 1};
    return p.build(bounds, 5, lbs, les, devs, 2, 2560, err);
}

}  // namespace

int main() {
    std::string err;

    // 1. split32: owners, foreign set (drafter on GPU1), payload and plane bytes.
    DFlashTapPlan p;
    check(build_split32(p, err), "split32 plan builds");
    check(p.owner[0] == 0 && p.owner[1] == 0 && p.owner[2] == 0 && p.owner[3] == 1 && p.owner[4] == 1,
          "split32 owners: GPU0 captures boundaries 6,16,26; GPU1 captures 36,46");
    std::array<int, strata::core::kDFlashMaxTaps> foreign{};
    check(p.foreign_taps(1, foreign) == 3 && foreign[0] == 0 && foreign[1] == 1 && foreign[2] == 2,
          "drafter on GPU1: exactly the three GPU0 taps are foreign, in tap order");
    check(p.foreign_taps(0, foreign) == 2 && foreign[0] == 3 && foreign[1] == 4,
          "a drafter on GPU0 would instead transport the two GPU1 taps");
    check(p.tap_device(0) == 0 && p.tap_device(3) == 1, "tap producers map to their stage devices");
    // Per-window verify rows (FP32): stage 0 ships 3 taps x rows x 2560 x 4 B.
    for (int rows : {1, 2, 4, 8})
        check(p.stage_payload_bytes(0, rows, 4) == (int64_t) 3 * rows * 2560 * 4 &&
              p.stage_payload_bytes(1, rows, 4) == (int64_t) 2 * rows * 2560 * 4,
              "compact verify payload = owned taps x valid rows only (rows 1,2,4,8)");
    check(p.stage_payload_bytes(0, 8192, 2) == (int64_t) 3 * 8192 * 2560 * 2,
          "compact prefill payload = 3 taps x 8192 rows BF16 (never the padded stride)");
    check(p.plane_bytes(8, 4) == (int64_t) 5 * 8 * 2560 * 4 && p.plane_bytes(8192, 2) == (int64_t) 5 * 8192 * 2560 * 2,
          "assembled plane = every tap x row cap");

    // 2. Splits crossing each tap: ownership follows the boundary, not the tap id.
    struct Crossing { int at; int expect[5]; };
    const Crossing crossings[] = {{7, {0, 1, 1, 1, 1}},  {16, {0, 1, 1, 1, 1}}, {17, {0, 0, 1, 1, 1}},
                                  {26, {0, 0, 1, 1, 1}}, {27, {0, 0, 0, 1, 1}}, {36, {0, 0, 0, 1, 1}},
                                  {37, {0, 0, 0, 0, 1}}, {46, {0, 0, 0, 0, 1}}};
    for (const auto& c : crossings) {
        DFlashTapPlan q;
        const int bounds[5] = {6, 16, 26, 36, 46};
        const int lbs[2] = {0, c.at}, les[2] = {c.at, 48}, devs[2] = {0, 1};
        const bool built = q.build(bounds, 5, lbs, les, devs, 2, 2560, err);
        bool owners_ok = built;
        for (int t = 0; t < 5 && owners_ok; ++t) owners_ok = q.owner[t] == c.expect[t];
        check(owners_ok, "split crossing a boundary: the tap moves with its boundary layer");
    }

    // 3. Single producer stage (the CLI --layer-split shape: full layer loop, streamed experts).
    {
        DFlashTapPlan q;
        const int bounds[5] = {6, 16, 26, 36, 46};
        const int lbs[1] = {0}, les[1] = {48}, devs[1] = {0};
        std::array<int, strata::core::kDFlashMaxTaps> fq{};
        check(q.build(bounds, 5, lbs, les, devs, 1, 2560, err) && q.owner[0] == 0 && q.owner[4] == 0 &&
                  q.foreign_taps(1, fq) == 5 && q.foreign_taps(0, fq) == 0,
              "CLI single full-layer producer: all five taps, foreign only to a drafter on GPU1");
    }

    // 4. Refusals: an unowned boundary (a gap), duplicate capture (overlap), empty range, caps.
    {
        DFlashTapPlan q;
        const int bounds[5] = {6, 16, 26, 36, 46};
        const int lbs[2] = {0, 30}, les[2] = {20, 48}, devs[2] = {0, 1};   // disjoint ranges with a gap
        check(!q.build(bounds, 5, lbs, les, devs, 2, 2560, err) && err.find("NO stage") != std::string::npos,
              "a boundary in a gap between stages is refused (stale slice would be read)");
    }
    {
        DFlashTapPlan q;
        const int bounds[5] = {6, 16, 26, 36, 46};
        const int lbs[2] = {0, 16}, les[2] = {32, 48}, devs[2] = {0, 1};   // stages 0 and 1 overlap
        check(!q.build(bounds, 5, lbs, les, devs, 2, 2560, err) && err.find("overlaps") != std::string::npos,
              "overlapping stage ranges are refused (duplicate capture)");
    }
    {
        DFlashTapPlan q;
        const int bounds[5] = {6, 16, 26, 36, 46};
        const int lbs[2] = {0, 32}, les[2] = {32, 32}, devs[2] = {0, 1};   // the second stage is empty
        check(!q.build(bounds, 5, lbs, les, devs, 2, 2560, err) && err.find("empty layer range") != std::string::npos,
              "an empty stage range is refused");
    }
    {
        DFlashTapPlan q;
        int bounds[9];
        for (int t = 0; t < 9; ++t) bounds[t] = 2 + 2 * t;
        const int lbs[1] = {0}, les[1] = {48}, devs[1] = {0};
        check(!q.build(bounds, 9, lbs, les, devs, 1, 2560, err) && err.find("outside") != std::string::npos,
              "a tap count beyond the plane cap is refused");
    }
    {
        DFlashTapPlan q;
        const int bounds[5] = {6, 16, 26, 36, 46};
        const int lbs[1] = {0}, les[1] = {48}, devs[1] = {0};
        check(!q.build(bounds, 5, lbs, les, devs, 1, 0, err), "a non-positive hidden width is refused");
    }

    // 5. Tap order is capture order: owners follow the list, never sorted by boundary value.
    {
        DFlashTapPlan q;
        const int bounds[5] = {26, 6, 46, 16, 36};
        const int lbs[2] = {0, 32}, les[2] = {32, 48}, devs[2] = {0, 1};
        check(q.build(bounds, 5, lbs, les, devs, 2, 2560, err) &&
                  q.owner[0] == 0 && q.owner[1] == 0 && q.owner[2] == 1 && q.owner[3] == 0 && q.owner[4] == 1,
              "owners map per tap in the artifact's order (the assembly reorders nothing)");
    }

    std::printf(failures == 0 ? "dflash_tap_plan_test: ALL PASS\n" : "dflash_tap_plan_test: %d FAILURES\n",
                failures == 0 ? 0 : failures);
    return failures == 0 ? 0 : 1;
}

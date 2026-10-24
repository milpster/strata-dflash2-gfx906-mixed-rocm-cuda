// include/strata/core/dflash_tap_plan.hpp - the ONE CPU model for DFlash2 target-tap
// ownership under --layer-split (docs/DFLASH2.md capture contract: completed layer
// outputs [5,15,25,35,45] captured at the NEXT-layer boundary, i.e. boundary id
// tap+1; averaged over the four raw HC streams when target_hc_mean).
//
// A stage owns the layer loop [lb, le); its window/prefill graph captures the tap whose
// boundary layer sits in that range, writing rows [0, T) of plane row-slice `tap` with
// plane stride (rows per tap).  The drafter lives on the FINAL stage's device; every
// other stage's tap slice must be transported there.  This header is the planning and
// validation core (pure host arithmetic - runtime collectors AND tests drive IT).
#pragma once

#include <array>
#include <cstdint>
#include <string>

namespace strata::core {

/// Max taps (the verifier/prefill planes' cap) and max stages (--layer-split bounds).
inline constexpr int kDFlashMaxTaps = 8;
inline constexpr int kDFlashMaxStages = 8;

/// A validated tap->stage ownership plan for one drafter geometry.
struct DFlashTapPlan {
    int64_t n_embd = 0;                        ///< the draft reads the target's hidden width
    int n_taps = 0;                            ///< boundary ids below (tap order: capture order)
    std::array<int, kDFlashMaxTaps> boundary{};///< the layer id the capturing graph copies AT (tap+1)
    int n_stages = 0;
    std::array<int, kDFlashMaxStages> lb{}, le{};   ///< each stage's layer range [lb, le)
    std::array<int, kDFlashMaxStages> dev{};        ///< the CUDA device each stage's graphs run on
    std::array<int, kDFlashMaxTaps> owner{};        ///< tap index -> stage index (built by build())

    /// Derive ownership from the ACTUAL stage ranges and boundary ids. Fails (and fills
    /// `err`) on: a boundary owned by NO stage (unowned taps are never treated as valid),
    /// a boundary owned by MULTIPLE stages (ranges overlap), an empty stage range,
    /// overlapping/out-of-order stage ranges, tap count beyond the plane cap, stage count
    /// beyond the cap, or non-positive n_embd. Serial-only semantics: this plan describes
    /// the ordered per-window collection; overlapped window loops are the caller's to refuse.
    bool build(const int* boundaries, int n_taps_in, const int* lbs, const int* les,
               const int* devs, int n_stages_in, int64_t n_embd_in, std::string& err) {
        n_taps = 0; owner.fill(-1);
        if (n_embd_in <= 0) { err = "dflash tap plan: non-positive hidden width"; return false; }
        n_embd = n_embd_in;
        if (n_taps_in <= 0 || n_taps_in > kDFlashMaxTaps) {
            err = "dflash tap plan: tap count " + std::to_string(n_taps_in) + " outside 1.." +
                  std::to_string(kDFlashMaxTaps);
            return false;
        }
        if (n_stages_in <= 0 || n_stages_in > kDFlashMaxStages) {
            err = "dflash tap plan: stage count " + std::to_string(n_stages_in) + " outside 1.." +
                  std::to_string(kDFlashMaxStages);
            return false;
        }
        n_taps = n_taps_in; n_stages = n_stages_in;
        for (int i = 0; i < n_taps; ++i) boundary[i] = boundaries[i];
        for (int k = 0; k < n_stages; ++k) { lb[k] = lbs[k]; le[k] = les[k]; dev[k] = devs[k]; }
        for (int k = 0; k < n_stages; ++k) {
            if (lb[k] >= le[k]) {
                err = "dflash tap plan: stage " + std::to_string(k) + " has an empty layer range [" +
                      std::to_string(lb[k]) + ", " + std::to_string(le[k]) + ")";
                return false;
            }
            if (k > 0 && lb[k] <= le[k - 1] - 1) {
                err = "dflash tap plan: stage " + std::to_string(k) + " overlaps stage " +
                      std::to_string(k - 1) + " (ranges must be ordered and disjoint)";
                return false;
            }
        }
        for (int t = 0; t < n_taps; ++t) {
            int owners = 0, who = -1;
            for (int k = 0; k < n_stages; ++k)
                if (boundary[t] >= lb[k] && boundary[t] < le[k]) { ++owners; who = k; }
            if (owners == 0) {
                err = "dflash tap plan: boundary " + std::to_string(boundary[t]) +
                      " (tap " + std::to_string(t) + ") is captured by NO stage - the plane slice "
                      "would be stale; refuse";
                return false;
            }
            if (owners > 1) {
                err = "dflash tap plan: boundary " + std::to_string(boundary[t]) +
                      " (tap " + std::to_string(t) + ") is captured by " + std::to_string(owners) +
                      " stages - duplicate capture; refuse";
                return false;
            }
            owner[t] = who;
        }
        // The drafter's plane assembly reorders NOTHING: tap order = capture order = artifact order.
        return true;
    }

    /// Taps whose producer stage runs on `drafter_dev` stay on-device (D2D); the rest are
    /// transported through pinned host staging. Returns the tap indices (in tap order)
    /// whose owner stage lives on a different device.
    int foreign_taps(int drafter_dev, std::array<int, kDFlashMaxTaps>& out) const {
        int n = 0;
        for (int t = 0; t < n_taps; ++t) if (dev[owner[t]] != drafter_dev) out[n++] = t;
        return n;
    }

    /// The device that produces tap slice `t`'s rows.
    int tap_device(int t) const { return dev[owner[t]]; }

    /// Compact transport payload bytes for `rows` valid rows of the foreign taps of one
    /// producer stage in `type_bytes` (2 = prefill BF16, 4 = verify FP32): ONLY the valid
    /// rows of owned slices - never the full stride, never the whole history.
    int64_t stage_payload_bytes(int stage, int rows, int type_bytes) const {
        int64_t cells = 0;
        for (int t = 0; t < n_taps; ++t) if (owner[t] == stage) cells += (int64_t) rows * n_embd;
        return cells * type_bytes;
    }

    /// The assembled plane's footprint on the drafter device: every tap x `rows_cap` rows.
    int64_t plane_bytes(int64_t rows_cap, int type_bytes) const {
        return (int64_t) n_taps * rows_cap * n_embd * type_bytes;
    }
};

}  // namespace strata::core

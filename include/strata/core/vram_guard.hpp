// include/strata/core/vram_guard.hpp - the per-GPU VRAM guard (journal 2026-11-13/14, rounds 18m/18o).
//
// What happened on prod (a 27B MoE split over 2x Radeon VII, HIP): at long contexts one card's
// free VRAM sank to tens of MiB; the request-start check warned ("only 35 MiB of VRAM free", the
// #2065 class), the driver paged GPU memory, and a GPU wait (the verification at layer 13, card 0)
// stalled past its deadline - "Unrecoverable native verification failure: verify: timed out at
// layer 13" - and the engine self-exited.  The static boot reserve (--vram-reserve-mib, one figure
// for every card) only partially mitigates that: the pressure is per-card and time-varying.
//
// The guard (operator mandate, round 18m: "fix residency but only by 64mb steps on the gpu it
// happened at"): when a card reports less free VRAM than the request-start warning's low line
// (kVramSafeMib in src/program/generate.cpp, mirrored in the TU), THAT card's reserve floor grows
// by exactly one step (64 MiB by default) and the card's reclaim donor is asked to free about a
// step's worth.  The floor never shrinks on its own (hysteresis: a card that once ran low keeps
// its headroom) and the growth stops at +STRATA_VRAM_GUARD_MAX_MIB over the card's boot floor.
//
// STRATA_VRAM_GUARD=1 enables; any other value or unset leaves the whole thing inert - a check
// site pays one atomic load.  STRATA_VRAM_GUARD_STEP_MIB (default 64) and
// STRATA_VRAM_GUARD_MAX_MIB (default 4096) tune the step and the cap.
//
// The check sites (Verifier::capture, the mtp draft wait, the conversation copy path) own the
// device reads: this module makes NO CUDA call, so the host test (tests/core/vram_guard_test.cpp)
// links its TU with no GPU and no runtime.
#ifndef STRATA_CORE_VRAM_GUARD_H
#define STRATA_CORE_VRAM_GUARD_H

#include <cstdint>

namespace strata::core::vram_guard {

/// One relaxed atomic load: the STRATA_VRAM_GUARD=1 switch, off unless the env says exactly "1".
bool enabled();

/// Overrides the env knobs (the engine or the host test calls this before the first check;
/// afterwards the env is ignored).  `min_step_ms` is the window between two steps on one card:
/// a card that stays under the line steps at most once per window, so a tight check loop
/// cannot walk the floor to the cap in seconds.
void configure(bool on, int64_t step_mib, int64_t max_growth_mib, int64_t min_step_ms);

/// The engine's per-card reclaim donor: free about `want_mib` on `device` (that card's
/// expert-cache slots are the natural pool) and return the MiB it actually freed.  It runs on
/// the check path under the guard's lock, so it must not call back into the guard, allocate,
/// or wait on a stream the caller depends on.  nullptr (the default) means the guard grows the
/// floor and logs that nothing was reclaimed.
using Reclaim = int64_t (*)(int device, int64_t want_mib);
void set_reclaim(Reclaim fn);

/// Publishes the card's static boot reserve (--vram-reserve-mib): the floor starts there and the
/// cap counts the growth over it.  Idempotent; a rising value lifts the floor, nothing lowers it.
void set_base_mib(int device, int64_t base_mib);

int64_t base_mib(int device);    ///< the boot floor the card started from
int64_t floor_mib(int device);   ///< the card's current reserve floor (never lower than the base)
int64_t steps(int device);       ///< how many times the card's floor has stepped up

/// The probe the check sites call with the card's CURRENT free MiB (they read the device
/// themselves - most sites already hold the cudaMemGetInfo figure, and skip everything when
/// disabled).  A card under the low line steps its floor by exactly one step - unless the cap,
/// the step window or the switch stops it - and asks the donor for the same amount.  Returns
/// true while the card is STILL under the low line (the caller keeps its own warning); false
/// when there is nothing to guard.
bool check(int device, int64_t free_mib);

/// Forgets the device's history (tests, between cases).
void reset(int device);

}  // namespace strata::core::vram_guard

#endif

// include/strata/core/expert_helper_reserve.hpp - the CUDA helper's --reserve-mib floor, honest.
//
// The helper's fill budget is `device free - reserve - scratch - buffers`, and until v0232 the
// reserve the budget ACTUALLY used was never the helper's own --reserve-mib: the option was parsed
// and validated, then simply never read, and the engine's kHello value (--expert-helper-reserve-mib,
// the engine's own 512 default) overwrote the helper's global on every connect.  Measured: a helper
// launched with --reserve-mib 300 or 320 still preflighted "reserve 512 MiB" - the launcher's 300
// default was dead and occupancy was code-capped, not config-capped.
//
// From v0232 the helper's own --reserve-mib is the budget's reserve, clamped to a representable,
// sane range [64, 4096]: the helper owns its card, so the process that owns the device owns its
// headroom knob (STRATA_HELPER_RESERVE on the launcher).  The kHello field stays on the wire
// unchanged (its layout is pinned) and stays read by pre-v0232 helpers; the v0232 helper ignores
// it in favour of its own option.  The floor is 64 MiB - honest, not safe: below ~256 MiB a
// display/peer hiccup on the card can OOM the fill, so values under 256 warn once at startup.
#pragma once

#include <algorithm>

namespace strata::core::expert_helper {

/// The clamp's range.  64: the honest floor the operator asked to be able to TEST (the old
/// effective floor was the engine's 512 default, silently).  4096: past this the reserve is no
/// longer a headroom but the card's whole budget, and a typo (51200) should not empty the fill.
inline constexpr int64_t kReserveFloorMiB = 64;
inline constexpr int64_t kReserveCeilMiB = 4096;
/// Below this the fill has little room to survive the card's other users; warned once, not refused.
inline constexpr int64_t kReserveWarnBelowMiB = 256;

/// The reserve the fill budget uses: `given` clamped into [kReserveFloorMiB, kReserveCeilMiB].
inline int64_t clamp_reserve_mib(int64_t given) {
    return std::clamp(given, kReserveFloorMiB, kReserveCeilMiB);
}

}  // namespace strata::core::expert_helper

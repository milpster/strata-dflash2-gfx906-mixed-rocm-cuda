// include/strata/core/expert_helper_xq_ring.hpp - F2 (STRATA_HELPER_XQ_RING, default OFF): the GPU's
// q8_1 activation image, published to a host-mapped ring beside the doorbell and consumed by the
// helper's claim frames (remote_experts.cpp proxy_begin) in place of the per-frame CPU quantize.
//
// CPU-only header arithmetic, exactly the expert_helper_socket.hpp gate pattern: the knobs, the
// ring's slot math and the per-window seq reset are shared by the Verifier (verify.cpp) and the
// host tests, and link no GPU runtime.  The publish kernel itself is doorbell_xq_publish
// (strata/kernels/elementwise.hpp); the ring's lifetime is Verifier-owned, identical to m_x_.
#pragma once

#include <cstdint>
#include <cstdlib>

namespace strata::core {

/// F2's ENGINE half of the gate - STRATA_HELPER_XQ_RING, default OFF.  Exactly "1" enables; absent,
/// empty, "0" and any other value keep today's bytes (the per-frame quantize_q8_1_cpu).  No wire
/// change rides this gate: the helper cannot distinguish the image's origin, so mixed engine/helper
/// builds are safe by construction (unlike W1's kFeatureF32Window, F2 needs no feature bit).
inline bool helper_xq_ring_enabled() {
    const char* v = std::getenv("STRATA_HELPER_XQ_RING");
    return v != nullptr && v[0] == '1' && v[1] == '\0';
}

/// Set once by the driver before the verifiers init: a window-tier helper is configured (the
/// RemoteExpertOpt peers or the out-of-process expert-helper socket tier).  Publishing the ring
/// with no consumer is pure waste, so the Verifier's static gate is the env knob AND this.
inline bool& helper_xq_ring_consumer() {
    static bool present = false;
    return present;
}

/// One ring slot: max_t rows of q8_1 (36 B per 32 values - the kBegin wire's own blocks).
inline constexpr int64_t xq_ring_slot_bytes(int64_t max_t, int64_t n_embd) {
    return max_t * (n_embd / 32) * 36;
}

/// The per-window seq reset, stage_inputs' sibling of the h_seq_/h_flag_/flagA/flagB zeroes: ring
/// values restart at 1 every window ((l-lb_)*G+grp+1 is capture-baked), and without this reset
/// window >= 2's wait passes instantly on window 1's stale values and the host reads a STALE image.
/// Safe: the previous window's graph is fully synced before the next stage_inputs.
inline void xq_ring_reset(uint32_t* h_xq_seq) {
    *(volatile uint32_t*) (h_xq_seq + 0) = 0;
    *(volatile uint32_t*) (h_xq_seq + 1) = 0;
}

/// The service loop's readiness test for slot `slot` (step parity) at ring value `want`.
inline bool xq_ring_ready(const uint32_t* h_xq_seq, int slot, uint32_t want) {
    return *(const volatile uint32_t*) (h_xq_seq + slot) >= want;
}

}  // namespace strata::core

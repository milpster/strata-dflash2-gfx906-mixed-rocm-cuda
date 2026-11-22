#!/bin/sh
# Build and run the serving lane's own tests on this box, without touching a GPU.
#
# Why a script and not a command line: the run needs three things at once that are easy to forget
# separately - the two opt-ins the lane's groups sit behind, the lane-only test filter, and a device
# that is invisible. Forgetting the last one is the expensive one: several of these programs link
# strata_engine, so they carry the GPU runtime, and letting them allocate VRAM takes memory from the
# engine this box serves from. They are written to skip themselves (exit 77) when no device is there,
# which is what HIP_VISIBLE_DEVICES=-1 gives them: an honest SKIPPED, never a pass, and never a device.
#
# Usage: sh tools-port/lane-tests.sh [BUILD_DIR]            the default DFlash2 pair
#        PAIR=lane sh tools-port/lane-tests.sh [BUILD_DIR]  the serving lane's pair
# It configures BUILD_DIR with the gfx906 pins (tools-port/configure-906.sh) plus the two opt-ins,
# builds the lane's programs, runs them, and leaves the logs in logs-port/.
#
# PAIR=lane configures STRATA_ENABLE_DFLASH2=ON / STRATA_HAVE_DFLASH2=OFF - the pair this box serves
# with - and adds the four device-parity programs of the lane's DFlash runtime, which that option
# gates (dflash_taps_test, dflash_rope_parity, dflash_inco_parity, dflash_load_test).  On the default
# pair those four are not even built, so they are only asked for when the pair compiles them.
ANCHOR='$'
set -eu
PORT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="${1:-build-906}"
LOGS="$PORT/logs-port"
mkdir -p "$LOGS"

# every program cmake/strata_lane_tests.cmake registers. native_mlock_test runs as ten named cases and
# ram_pool_test as two, so the case set is wider than the target set.
TARGETS="vram_guard_test expert_helper_reserve_test expert_helper_candidates_test expert_helper_arena_cap_test \
arena_pin_ranges_test conv_cache_knobs_test swapin_depth_test tail_skip_guard_test read_once_test \
pipeline_chunks_test cache_spill_test dflash_refusal_test dflash_tap_plan_test \
native_mlock_test native_mlock_nonlinux_probe \
ram_pool_test ram_pool_nonlinux_probe pool_expert_cache_test pool_expert_store_test \
bounded_direct_reader_test pool_expert_source_adapter_test \
expert_helper_socket_test expert_helper_f32_window_test expert_helper_fallback_test helper_microbench \
p2_measure_test dflash_policy_test dflash_tap_collect_test dflash_tap_slot_test dflash_owned_kv_test \
expert_helper_ownership_test dflash_ring_test conversation_disk_cache_test"
# dflash_head_bf16_test is deliberately NOT here: it is EXCLUDE_FROM_ALL, and it fails to compile for a
# reason that belongs to the fork (PORT-NOTES.md item 7). Building it on purpose is `ninja -C $BUILD
# dflash_head_bf16_test`.
CASES='^(vram_guard_test|expert_helper_reserve_test|expert_helper_candidates_test|expert_helper_arena_cap_test|arena_pin_ranges_test|conv_cache_knobs_test|swapin_depth_test|tail_skip_guard_test|read_once_test|pipeline_chunks_test|cache_spill_test|dflash_refusal_test|dflash_tap_plan_test|native_mlock_test_[a-z_]+|native_mlock_nonlinux_probe|ram_pool_test_[a-z_]+|ram_pool_nonlinux_probe|pool_expert_cache_test|pool_expert_store_test|bounded_direct_reader_test|pool_expert_source_adapter_test|expert_helper_socket_test|expert_helper_f32_window_test|expert_helper_fallback_test|helper_microbench|p2_measure_test|dflash_policy_test|dflash_tap_collect_test|dflash_tap_slot_test|dflash_owned_kv_test|expert_helper_ownership_test|dflash_ring_test|conversation_disk_cache_test)$'

echo "==> configure $BUILD (gfx906 pins + the lane's two opt-ins)"
if [ "${PAIR:-default}" = "lane" ]; then
  DFLASH_ON=ON; DFLASH_HAVE=OFF; EXTRA_TARGETS="dflash_taps_test dflash_rope_parity dflash_inco_parity dflash_load_test"
  EXTRA_CASES='|dflash_taps_test|dflash_rope_parity|dflash_inco_parity|dflash_load_test'
else
  DFLASH_ON=OFF; DFLASH_HAVE=ON; EXTRA_TARGETS=""; EXTRA_CASES=""
fi
sh "$PORT/tools-port/configure-906.sh" "$BUILD" -- \
  -DSTRATA_ENABLE_DFLASH2=$DFLASH_ON -DSTRATA_HAVE_DFLASH2=$DFLASH_HAVE \
  -DSTRATA_BUILD_RAM_POOL_TEST=ON -DSTRATA_BUILD_CONVERSATION_TESTS=ON \
  > "$LOGS/lane-tests-configure.log" 2>&1
echo "    logs: $LOGS/lane-tests-configure.log"

echo "==> build"
# -k 0: one program that cannot build must not hide the results of the others.
ninja -C "$PORT/$BUILD" -k 0 $TARGETS $EXTRA_TARGETS > "$LOGS/lane-tests-build.log" 2>&1 || true
grep -E "^FAILED:" "$LOGS/lane-tests-build.log" | sed 's/CMakeFiles\///;s/\.dir.*//' | sort -u | sed 's/^/    did not build: /' || true

echo "==> run (no device visible to any of them)"
cd "$PORT/$BUILD"
CASES_ALL="${CASES%$ANCHOR}${EXTRA_CASES}${ANCHOR}"
HIP_VISIBLE_DEVICES=-1 CUDA_VISIBLE_DEVICES=-1 ctest -R "$CASES_ALL" \
  > "$LOGS/lane-tests-run.log" 2>&1 || true
grep -E "tests passed|did not run|^\s+[0-9]+ - .*\(Failed\)" "$LOGS/lane-tests-run.log" | sed 's/^/    /'
echo "    logs: $LOGS/lane-tests-build.log and $LOGS/lane-tests-run.log"

#!/usr/bin/env bash
# ram_raw_launch.sh — operator entry for the FUTURE production-down RAM raw pair window.
#
# Single ownership path (the adapter's unscoped-outer + scoped-worker model, mirrored here):
#   1. preflight the production-absence gate (READ-ONLY; launches nothing, imports no server);
#   2. launch the expert helper UNSCOPED, from SETTINGS the adapter owns (`helper-command`),
#      wait for "listening" + both isolated sockets;
#   3. launch the engine worker INSIDE the canonical engine scope (systemd-run --user --scope
#      -p MemoryMin=48G), so the helper is OUTSIDE the engine scope by construction;
#   4. the worker closes its engine in its own finally; this outer owner then terminates/waits
#      the helper (also on worker failure via the trap), so the engine is always closed first
#      and the pair never leaks.
#
# This script NEVER stops or signals production: a live pair makes step 1's preflight (and the
# worker's own re-check) refuse. Helper/engine SETTINGS are the adapter's authority, not here.
set -euo pipefail

RAM_RAW_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PY=${RAM_RAW_PY:-/mnt/srcds/Strata/.venv/bin/python}
MANIFEST=${RAM_RAW_MANIFEST:?set RAM_RAW_MANIFEST to the frozen test manifest}
FIXTURE=${RAM_RAW_FIXTURE:?set RAM_RAW_FIXTURE to the frozen request fixture}
STOP_PROOF=${RAM_RAW_STOP_PROOF:?set RAM_RAW_STOP_PROOF to a stop-all output that reads ALL STOPPED}
[[ -n ${RAM_RAW_TEST_OUT:-} ]] || { echo "REFUSE: set RAM_RAW_TEST_OUT to an isolated dir" >&2; exit 2; }
mkdir -p "$RAM_RAW_TEST_OUT"

# Import-time flag serve.server reads must exist BEFORE the worker's lazy import. Canonical
# engine env mirrors launch-v0238.sh; config.env still overrides per-child via child_env last.
STRATA_ENGINE_READY_S=${STRATA_ENGINE_READY_S:-2400}
export STRATA_ENGINE_READY_S

# 1. Read-only preflight. Refuses (no helper, no import) unless stop proof + absence hold.
"$PY" "$RAM_RAW_DIR/ram_raw.py" preflight --manifest "$MANIFEST" --stop-proof "$STOP_PROOF"

# 2. Helper SETTINGS come from the adapter (single authority); launch it UNSCOPED.
helper_json=$("$PY" "$RAM_RAW_DIR/ram_raw.py" helper-command --manifest "$MANIFEST")
mapfile -t HELPER_ARGV < <("$PY" -c 'import json,sys; print("\n".join(json.loads(sys.argv[1])["argv"]))' "$helper_json")
helper_pid=
cleanup_helper() {
  [[ -n "$helper_pid" ]] && kill -TERM "$helper_pid" 2>/dev/null || true
  [[ -n "$helper_pid" ]] && wait "$helper_pid" 2>/dev/null || true
}
trap cleanup_helper EXIT INT TERM
"${HELPER_ARGV[@]}" >"$RAM_RAW_TEST_OUT/helper.log" 2>&1 &
helper_pid=$!

# Wait for readiness (the helper's own "listening" line + both isolated sockets); refuse, not kill prod.
for _ in $(seq 1 600); do
  grep -q 'expert-helper: listening on' "$RAM_RAW_TEST_OUT/helper.log" && \
    [[ -S $("$PY" -c 'import json;print(json.load(open("'"$MANIFEST"'"))["socket1"])' 2>/dev/null) ]] && break
  kill -0 "$helper_pid" 2>/dev/null || { echo "REFUSE: helper exited before ready" >&2; exit 3; }
  sleep 0.5
done
[[ -n "$helper_pid" ]] && kill -0 "$helper_pid" 2>/dev/null || { echo "REFUSE: helper never ready" >&2; exit 3; }

# 3. Engine worker, SCOPED (helper above is already outside this scope). It re-runs the
#    preflight and closes its engine in its finally before this wait returns. Capture its rc
#    without tripping `set -e` so the owned helper is still stopped (the trap covers aborts).
env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \
    -u HSA_USERPTR_FOR_PAGED_MEM -u LD_PRELOAD \
    HSA_OVERRIDE_GFX_VERSION=9.0.6 GPU_MAX_ALLOC_PERCENT=100 \
    LD_LIBRARY_PATH="${STRATA_ROCM_ROOT:-/home/srcds/rocm-gfx906-xnack}/lib:/opt/rocm-6.1.0/lib" \
systemd-run --user --scope -p MemoryMin=48G \
  "$PY" "$RAM_RAW_DIR/ram_raw.py" run --worker --manifest "$MANIFEST" --fixture "$FIXTURE" \
    --stop-proof "$STOP_PROOF" --confirm-production-down && worker_rc=0 || worker_rc=$?

# 4. Engine closed in the worker's finally; now the outer owner stops the owned helper.
cleanup_helper
trap - EXIT INT TERM
exit "$worker_rc"

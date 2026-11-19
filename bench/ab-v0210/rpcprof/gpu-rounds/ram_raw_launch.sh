#!/usr/bin/env bash
# ram_raw_launch.sh — operator entry for the FUTURE production-down RAM raw pair window.
#
# Single-ownership model: the Python adapter (ram_raw.py) owns the canonical helper
# command, the engine argv and provenance. This script does NOT re-derive any of that.
# It only (a) establishes the import-time + engine-scope environment that must exist
# BEFORE serve.server is lazily imported, (b) refuses to proceed if production identities
# are live (read-only; it never stops or signals production), and (c) delegates to
# `ram_raw.py execute` inside the canonical engine MemoryMin scope.
#
# Helper SETTINGS come from: python3 ram_raw.py helper-command ... (adapter authority).
# Exact placement of the helper OUTSIDE the engine cgroup in a real window is a recorded
# open runtime gate (see the adapter + journal); this script does not fake that.
set -euo pipefail

[[ -n ${RAM_RAW_TEST_OUT:-} ]] || { echo "REFUSE: set RAM_RAW_TEST_OUT to an isolated dir" >&2; exit 2; }
RAM_RAW_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PY=${RAM_RAW_PY:-/mnt/srcds/Strata/.venv/bin/python}
mkdir -p "$RAM_RAW_TEST_OUT"

# Canonical engine environment for the embedded raw path: import-time flags first, then the
# engine scope env. These mirror launch-v0238.sh; config.env still overrides per-child via
# the adapter's child_env (last-wins), so config.env is NOT re-applied here.
STRATA_ENGINE_READY_S=${STRATA_ENGINE_READY_S:-2400}
export STRATA_ENGINE_READY_S
HSA_OVERRIDE_GFX_VERSION=9.0.6 GPU_MAX_ALLOC_PERCENT=100 \
LD_LIBRARY_PATH="${STRATA_ROCM_ROOT:-/home/srcds/rocm-gfx906-xnack}/lib:/opt/rocm-6.1.0/lib" \
env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \
    -u HSA_USERPTR_FOR_PAGED_MEM -u LD_PRELOAD \
systemd-run --user --scope -p MemoryMin=48G \
  "$PY" "$RAM_RAW_DIR/ram_raw.py" execute --confirm-production-down

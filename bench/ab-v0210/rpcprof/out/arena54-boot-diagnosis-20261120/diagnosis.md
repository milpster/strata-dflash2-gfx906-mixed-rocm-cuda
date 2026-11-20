# Arena-54 pre-gen boot failure — root cause (2026-11-20, parent diagnosis)

Symptom: v0239 engine exited 2 every pre-gen generation (32x, ~33s cycle) with
"conversation parking does not support a layer split on one GPU (--split-device 0)";
experts.bin never written; supervisor flapped; window aborted; prod restored OK.

Chain of evidence (all with the exact trial engine args, prod up, exits before loading):
- v0238p0 AND v0239 standalone: "layer split across 2 GPUs" — binaries fine.
- Full launcher env, systemd-run scope, HIP_VISIBLE_DEVICES=0,1: all 2 GPUs — fine.
- Full merged child env (config env added): REPRODUCED exit 2.
- STRATA_NATIVE_MLOCK=arena alone: fine. STRATA_ARENA_MMAP=1 alone: REPRODUCED.
- STRATA_ARENA_MMAP=1 + GPU_PINNED_MIN_XFER_SIZE=0: 2 GPUs, clean.
- GPU_PINNED_MIN_XFER_SIZE=1000000 alone (no ARENA_MMAP): REPRODUCED.

Root cause: STRATA_ARENA_MMAP=1 makes main() setenv("GPU_PINNED_MIN_XFER_SIZE",
"1000000", 0) (generate.cpp:3758, perf-review D-4 staging optimization, measured
49.06 vs 49.16 ms/window — a wash). On this ROCm 6.1.0 + xnack runtime that raised
ROCclr pin threshold makes cudaGetDeviceCount see <= 1 device -> split_devs=[0] ->
split_same -> parking guard (generate.cpp:4599) -> exit 2. ARENA_MMAP was designed
for single-GPU small-RAM machines; nobody ran it with a layer split + parking.

Fix (config-level, free): trial-config-arena54.json env GPU_PINNED_MIN_XFER_SIZE=0 —
the engine's setenv uses overwrite=0, an explicit value wins (proven by the probe).
Commit f0b075e7a3.

Parked engine-side proper fix: skip or guard the knob when it breaks enumeration
(e.g. only raise it when no layer split / enumerate first). Not needed for the window.
Evidence: the *.out files + enum-v0239.strace in this directory.

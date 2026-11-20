# SPEEDS INVALIDATED — operator directive 2026-11-20

Per operator directive (2026-11-20, during Phase B): **ALL speed, throughput,
latency and wall-time observations from this window are invalidated as
evidence.** Do not cite, do not register, do not compare.

## Struck

- `probe.log` / `window.log` T0-style probes (24.58 / 29.81 / 25.76 tok/s calc;
  23.7 / 28.7 / 24.9 reported; walls 28.53 / 17.30 / 19.92 s)
- turn1 wall (135.60 s) and any per-phase timing in this directory
- Any speeds the window agent reports at completion — including any derived
  medians or comparisons to the trial or to prod's old baseline

## Why (recorded reasons)

1. This is an operational validation window (promotion gates + park-recover
   mechanics), not a measurement leg: no baseline arm, no same-day A/B pair.
2. Wall values deviate from the validated trial: boot ran 56G/4G; the
   operator-ordered 55G/10G applies after this window — speeds measured under
   a wall that is being replaced.
3. Park-conversation turns interleaved with probes; boot-state confounds
   uncontrolled in this window.

## Still valid from this directory (mechanics only)

- Gate results: 2-GPU split, mlock exactly 1 span 47965/47965 MiB, VmLck,
  wall measured from cgroup, oom counters
- experts.bin size + sha, pin integrity checks
- Park-recover behavior: park write, restart, `reused > 0`, absence of a
  78.7 s-class cold read, conversation continuation correctness

## Unaffected speed evidence

- `arena54-trial-20261120` attempt 3 (T0 median 30.3, T1 30.45 tok/s,
  confound-flagged) — stands as before.
- Attribution of the +33% signal moves entirely to the planned same-binary
  A/B arm (v0239, arena-mmap OFF), which remains queued.

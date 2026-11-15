# Cross-boot sha non-reproducibility — findings and gate policy

Status: pinned property of the mixed-GPU stack as of 2026-11-15. Evidence: journal
`serving-tg-reeval-split-retest-2026-11-11.md`, rounds 18al, 18ar-era repro pins, 18bq, 18br.

## What is observed

- **Cross-boot**: greedy temp-0 generations of the same prompt diverge across boots
  (baseline repro pin: 7 distinct shas from 8 generations).
- **Within-boot**: even back-to-back identical greedy POSTs on one boot diverge
  (18bq: warm3a ≠ warm3b, 192 vs 131 words — trajectories separate early).
- **Teacher-forced is reproducible**: `tools/quant_quality.py` logprob probes are
  bit-identical run-to-run (KL 0.00000, p99 0.0, PPL equal across boots — 18br gate).

## What has been ruled out as the cause

- **Drafting-dependent rounding in the CPU expert kernels** — `STRATA_IQ_MT_MIN=1`
  forces multi-token kernels for every group (native_expert.cpp:351-368); 5 distinct
  shas from 5 generations with it on (18bq). Dead.
- Kernel-layout changes per se — MMVF_ROWS (fused 2–8-row BF16 GEMV) is bit-identical
  teacher-forced (18br); it neither causes nor cures divergence.

## Current mechanism account (18al)

Free-running decode is a speculative loop: tiny logit differences from placement/timing
(expert placement trajectory, async admission order, pool timing) flip near-tie
draft accept/reject boundaries; once one token differs, the continuation diverges
combinatorially. Teacher-forced probes never let a divergence compound — one forward
pass, no feedback — hence bit-identical.

## Gate policy (tier D, current)

- **Never** use free-running greedy sha as a quality or reproducibility gate across
  boots. It is not reproducible by construction.
- Quality gates: **teacher-forced KL + PPL** via quant_quality (campaign convention:
  ≥3 clean passes; KL 0.00000 + identical PPL for bit-exact claims).
- Speed metric: engine-line rates (`tokens = … generated in …`) — judge legs, not walls.

## Higher tiers (deferred, engine roadmap)

- **Tier A — deterministic repro rig**: fixed slot counts, foresight/shadow off, one
  rounding regime; would give cross-boot greedy sha repro (~30 min config + 3 boots).
  Shelved with the bistability investigation (operator, 2026-11-15).
- **Tier B/C — placement pinning / bit-exact GPU expert contract**: future work;
  only paths to same-prompt-same-sha serving.

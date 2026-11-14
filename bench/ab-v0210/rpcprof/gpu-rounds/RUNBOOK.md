# GPU rounds — prepared 2026-11-14, DO NOT RUN until operator says go (or autonomous green light)

Order and gates. Each round is one script; every script refuses to run if the
previous round's gate is unmet (marker files under out/gpu-rounds-20261114/).

## Round 1 — residency flip + boot verification (round1-residency-boot.sh)
Gate in: lane A (register-last) landed + reviewer PASS + fresh binary at
  /mnt/srcds/Strata-wt-integ/build-mixed-gpu/rpcw-hip/strata (BIN= override allowed).
Does: stop-all -> backup config -> drop the four relief args -> keep reserves 576
  + all champion env knobs + startup-timeout 1200 -> pin binary + SHA256SUMS ->
  commit config -> boot -> verify banners, in order:
    1. "GPU host-callback thread started" BEFORE any arena registration line
    2. "registering the whole arena (NN.N GiB) as the last boot step" then "... ok in N s"
       appearing AFTER the expert-cache fill line and the helper handshake
    3. "loaded 46.84 GiB" -> health on :8009 -> first-request smoke (temp-0 completion, reviewer note E) -> 8 min survival watch
Gate out: banners in order + health + survival -> marker round1.ok
Failure: any hang past startup-timeout 1200 s, or banners out of order ->
  config auto-restored from backup, relief binary re-pinned, boot relief, marker round1.fail.

## Round 2 — killer-shape PP (round2-killer-pp.sh)
Gate in: round1.ok. Runs ON the residency boot (no restart needed).
Does: ab9_driver prefill leg ctx=995000 (unique tag, 0-reused, temperature 0) ->
  judge line "N tokens = 0 reused + N read in X ms" -> tok/s; then the SAME leg
  again (cached rerun) whose completion sha256 MUST equal run 1 (temp-0 determinism
  guard — register-last is transport-only and may not change one token).
Gate out: sha match + tok/s recorded vs the T1 reference (180k read at 620 tok/s;
  R3A-era killer measured 466.7). Verdict line KILLER_PP: <tps> tok/s sha=OK|MISMATCH.
sha MISMATCH = correctness stop: quarantine (STRATA_REGISTER_LAST=0), fix round before any promotion.

## Round 3 — ARM7 ring sweep (round3-arm7-ring.sh)
Gate in: round2 done (sha OK) + prod may be taken down again.
NOTE (staging review): with STRATA_STAGE_OFFTHREAD=1 this is the stager's FIRST-EVER execution
  (zero host-test coverage on the ON path). Pass criteria: stage_drops/ring_busy report lines present,
  ring-full rejections ~0, checkpoints 1-4 — and any temp-0 sha delta vs knob-off is a DEFECT
  (transport-only change; rows shifting helper/CPU->GPU-slot must stay bitwise identical), not noise.
Does: for R in 64 128: boot champion env + STRATA_FS_RING=$R (+ STRATA_STAGE_OFFTHREAD=1 arm
  and a knob-off control) via boot-arm.sh -> panel-tg.sh ARM7_R$R -> per-leg fresh-vs-rerun sha
  verdicts (determinism within arm) + tps vs champion records (out/ab9-window-20261113/ARM1_*).
Gate out: winner table; ring only promotes if >= champion warm-180k TG AND all
  sha verdicts OK; otherwise champion stands and the env is simply not set.

## Restore (restore-prod.sh)
Brings prod back on the best-measured config (default: residency if round1+2 green,
else relief+576) — run at the end of any GPU window.

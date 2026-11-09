# SPEC-stage2 — memory levers, prepared arms, measured baseline (no prod-down work done yet)

Baseline: `bench/ab-v0210/rpcprof/out/mem-stage2-base-20261108-172208.txt`
(collector: `bench/ab-v0210/rpcprof/mem-baseline.sh`, read-only, rerunnable any time, prod untouched).

## Measured baseline (2026-11-08 17:22, prod = `v0211` f16kv pair)
- Host 63.6 GiB: used 54.5, MemAvailable **9.1 GiB**, swap 8.4 GiB used (6 swapfiles, `/home/swap3,4` unused).
- Engine RSS **47.09 GiB** = anon 39.37 + file 7.72; Pss 43.85 (anon 39.37, file 4.48).
- Helper RSS 6.75 GiB, **file-backed 6.56** → Pss 3.51 GiB. Its footprint is page cache, not anonymous RAM.
- Vision 0.93 GiB anon; server 0.25 GiB.
- VRAM: VII#0 16309/16662 MiB (~**59 MiB free**), VII#1 16210 (~158 MiB free), 3080 7145/8192 (**698 MiB free**).
- KSM: `general_profit = −109.9 MB` (worse than the −49.8 MB read 1 h earlier), `pages_sharing=141`, ksmd 0.2 % CPU.
- AnonPages 47.88 GiB, Cached 10.7 GiB, SwapCached 1.79 GiB, pgmajfault 13.9 M cumulative.

## Lever status after measurement (corrections to the plan file)
1. **VII VRAM headroom — live deficit, top priority.** Both VIIs are within 59–158 MiB of full; the engine already
   logs `below 512 MiB ... driver can page GPU memory to system RAM`. Arms to prepare: lower CUDA1 (VII#1)
   `--expert-cache` slots (4407) and/or its borrowed prompt slots (3608), raise `--vram-reserve-mib` /
   `--vram-reserve-later-mib` (256), and evaluate `--layer-split` shift (32 → 36/40). Success = zero sub-512 MiB
   warnings, zero paging stalls, PP/TG within drift `.9..1.1`.
2. **Deeper relief onto the 3080 — nearly dead.** Only 698 MiB free (not the 7471 MiB seen at hello; the card
   fills after context+buffers), so the planned `--fill-mib 6796 → 7200` gain is ≤ ~0.4 GiB and only by cutting
   `--reserve-mib` 300 → 128. Prepare as a single arm with the reserve change; expect a small, not structural win.
3. **KV dtype `fp16 → int8`** — halves resident KV at 262144-resident; AB8 proved no speed difference. Prepare
   arm: clone the live config with `--kv int8` only. Gate: sha byte-parity (temp 0, thinking off, fresh boot,
   8k) + PP/TG drift + no `int8` harness wedge (that wedge was harness-shape-specific).
4. **`STRATA_KSM=0`** — measured net loss and growing. Zero-risk config-only change at the next natural start.
   Record ksm counters before/after; no window needed (launcher env, applies at next pair start).
5. **Conversation cache — ALREADY LIVE as disk-only** (committed `d62feebc3`; engine cmdline:
   `--conversation-cache-mib 0 --conversation-cache-dir data/conversation-cache
   --conversation-cache-disk-mib 24576 --conversation-cache-disk-ram-mib 64 --conversation-cache-disk-idle-save`).
   Live measured cost, from `logs/engine-swift15-f16kv-dflash-w4-q4_0-mixed.log`:
   - park (save): 63–67k tokens in **3.2–3.9 s**, 137–141k tokens in **5.9–6.3 s** (~22 MB/s writes, streaming).
   - restore: 63448 tok **33.7 s**, 64179 tok **33.3 s**, 137218 tok **54.1 s**, 139218 tok **55.7 s**,
     140563 tok **56.1 s** → ~1.9–2.5 k tok/s restore, i.e. a 140k conversation costs ~56 s of TTFT.
   This is exactly the AB8 warm-followup stall, and it is now the price of ~2 GiB+ host-RAM relief.
   **This lever belongs to `/home/srcds/.opencode/plan/strata-disk-cache-streaming.md`.** Here it is measured as a
   metric only; the optimization target is restore throughput (~2.5 k tok/s today).
6. **Registered complement 33.37 GiB** — `--pcie-frac 0` (plus `STRATA_NO_PCIE_GROUP=1` in the live config env)
   means the engine never reads expert bytes over PCIe, so pinned bulk beyond DMA-touching ranges is pure cost.
   Preparation (no window): smaps range census of the engine vs the DMA tiers
   (`awk` over `/proc/1119294/smaps`, count `KernelPageSize`/`Mapped` ranges) → identify how much registered
   memory is never touched. Implementation must keep the proven drop-before-register sequencing (V4-r3/r4).
   Parked: hugepages (`vm.nr_hugepages=0`, MAP_HUGETLB needs 20464 2 MiB pages), THP (kernel-dead),
   whole-arena registration (blocks relief).

## Prepared, window-ready (nothing has been run)
- `mem-baseline.sh [label]` — rerun before/after every arm (the gate evidence).
- `SPEC-ab9.md` — Stage 1 arms + legs + gates (Stage 1 and Stage 2 arms can share ONE window: arms are disjoint
  variables; sequence Stage 1 A/B/C first, then Stage 2 lever arms, then restore prod supervised).
- Stage 2 arms to materialize as configs (owner: lane 3, together with ab9):
  `arm-m1.json` (VII#1 slot/reserve trim), `arm-m2.json` (`--kv int8`), `arm-m3.json` (fill 7200 + reserve 128),
  `arm-m4.json` (layer-split 36) — each derived from `prod-config-swift15-f16kv-dflash-w4-q4_0-mixed.json` by a
  single-variable diff, with socket/log/name kept per the launcher↔config cross-check rule (the `ud` socket-name
  defect is the precedent).
- Gate for every Stage 2 arm: `mem-baseline.sh` before/after + PP/TG drift `.9..1.1` + sha byte-parity +
  zero sub-512 MiB VRAM warnings + ≥30 min real-traffic soak. Adopt one lever at a time; prod swap on explicit go.

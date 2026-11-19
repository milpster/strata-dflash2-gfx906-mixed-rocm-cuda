# Targeted conversation-parking validation — 2026-11-19

Operator order (2026-11-11 journal, reactivated 2026-11-19): validate the conversation
parking/reloading fix — park/restore under RAM pressure and checkpoint restart recovery.
Blanket go granted 2026-11-19 ("go for all further tests"). Canonical engine only
(`b0ec0109…`, source `42cb41b314`), prod config `95ef3693…` unchanged, nothing promoted.

## Already observed (no new test needed)

Evidence: `logs/engine-prod-ud-iq3xxs-v0233.log` (the supervisor engine log; the wrapper
`prod-swift15-*.log` does not carry these lines).

| Behavior | Evidence (line, 2026-11-19) |
|---|---|
| Idle disk park streams | 35658, 35686, 35700, 35728, 35738 (`disk park streamed N tokens idle to conv-NN.sess`) |
| Park-gain gate | 35651, 35665 … (`idle save skipped (park-gain: advanced X of 4096 …)`) |
| Over-ceiling park → disk only | 35735 (`parked to disk only … snapshot 3356 MiB over the 512 MiB RAM ceiling`) |
| Page-cache release | park lines `pages-released=…`; boot banner line 35894 |
| Boot-time disk-tier scan clean | 35895 (`disk tier ON … generations=4 … corrupt=0 foreign=0`) |
| Live RAM prefix reuse | 35767-35781 (`prompt 19x k tokens = 19x k reused + 40 read`) |

## T1 — park + restore round-trip (no artificial pressure)

1. New conversation on `127.0.0.1:8009/v1/chat/completions` (no key): the frozen
   88400-B corpus prompt (reserve panel corpus), prod sampling, `max_tokens 256`.
2. Wait ≤ 120 s idle; expect a `disk park streamed … conv-NN.sess` line and the new
   file in `/home/srcds/strata-conv-cache/`.
3. Continue the same conversation (append one turn); expect the request line to show
   `reused ≈ 16 k`, wall of the cached-leg class (the panel's cached legs ran 9-30 s).

PASS: park line + file + restore reuse + no stall. FAIL: any stall > 90 s, missing
park, or zero reuse on continue.

## T2 — park + restore under RAM pressure

1. Bounded allocator (python `bytearray`, hard cap 5 GiB, RSS reported, sampled every
   5 s alongside MemAvailable/swap/engine RSS) until MemAvailable ≈ 3 GiB — above the
   2560 MiB engine floor, below the standing ~8 GiB.
2. Repeat T1 (new conversation, park, continue) while held.
3. Expect over-ceiling or pressure parks logged (`parked to disk only` / eviction
   lines), restore still correct, no request-path stall.

PASS: park + restore correct under pressure, walls ≤ 3× T1, allocator reaped clean.
FAIL: stall (18v-class spin), restore corruption, or guard refusal without a logged
cause. The allocator is killed and verified dead in a `finally` before any verdict.

## T3 — checkpoint restart recovery

1. With a fresh park on disk from T2 (or a new one), stop through `stop-prod.sh`
   (state files, never inline patterns), boot through `launch-v0238.sh`.
2. Verify identity + serving (`champions_restore_verify.py`).
3. Continue the parked conversation. PASS: a disk-tier restore serves it — reuse line
   or explicit restore marker, first-prompt wall ≤ 2× the cold 16 k prefill (~70 s
   class) with disk-tier use visible. FAIL: cold re-read with no disk-tier use after a
   clean scan, crash, or `corrupt>0`. A cold re-read WITH a logged reason is UNTESTED
   → recorded, not passed.

## Evidence

`out/parking-validation-<ts>/`: start env snapshot (MemAvailable/swap/RSS), engine-log
excerpts with line numbers per step, request walls + token accounting, 5 s memory
samples, conv-cache listings before/after each step, allocator footprint, verdict table.

Safety: no config change, restarts only via on-disk scripts, pressure capped and
reaped, canonical production verified after the window.

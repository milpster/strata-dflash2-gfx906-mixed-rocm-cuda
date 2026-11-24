# The quality gate for the RAM-pool huge-page knob: teacher-forced numerics unchanged — 2026-11-24

Gate record for the candidate engine carrying `STRATA_POOL_NOHUGEPAGE` (the opt-in `MADV_NOHUGEPAGE` on the 32 GiB
RAM pool mapping). Fixer lane, branch `recovery-startup-watchdog`, GUAM, nothing serving, GPU idle. **No boot and no
GPU work was run here**: every number is read from an artifact or from the command beside it. No push. Fault side:
`journal/pool-nohuge-verification-2026-11-24.md`. Pack `/mnt/srcds/ai/iq3xxs-ud` (UD-IQ3_XXS), DFlash q4_0 drafter,
2x Radeon VII (gfx906), xnack. 4,995 teacher-forced positions = 5 segments x 999.

## 1. The arms

| Arm | Config | Binary (sha256) | Engine | Knob |
|---|---|---|---|---|
| REF | `qq0218F.json` (fork argv) | `run/fsclamp-bin/rpcw-hip/strata` `4b8251e9e06334a8…` | 0.1.57 | absent |
| N0 | `bench/ab-v0210/quality/qq0218N0.json` | `run/port0218-nohuge-bin/rpcw-hip/strata` `15d330ea4d0eec4c…` | 0.1.64 | off |
| N1 | `bench/ab-v0210/quality/qq0218N1.json` | same binary, same sha256 | 0.1.64 | `STRATA_POOL_NOHUGEPAGE=1` |

**Why N0 exists.** The candidate is another build of another tree (0.1.64 against REF's 0.1.57), so a N1-vs-REF delta
could come from the rebuild rather than the knob. N0 is the rebuilt binary with the knob off: N0 vs REF = "did the
rebuild itself change anything", N1 vs N0 = "did the knob change anything". N0 and N1 differ in one thing only (`diff`
of the parsed JSON: `only in N1: {'STRATA_POOL_NOHUGEPAGE': '1'}`). Caveat: N0/N1 argv is **not** REF's argv
(`--vram-reserve-mib` 256/192 against 144/144, another `--conversation-cache-dir`, `--conversation-spill-mib 0`), so
REF->N0 is not one-variable; **N0->N1 is**.

## 2. Teacher-forced numerics

Run in `bench/ab-v0210/quality/`; `$QQ` = `tools/quant_quality.py`. The N arms were driven by hand
(`$QQ run --config …`); `run/qgate-0218port.sh` knows F and P only.

```
python $QQ compare out/qq0218-F.tsv  out/qq0218-N0.tsv
python $QQ compare out/qq0218-N0.tsv out/qq0218-N1.tsv

run            segment   pos    top1       KL   KL p99     PPL PPL ref
qq0218-N0.tsv  all      4995 100.00%  0.00000   0.0000   3.924   3.924
qq0218-N1.tsv  all      4995 100.00%  0.00000   0.0000   3.924   3.924
```

Equal beyond printed precision: all three TSVs share one md5 (`9d9e64ebfaef6577cee09324aa577bfa`, 5,263,820 B each,
`md5sum out/qq0218-{F,N0,N1}.tsv`) — **byte for byte**, not a rounding agreement.

## 3. Prompt mode (first logits, full vocabulary)

20 prompts (4 token lengths x 5 segments), REF vs N1; N0 has no prompt leg. Same figures the 0.2.18 port recorded
(`journal/pool-admission-fix-2026-11-21.md:1293`).

```
python $QQ pcompare out/prompt-0218-F out/prompt-0218-N1
prompt-0218-N1   prompts 20  top-1 100.00%  KL mean 0.00964  median 0.00100  max 0.12926
```

## 4. The knob fired in each arm

The engine's own pool line is the assertion; its absence is the default path.

| Arm | Count | Verdict |
|---|---|---|
| N1 | **1** | `strata: RAM pool: 32768.0 MiB (34359738368 bytes) mapping: transparent huge pages off (MADV_NOHUGEPAGE; STRATA_POOL_NOHUGEPAGE)` |
| N0, REF | **0** | knob off, line absent — the default path prints nothing |
| N1 / F prompts | 1 / 0 | `out/prompt-0218-N1/stderr.txt`, `out/prompt-0218-F/stderr.txt` |

## 5. The bars

Operator bars, `run/qgate-0218port.sh:6`: `dPPL <= 0.5%, KL mean <= 0.02, p99 <= 1.0, top-1 >= 99%`; both N arms meet
all four.

| Bar | Limit | Measured (N0 and N1) | Margin |
|---|---|---|---|
| top-1 | >= 99% | 100.00% | at the ceiling |
| KL mean | <= 0.02 | 0.00000 | at the floor |
| KL p99 | <= 1.0 | 0.0000 | at the floor |
| PPL delta | <= 0.5% | 0.000% (3.924 = 3.924) | at the floor |

Maximum margin everywhere. **The tooling does not assert the bars**: `cmd_compare` and `cmd_pcompare` print the table and
`return 0` unconditionally (`tools/quant_quality.py:362`, `:288`), so a bad arm also exits 0. The verdict is a reading,
not a return code; a loud gate needs a check wrapper.

## 6. What this gate does not show

| Not covered | Note |
|---|---|
| speed | no timing claim; the prefill lines in the stderr are context, not a tg rate |
| the serving tuple at 240k | teacher-forced text and 20 short prompts; max context not exercised |
| the expert helper | the yardstick runs helper-less |
| park/restore, cache spill | not exercised; spill tier off (`--conversation-spill-mib 0`) |

## 7. Provenance was not in git

`qq0218N0.json` and `qq0218N1.json` were untracked until the commit adding this file; `qq0218F.json` and
`qq0218P.json` still are. The gate that justified the 0.2.18 port left its configs and driver outside version control (only
the 20261106/20261107 gates are in git), so its numbers cannot be re-derived from the repo alone. These two arm configs
(~4 KB each) are committed as the provenance; the 5 MB TSVs are not (md5s in §2).

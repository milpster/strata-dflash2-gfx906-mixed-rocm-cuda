#!/usr/bin/env python3
"""S-lane arms: the shortpp-src source changes, on the promoted T4 (CPU_SHARE=auto) base.

Base = armV9.json (iq3s pack, helper relief, dflash spec 5) — identical to the T-lane, so
S0 (flags off) is directly comparable to T4's measured numbers on the same probe.

  S0 = T4-equivalent control (new binary, flags OFF; byte-identical-default check)
  S1 = S0 + STRATA_REFILL_OVERLAP=1   (final refill fills run under the first decode window)
  S2 = S0 + STRATA_WINDOW_POPULATE=1  (window reads promote residency + per-pair arena refill)
  S3 = S0 + both
  S4 = S3 + --short-read 256          (T2c-style window routing WITH both fixes: the poison test)
  SP = S0 + STRATA_PF_STEP_SYNC=1     (intercept profile: layer-experts/KV pf-step decomposition)

M-lane (2026-11-05, the 3-hot-layer residency gap): the memo binary is build-memo/rpcw-hip
(fixer commits 375183eb dedup + b8470d07 STRATA_PROMPT_STAGE_MEMO, default OFF):

  M0 = S0 env on the memo binary, flags OFF   (parity vs S0 + same-binary control)
  M1 = M0 + STRATA_PROMPT_STAGE_MEMO=1        (a dropped prompt pair pack-reads once per request)

M-lane re-bench (2026-11-05 evening, after the fallback fix 14ba81f3): both arms share
STRATA_PREFILL_HELPER_TIMEOUT_MS=120000 so a slow request phase no longer kills the prompt
tier mid-request (recon: 10 s default reply deadline vs 100-240 s slow phases); internally
consistent A/B, footnoted vs session-3's default-timeout numbers:

  M0b = control, flags OFF + the raised timeout
  M1b = M0b + STRATA_PROMPT_STAGE_MEMO=1
"""
import json
import pathlib

B = pathlib.Path("/home/user/dev/Strata/bench/ab-v0210")
BASE = json.loads((B / "armV9.json").read_text())


def arm(name, env=None, args_delta=None):
    c = json.loads(json.dumps(BASE))  # deep copy
    c["env"] = dict(c.get("env", {}))
    c["env"]["STRATA_TRACE"] = "1"
    for k, v in (env or {}).items():
        c["env"][k] = v
    if args_delta:
        a = list(c["args"])
        for flag, val in args_delta:
            if flag in a:
                i = a.index(flag)
                a[i + 1] = val
            else:
                a += [flag, val]
        c["args"] = a
    c["log"] = f"logs/{name}-rROUND-engine.log"
    c["model_name"] = f"shortpp-{name.lower()}"
    out = B / "shortpp" / f"arm{name}.json"
    out.write_text(json.dumps(c, indent=1) + "\n")
    json.loads(out.read_text())
    print("wrote", out)


if __name__ == "__main__":
    (B / "shortpp").mkdir(exist_ok=True)
    arm("S0", env={"STRATA_PREFILL_CPU_SHARE": "auto"})
    arm("S1", env={"STRATA_PREFILL_CPU_SHARE": "auto", "STRATA_REFILL_OVERLAP": "1"})
    arm("S2", env={"STRATA_PREFILL_CPU_SHARE": "auto", "STRATA_WINDOW_POPULATE": "1"})
    arm("S3", env={"STRATA_PREFILL_CPU_SHARE": "auto", "STRATA_REFILL_OVERLAP": "1",
                   "STRATA_WINDOW_POPULATE": "1"})
    arm("S4", env={"STRATA_PREFILL_CPU_SHARE": "auto", "STRATA_REFILL_OVERLAP": "1",
                   "STRATA_WINDOW_POPULATE": "1"},
        args_delta=[("--short-read", "256")])
    arm("SP", env={"STRATA_PREFILL_CPU_SHARE": "auto", "STRATA_PF_STEP_SYNC": "1"})
    arm("M0", env={"STRATA_PREFILL_CPU_SHARE": "auto"})
    arm("M1", env={"STRATA_PREFILL_CPU_SHARE": "auto", "STRATA_PROMPT_STAGE_MEMO": "1"})
    arm("M0b", env={"STRATA_PREFILL_CPU_SHARE": "auto", "STRATA_PREFILL_HELPER_TIMEOUT_MS": "120000"})
    arm("M1b", env={"STRATA_PREFILL_CPU_SHARE": "auto", "STRATA_PREFILL_HELPER_TIMEOUT_MS": "120000",
                    "STRATA_PROMPT_STAGE_MEMO": "1"})

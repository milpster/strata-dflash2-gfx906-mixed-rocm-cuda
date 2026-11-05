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

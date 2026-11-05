#!/usr/bin/env python3
"""Short-prompt PP / TG<->PP turn-around arms (the T-lane).

Base = armV9.json (the live prod lineage: iq3s pack, helper relief, dflash spec 5).
Every arm carries STRATA_TRACE=1 so trace overhead is CONSTANT across the A/B and
the request-stage/admission lines are present for decomposition.

  T1  = baseline (V9 + trace)                - decomposition target
  T2a = T1 + --short-read 16                 - smaller window tail
  T2b = T1 + --short-read 128                - bigger window tail
  T3  = T1 + STRATA_PREFILL_HELPER_PIPE=1    - M6 async claim lane (socket2 off the serial channel)
  T4  = T1 + STRATA_PREFILL_CPU_SHARE=auto   - CPU share for short chunks (off on 2-GPU HIP by default)
  T5  = T1 + STRATA_SPLIT_SMALL_OWN=1024 _MAX=4096 - small requests read in S-token chunks, nothing lent
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
            if flag in a:  # replace existing value (short-read is absent by default, but be safe)
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
    arm("T1")
    arm("T2a", args_delta=[("--short-read", "16")])
    arm("T2b", args_delta=[("--short-read", "128")])
    arm("T3", env={"STRATA_PREFILL_HELPER_PIPE": "1"})
    arm("T4", env={"STRATA_PREFILL_CPU_SHARE": "auto"})
    arm("T5", env={"STRATA_SPLIT_SMALL_OWN": "1024", "STRATA_SPLIT_SMALL_MAX": "4096"})
    # added after the T1 decomposition:
    #   T1b = T1 + STRATA_RPC_PROFILE=1  - splits the ~10s short batched read (per-layer d2h/send vs compute)
    #   T2c = T1 + --short-read 256       - chat-sized prompts (~185 tok) read fully through the fast window
    #                                      path (T1 measured: 55-tok windows read 0.98s vs 185-tok batched 5.6-7.9s);
    #                                      T2a(16)/T2b(128) dropped: the windows tail is 4 tok / ~55ms, and 16
    #                                      would push MORE tokens into the slow batched path.
    arm("T1b", env={"STRATA_RPC_PROFILE": "1"})
    arm("T2c", args_delta=[("--short-read", "256")])

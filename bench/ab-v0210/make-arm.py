#!/usr/bin/env python3
"""Generate the F/K window arm configs from armB.json (TAP_OVERLAP_DESIGN section 4).

F1off/F1/F2 run the prototype build (tap-overlap-f1f2); F0/K1/K2 run the
pre-prototype build so the probe and knob arms carry no prototype code.
Deltas only - everything else is arm B verbatim.  Reusable: future arms add
one entry to ARMS.  Note: corpus.py PROBE_PROMPT was fixed (bc8c30ce) AFTER
the A-E rounds, so probe shas of F/K arms do not compare with A-E records;
the 80k infill corpus generator is untouched and its sha gate stands.
"""
import copy
import json
import os
import sys

B = os.path.dirname(os.path.abspath(__file__))
BASE = json.load(open(f"{B}/armB.json"))
OLD = "/home/user/dev/Strata/build-v0210/rpcw-hip/strata"
NEW = "/home/user/dev/Strata/build-tap-overlap/rpcw-hip/strata"


def arm(name, exe, env=None, layer_split=None, prefill=None):
    cfg = copy.deepcopy(BASE)
    cfg["exe"] = exe
    if env:
        cfg["env"].update(env)
    if layer_split:
        cfg["layer_split"] = layer_split
    if prefill:
        i = cfg["args"].index("--prefill")
        cfg["args"][i + 1] = prefill
    cfg["log"] = f"logs/{name}-rROUND-engine.log"
    return cfg


ARMS = {
    "F0": dict(exe=OLD, env={"STRATA_DF_EVENTS": "1"}),
    "F1off": dict(exe=NEW),
    "F1": dict(exe=NEW, env={"STRATA_DF_DEFER_CTX": "1"}),
    "F2": dict(exe=NEW, env={"STRATA_DF_DEFER_CTX": "1", "STRATA_DF_ASYNC_D2H": "1"}),
    "K1": dict(exe=OLD, layer_split="16"),
    "K2": dict(exe=OLD, prefill="16384"),
}

if __name__ == "__main__":
    names = sys.argv[1:] or list(ARMS)
    for n in names:
        json.dump(arm(n, **ARMS[n]), open(f"{B}/arm{n}.json", "w"), indent=1)
        print(f"arm{n}.json written")

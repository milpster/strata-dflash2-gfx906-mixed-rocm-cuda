#!/usr/bin/env python3
"""Restore the ud-iq3_xxs prod config to the champion RELIEF shape.

Idempotent: args come verbatim from commit ff42ca273a (relief args + XXS paths,
the shape that served stably all week), env from the same commit minus
STRATA_ARENA_MLOCK / STRATA_ARENA_PIN_GIB, and the engine log field moves to a
dedicated XXS elog so judge lines stop mixing with the IQ3_S log.
"""
import json
import subprocess

P = "/mnt/srcds/Strata/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json"
REL = "prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json"
BASE = "a9eac52642"  # last commit with the champion relief args (helper pack fix)

old = json.loads(subprocess.check_output(["git", "-C", "/mnt/srcds/Strata", "show", f"{BASE}:{REL}"]))
cur = json.load(open(P))

cur["args"] = old["args"]
env = dict(old.get("env", {}))
env.pop("STRATA_ARENA_MLOCK", None)
env.pop("STRATA_ARENA_PIN_GIB", None)
cur["env"] = env
cur["log"] = "/mnt/srcds/Strata/logs/engine-prod-ud-iq3xxs-v0233.log"
json.dump(cur, open(P, "w"), indent=2)

a = cur["args"]
print(
    "relief args:", [f for f in a if "relief" in f],
    "| reserves:", [a[i + 1] for i, f in enumerate(a) if f in ("--vram-reserve-mib", "--vram-reserve-later-mib")],
    "| MLOCK:", env.get("STRATA_ARENA_MLOCK", "absent"),
    "| log:", cur["log"],
)

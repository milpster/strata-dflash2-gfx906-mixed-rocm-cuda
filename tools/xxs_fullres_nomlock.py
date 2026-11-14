#!/usr/bin/env python3
"""Pivot the ud-iq3_xxs prod config to full residency WITHOUT mlock (kernel 7.3.1 discriminator).

Idempotent: drops the four relief args, sets both vram reserves to 2304,
removes STRATA_ARENA_MLOCK and STRATA_ARENA_PIN_GIB from env. Safe to re-run.
"""
import json

P = "/mnt/srcds/Strata/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json"
RELIEF = {
    "--expert-helper-ram-relief",
    "--expert-helper-ram-relief-packs",
    "--expert-helper-relief-hot-mib",
    "--expert-helper-relief-arena-cap-mib",
}

c = json.load(open(P))
a, out = c["args"], []
i = 0
while i < len(a):
    if a[i] in RELIEF:
        i += 2 if i + 1 < len(a) and not a[i + 1].startswith("--") else 1
    else:
        out.append(a[i])
        i += 1
for j, f in enumerate(out):
    if f in ("--vram-reserve-mib", "--vram-reserve-later-mib"):
        out[j + 1] = "2304"
c["args"] = out
env = c.get("env", {})
env.pop("STRATA_ARENA_MLOCK", None)
env.pop("STRATA_ARENA_PIN_GIB", None)
c["env"] = env
json.dump(c, open(P, "w"), indent=2)
print(
    "args:", len(out),
    "| relief remnants:", [f for f in out if f in RELIEF] or "none",
    "| MLOCK:", env.get("STRATA_ARENA_MLOCK", "absent"),
    "| PIN_GIB:", env.get("STRATA_ARENA_PIN_GIB", "absent"),
    "| reserves:", [out[j + 1] for j, f in enumerate(out) if f in ("--vram-reserve-mib", "--vram-reserve-later-mib")],
)

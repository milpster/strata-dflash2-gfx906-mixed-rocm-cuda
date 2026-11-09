#!/usr/bin/env python3
"""Stage-2 arm generator: derive arm-m1..m4.json from the CURRENT live prod config.

Every arm is ONE lever against the live file (SPEC-stage2 §Prepared). Nothing is hardcoded
from a snapshot: the exe, sockets, log path, model/port/cwd and every untouched key come from
the live config, so the arms re-derive correctly after any prod config change. Re-run this
then `validate-arms.py` after the live config moves (the v0211-era arms were exactly such a
stale snapshot: exe + --conversation-cache-mib had already moved under them).

Levers (SPEC-stage2 1..3 + the layer-split shift):
  m1  VII#1 (CUDA1) VRAM headroom: expert-helper slots 4600 -> 4400 (CUDA1 owns 4407 slots and
      lends 3608 to the prompt path; the ask exceeds its own slot count) AND both vram reserves
      256 -> 512, so free VRAM stays >= 512 MiB (measured 504 MiB with everything loaded,
      404 MiB at request start, engine already warns 'below 512 MiB').
  m2  --kv fp16 -> int8 only (halves resident KV at 262144-resident; AB8 proved speed parity).
  m3  helper-side relief depth on the 3080: --fill-mib 6796 -> 7200 + --reserve-mib 300 -> 128.
      Helper-args lever: ZERO engine-config keys change (the numbers reach the boot through
      _ab9_helper -> ab9_mkconfig.py -> the helper command line).
  m4  layer_split 32 -> 36 (shifts the resident split off the full VII#1).

The ud socket-name defect is the rule this generator obeys: a value the launcher also carries
(sockets, helper slots/fill/reserve) is written from the live config, never re-invented, so the
launcher strings and the config keys stay byte-identical.
"""
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PROD = ROOT / "prod-config-swift15-f16kv-dflash-w4-q4_0-mixed.json"

# lever = (doc string, {top-level key: new value}, {args flag: new value}, {helper arg: new value})
LEVERS = {
    "m1": {
        "diff": (
            "VII#1/CUDA1 headroom: ONE lever = expert-helper slots 4600->4400 (args pair AND the "
            "top-level expert_helper_slots mirror, kept in sync per the ud socket-name precedent) "
            "+ --vram-reserve-mib/--vram-reserve-later-mib 256->512, so free VRAM stays >=512 MiB "
            "(measured 504 MiB with everything loaded, 404 MiB at request start)"
        ),
        "top": {"expert_helper_slots": 4400},
        "args": {"--expert-helper-slots": "4400", "--vram-reserve-mib": "512",
                 "--vram-reserve-later-mib": "512"},
    },
    "m2": {
        "diff": "KV dtype: ONE key --kv fp16 -> int8 (halves resident KV at 262144-resident; AB8: no speed delta)",
        "top": {},
        "args": {"--kv": "int8"},
    },
    "m3": {
        "diff": (
            "Deeper relief on the 3080: ZERO engine-config keys; helper-side --fill-mib 6796->7200 "
            "+ --reserve-mib 300->128 (SPEC-stage2 lever 2: the card fills after context+buffers, "
            "gain <=~0.4 GiB, one lever with its two coupled numbers)"
        ),
        "top": {},
        "args": {},
        "helper": {"fill_mib": 7200, "reserve_mib": 128},
    },
    "m4": {
        "diff": "Resident split: ONE key layer_split 32 -> 36 (shifts layers off the full VII#1)",
        "top": {"layer_split": "36"},
        "args": {},
    },
}


def set_arg(args: list[str], flag: str, value: str) -> None:
    i = args.index(flag)
    args[i + 1] = value


def main() -> int:
    prod = json.loads(PROD.read_text())
    for arm, lever in LEVERS.items():
        cfg = json.loads(PROD.read_text())  # deep copy by round-trip: arms stay byte-derived
        for key, value in lever["top"].items():
            cfg[key] = value
        for flag, value in lever["args"].items():
            set_arg(cfg["args"], flag, value)
        cfg = {"_ab9_diff": lever["diff"], **cfg}
        if "helper" in lever:
            cfg = {"_ab9_helper": dict(lever["helper"]), **cfg}
        out = HERE / f"arm-{arm}.json"
        out.write_text(json.dumps(cfg, indent=1, sort_keys=True) + "\n")
        print(f"ab9_mkarms: {out.name} from {PROD.name} keys="
              f"{sorted(set(lever['top']) | set(lever['args']))} "
              f"helper={lever.get('helper', {})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

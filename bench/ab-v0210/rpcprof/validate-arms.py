#!/usr/bin/env python3
"""ab9 Stage-2 arm validator: arm-m1..m4.json must be single-lever diffs of the LIVE config.

Checks per arm (SPEC-stage2 §Prepared + the ud socket-name defect precedent):
  (a) the diff against the live prod config is EXACTLY the declared lever (printed key by key);
  (b) socket pairs equal the launcher's own strings, and the helper `--slots` the harness will
      boot equals the arm's expert_helper_slots (engine ask == helper capacity, no split-brain);
  (c) every absolute path the arm references exists on disk;
  (d) the exe is the live exe (arms never move the binary).
PASS/FAIL per arm; exit non-zero if any arm fails. Read-only: prod config and launcher are
never written, no process is touched.

Usage: validate-arms.py [--verbose]
"""
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PROD = ROOT / "prod-config-swift15-f16kv-dflash-w4-q4_0-mixed.json"
LAUNCHER = ROOT / "launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh"
ARMS = ["m1", "m2", "m3", "m4"]

# One lever per arm = the exact key set allowed to differ (args flags are keyed 'args:--flag').
LEVERS = {
    "m1": ("VII#1/CUDA1 slots + vram-reserve trim (>=512 MiB free)",
           {"expert_helper_slots", "args:--expert-helper-slots", "args:--vram-reserve-mib",
            "args:--vram-reserve-later-mib"}),
    "m2": ("--kv int8 only", {"args:--kv"}),
    "m3": ("helper fill 7200 + reserve 128 (helper args, zero config keys)", set()),
    "m4": ("layer_split 36", {"layer_split"}),
}
# m3 changes the helper command line only; its declared numbers:
M3_HELPER = {"fill_mib": 7200, "reserve_mib": 128}


def flat(cfg: dict) -> dict:
    """Config as a comparable key->value map: top-level scalars + each args flag."""
    out = {k: v for k, v in cfg.items() if k != "args" and not k.startswith("_ab9")}
    args = cfg["args"]
    i = 0
    while i < len(args):
        flag = args[i]
        value = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else True
        out[f"args:{flag}"] = value
        i += 1 if value is True else 2
    return out


def grab(pattern: str, text: str) -> re.Match:
    """A launcher shape change must be a loud validator failure, not an AttributeError."""
    m = re.search(pattern, text)
    if not m:
        raise SystemExit(f"validate-arms: launcher pattern not found: {pattern}")
    return m


def launcher_strings() -> dict:
    """The launcher's own socket strings and helper args — the pair the config must match."""
    text = LAUNCHER.read_text()
    root = grab(r'ROOT=\$\{STRATA_PROD_TREE:-([^}]*)\}', text).group(1)
    sock = grab(r'SOCKET="\$ROOT/([^"]+)"', text).group(1)
    sock2 = grab(r'SOCKET2="\$ROOT/([^"]+)"', text).group(1)
    slots = grab(r'--slots (\d+)', text).group(1)
    reserve = grab(r'--reserve-mib (\d+)', text).group(1)
    fill = grab(r'--fill-mib (\d+)', text).group(1)
    return {"root": root, "socket": f"{root}/{sock}", "socket2": f"{root}/{sock2}",
            "slots": int(slots), "reserve_mib": int(reserve), "fill_mib": int(fill)}


def paths(cfg: dict) -> list[str]:
    """Absolute paths the arm references; sockets are checked as parent dirs (they are unlinked
    at stop, created at boot)."""
    out = [v for v in cfg["args"] if isinstance(v, str) and v.startswith("/")]
    out += [cfg["exe"], cfg["cwd"], cfg["tokenizer"], cfg["log"].rsplit("/", 1)[0]]
    out += [cfg["expert_helper_socket"].rsplit("/", 1)[0], cfg["expert_helper_socket2"].rsplit("/", 1)[0]]
    out += list(cfg["lib_dirs"])
    out += [cfg["vision"][k] for k in ("exe", "mmproj", "model")]
    return out


def main() -> int:
    verbose = "--verbose" in sys.argv
    prod = json.loads(PROD.read_text())
    prod_flat = flat(prod)
    L = launcher_strings()
    failures = 0
    for arm in ARMS:
        path = HERE / f"arm-{arm}.json"
        problems: list[str] = []
        if not path.exists():
            print(f"arm-{arm}: FAIL missing {path}")
            failures += 1
            continue
        cfg = json.loads(path.read_text())
        label, declared = LEVERS[arm]

        # (a) exactly the declared lever differs
        arm_flat = flat(cfg)
        changed = {k for k in set(arm_flat) | set(prod_flat) if arm_flat.get(k, "∅") != prod_flat.get(k, "∅")}
        print(f"arm-{arm}  lever: {label}")
        for k in sorted(changed):
            print(f"    {k}: {prod_flat.get(k, '∅')} -> {arm_flat.get(k, '∅')}")
        if changed != declared:
            problems.append(f"(a) diff {sorted(changed)} != declared lever {sorted(declared)}")
        if not cfg.get("_ab9_diff", "").strip():
            problems.append("(a) missing _ab9_diff comment field")

        # (b) launcher <-> config pair
        for key, want in (("expert_helper_socket", L["socket"]), ("expert_helper_socket2", L["socket2"])):
            if cfg[key] != want:
                problems.append(f"(b) {key}={cfg[key]} != launcher {want}")
        if prod["expert_helper_slots"] != L["slots"]:
            problems.append(f"(b) live config slots {prod['expert_helper_slots']} != launcher {L['slots']}")
        helper = cfg.get("_ab9_helper", {})
        if int(helper.get("slots", cfg["expert_helper_slots"])) != int(cfg["expert_helper_slots"]):
            problems.append("(b) helper --slots != expert_helper_slots (engine ask vs helper capacity)")
        if arm == "m3" and {k: int(v) for k, v in helper.items()} != M3_HELPER:
            problems.append(f"(b) m3 helper block {helper} != {M3_HELPER}")
        if arm != "m3" and helper:
            problems.append("(b) non-helper arm carries a _ab9_helper block")

        # (c) referenced paths exist
        missing = [p for p in paths(cfg) if not pathlib.Path(p).exists()]
        if missing:
            problems.append(f"(c) missing paths: {missing}")

        # (d) the exe is the live exe
        if cfg["exe"] != prod["exe"]:
            problems.append(f"(d) exe {cfg['exe']} != live {prod['exe']}")
        if cfg["log"] != prod["log"] or cfg["port"] != prod["port"]:
            problems.append("(d) log/port names not preserved")

        if problems:
            failures += 1
            print(f"  FAIL arm-{arm}: " + "; ".join(problems))
        else:
            print(f"  PASS arm-{arm}: {len(changed)} key(s) changed = one lever, "
                  f"sockets/slots match launcher ({L['socket'].split('/')[-1]}+2), "
                  f"{len(paths(cfg))} paths exist, exe is live")
        if verbose:
            print(f"    exe={cfg['exe']} log={cfg['log']} slots={cfg['expert_helper_slots']} "
                  f"fill={helper.get('fill_mib', L['fill_mib'])} reserve={helper.get('reserve_mib', L['reserve_mib'])}")

    print(f"validate-arms: {'PASS all 4' if not failures else f'FAIL {failures} arm(s)'} "
          f"(live exe {prod['exe'].split('/Strata/')[-1]}, slots {prod['expert_helper_slots']})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

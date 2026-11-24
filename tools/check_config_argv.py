#!/usr/bin/env python3
"""Statically validate a config's args against an engine binary, WITHOUT running it.

1. Every token starting with '--' must appear as a literal in the binary's strings
   (the engine refuses unknown args: generate.cpp "unknown argument").
2. Re-evaluate the relief dependency validators from the source rules against the
   parsed option values, and report each check pass/fail.

No GPU, no boot, no process started: reads the binary's string table and the JSON.
"""
import json
import subprocess
import sys


def flags_in_args(args):
    """Return the set of --flags and count value tokens."""
    return {t for t in args if t.startswith("--")}


def binary_literals(exe):
    out = subprocess.run(["strings", "-n", "4", exe], capture_output=True, text=True, check=True).stdout
    return set(out.splitlines())


def main():
    cfg_path, exe = sys.argv[1], sys.argv[2]
    cfg = json.load(open(cfg_path))
    args = cfg["args"]
    lits = binary_literals(exe)

    used = flags_in_args(args)
    missing = sorted(f for f in used if f not in lits)
    print(f"config: {cfg_path}")
    print(f"exe:    {exe}")
    print(f"args tokens: {len(args)}, distinct flags: {len(used)}")
    if missing:
        print("UNRECOGNISED FLAGS (engine would exit 2 'unknown argument'):")
        for f in missing:
            print(f"  {f}")
    else:
        print("unrecognised flags: NONE (every --flag is a literal in the binary)")

    # ---- relief dependency validators (generate.cpp:5873..5915) ----
    def has(flag):
        return flag in used

    def val(flag, default):
        if flag not in args:
            return default
        return int(args[args.index(flag) + 1])

    relief = has("--expert-helper-ram-relief")
    packs = has("--expert-helper-ram-relief-packs")
    hot = val("--expert-helper-relief-hot-mib", 7000)
    cap = val("--expert-helper-relief-arena-cap-mib", 0)
    dec = val("--expert-helper-relief-arena-cap-decode-mib", 0)
    rer = has("--expert-helper-relief-arena-cap-rerank")
    sock = cfg.get("expert_helper_socket", "")

    checks = [
        ("5873 relief needs --expert-helper-socket", not (relief and not sock),
         f"relief={relief} socket={'set' if sock else 'EMPTY'}"),
        ("5882 packs needs relief", not (packs and not relief), f"packs={packs} relief={relief}"),
        ("5889 hot-mib!=7000 needs packs", not (hot != 7000 and not packs), f"hot={hot} packs={packs}"),
        ("5897 cap>0 needs relief", not (cap > 0 and not relief), f"cap={cap} relief={relief}"),
        ("5903 decode>0 needs cap>0", not (dec > 0 and cap == 0), f"decode={dec} cap={cap}"),
        ("5910 rerank needs cap>0", not (rer and cap == 0), f"rerank={rer} cap={cap}"),
    ]
    print("\nrelief dependency validators:")
    ok = not missing
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}   ({detail})")

    print(f"\nVERDICT: {'ACCEPTED' if ok else 'REFUSED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

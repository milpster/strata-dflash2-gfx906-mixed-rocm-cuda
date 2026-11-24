#!/usr/bin/env python3
"""port_ab_configs.py - derive the two Phase-2 arm configs from ONE frozen tuple.

Why this exists: the Phase-2 contract (plan/measurement-contract-speed-attribution-faults.md)
compares A (fork engine 4b8251e9 + helper 9d16742d + the v0215 serve tree) with B (promoted
port engine 936efcc7 + helper 58c3ae02 + run/port0218-server). The two arms must differ in
exactly the things the harness owns - exe, cwd, log, sockets and the per-leg cache dir - and
in nothing else, or a delta cannot be attributed to the engine+server pair.

How the configs are built (the knob-leg.sh / champions_prepare.py pattern): each arm starts
from its ADOPTED base config (A: prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed-fson.json,
B: prod-config-port0218.json). Only the frozen tuple and the harness paths are written;
every other key stays exactly as adopted. Every change is recorded as a delta.

Structural difference that is NOT a lever: the fork engine has no --conversation-spill-mib
option, so arm A's args carry no spill flag (spill tier structurally absent) and arm B's
carry --conversation-spill-mib 0 (tier present, off). Both mean "no spill".

CPU-only: reads JSON bases, writes JSON. No engine, no helper, no process control, no GPU.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys

ROOT = pathlib.Path("/mnt/srcds/Strata")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import port_ab_contract as C
HERE = pathlib.Path(__file__).resolve().parent
OUT_DEFAULT = HERE / "out" / "port-ab-20261123"

# Frozen arm identities. The shas here are the boot-line shas the contract names;
# port_ab_prepare.py re-hashes the binaries on disk and refuses to run if they drifted.
ARMS = {
    "A": {
        "label": "fork engine + v0215 server",
        "engine": "run/fsclamp-bin/rpcw-hip/strata",
        "engine_sha256": "4b8251e9e06334a8e2dc8ab78453449a4820dccd1a4d13efe4faf19100233d41",
        "helper": "run/fsclamp-bin/rpcw-cuda/strata-expert-helper-patient",
        "helper_sha256": "9d16742d5f30e9c199989f27643ee434e7aec66407f02e5cec36355109ba57b2",
        "server_tree": "v0215",
        "base_config": "prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed-fson.json",
        # the fork engine's parser has no --conversation-spill-mib (verified statically by
        # port_ab_preflight.py: the literal is absent from the binary)
        "spill_flag": False,
    },
    "B": {
        # 2026-11-24 operator directive: arm B re-pinned from 936efcc7 (the 0.2.18 port) to
        # 15d330ea (the same port + MADV_NOHUGEPAGE on the 32 GiB RAM pool, the promoted prod
        # binary). Measuring the old pin would answer a question nothing serves. W1's existing
        # records stay as the OLD-B baseline and are not re-derived; every window from this
        # amendment on measures what prod actually runs.
        "label": "promoted port + pool MADV_NOHUGEPAGE + port server",
        "engine": "run/port0218-nohuge-bin/rpcw-hip/strata",
        "engine_sha256": "15d330ea4d0eec4ca64ef3973e52d332253a981c25dde42b70567bf8b3ae7577",
        "helper": "run/port0218-nohuge-bin/rpcw-cuda/strata-expert-helper",
        "helper_sha256": "58c3ae020a7479a13c622627544507f5b43f284f9be4b5222ac90c3421b88bb2",
        "server_tree": "run/port0218-server",
        "base_config": "prod-config-port0218-nohuge.json",
        "spill_flag": True,
        "pool_nohuge": True,   # REQUIRED in every B config: the binary runs the fix only when set
    },
}

# The frozen tuple: every value here is written into BOTH configs (or into both helper
# command lines). Changing a value here is changing the measurement contract.
TUPLE = {
    "vram_reserve_mib": 256,
    "vram_reserve_later_mib": 192,
    "ram_budget_mib": 36864,
    "helper_slots": 4600,
    "helper_fill_mib": 7000,      # helper argv (not a config key): caps both arms' fill
    "helper_reserve_mib": 0,      # helper argv; inert while fill-mib 7000 binds first
    "conversation_spill_mib": 0,  # arm B flag value; arm A: flag structurally absent
    "spec": 5,
    "dflash_block": 7,
    "layer_split": 30,
    "expert_helper_timeout": 10000,
    # Matched serving identity. model_name is read by the servers' drafting chooser: neither
    # "ud-iq3_xxs" nor the swift name matches a drafter regex in either tree, so
    # drafting.apply is a no-op and the engine receives the config's args verbatim.
    "model_name": "ud-iq3_xxs",
    # 0 = no server-side thinking budget. A's base served 55000 and B's 2000; that is a
    # generation-shape difference, not a speed lever, so it is matched to the neutral value
    # and the request template pins enable_thinking=false (the pilot asserts reasoning=0).
    "reasoning_budget_tokens": 0,
    "reasoning_effort": "high",
    "port": 8009,                 # unused by serve/server.py; the launcher's --port wins
}

# Keys the harness owns per arm. Blanked by settings_only() so arms compare on settings.
HARNESS_FIELDS = ("exe", "cwd", "log", "expert_helper_socket", "expert_helper_socket2",
                  "port", "trial_note")
HARNESS_ARGS = ("--conversation-cache-dir",)
PLACEHOLDER = "<harness>"

# Base-config keys that are provenance notes, not settings (and one with no consumer).
DROP_KEYS = ("trial_note", "derived_from", "derived_note", "managed_budget_mib", "_port_ab")

# Engine args that name the helper channels: stripped from the base and re-added per leg.
HELPER_SOCKET_ARGS = ("--expert-helper-socket", "--expert-helper-socket2")


def strip_args(args, flags):
    """Remove each flag in flags and its value (pair-wise) from an argv list."""
    out, i, flags = [], 0, set(flags)
    while i < len(args):
        if args[i] in flags:
            i += 2 if (i + 1 < len(args) and not str(args[i + 1]).startswith("--")) else 1
            continue
        out.append(args[i])
        i += 1
    return out


def arg_value(args, flag):
    return str(args[args.index(flag) + 1]) if flag in args else None


def set_arg(args, flag, value, *, allow_add, deltas, who):
    """Set one engine argument in place, or append it when the base does not carry it.

    A flag this function may not add (allow_add false and absent) is a harness bug, not a
    warning: inserting an unknown flag would silently change the engine command line.
    """
    want = str(value)
    if flag in args:
        have = args[args.index(flag) + 1]
        if str(have) != want:
            deltas.append({"field": "args/" + flag, "from": have, "to": want, "kind": who})
        args[args.index(flag) + 1] = want
        return args
    if not allow_add:
        raise SystemExit(flag + " is not in this base config and this arm may not invent it")
    args += [flag, want]
    deltas.append({"field": "args/" + flag, "from": "<absent>", "to": want, "kind": who})
    return args


def ensure_absent(args, flag, deltas, who):
    if flag in args:
        deltas.append({"field": "args/" + flag, "from": arg_value(args, flag), "to": "<absent>",
                       "kind": who})
    return strip_args(args, (flag,))


def default_boot(window):
    """The boot group a window's single-shape legs use."""
    return next(iter(C.WINDOWS[window]["boot_groups"]))


def boot_for_shape(window, shape):
    """Which boot group of the window carries this shape (W2 has two: dec and sht)."""
    for name, shapes in C.WINDOWS[window]["boot_groups"].items():
        if shape in shapes:
            return name
    raise SystemExit(shape + " is not a leg of " + window)


def boot_key(window, arm, rep, boot=None):
    """The boot's identity. The boot group is ALWAYS part of the key.

    W2 has two boot shapes; without the group, W2-A-r1 would name both the decode boot and
    the short boot, and the second boot would either refuse (its cache dir is not empty) or
    silently reuse the first one's sockets, cache and logs.
    """
    return window + "-" + arm + "-" + (boot or default_boot(window)) + "-r" + str(rep)


def leg_paths(out, window, arm, rep, boot=None):
    """Every harness-owned path of one boot (one fresh cache dir per boot)."""
    key = boot_key(window, arm, rep, boot)
    return {
        "key": key,
        "engine_log": str(out / "logs" / (key + ".engine.log")),
        "helper_log": str(out / "logs" / (key + "-helper.log")),
        "boot_log": str(out / "logs" / (key + "-boot.log")),
        "socket": str(out / "run" / ("pa-" + key + ".sock")),
        "socket2": str(out / "run" / ("pa-" + key + "-2.sock")),
        "cache_dir": str(out / "cache" / key),
        "config": str(out / "configs" / (key + ".json")),
        "records": str(out / "records" / key),
    }


def derive(arm, *, window, rep, out, port=None, base_override=None, boot=None):
    """The config for one leg of one arm, the deltas that produced it, and its paths.

    The engine arm decides the structural flags (spill) and the base config; the server tree
    is chosen by the launcher, never by this file, so a 2x2 cell uses its engine arm's config
    unchanged and only the server process differs.
    """
    if arm not in ARMS:
        raise SystemExit("unknown arm " + repr(arm) + ": choose A or B")
    spec = ARMS[arm]
    base_path = ROOT / (base_override or spec["base_config"])
    cfg = json.loads(base_path.read_text())
    deltas = []
    args = list(cfg["args"])

    # 1. provenance / unmatched keys leave, so the arms compare on settings only
    for key in DROP_KEYS:
        if key in cfg:
            deltas.append({"field": key, "from": cfg.pop(key), "to": "<absent>", "kind": "harness"})
    for key in ("model_name", "reasoning_budget_tokens", "reasoning_effort"):
        old = cfg.get(key)
        if old != TUPLE[key]:
            deltas.append({"field": key, "from": old, "to": TUPLE[key], "kind": "tuple"})
        cfg[key] = TUPLE[key]

    # 2. helper channels: stripped from the base, re-added per leg (strip-then-repoint)
    args = strip_args(args, HELPER_SOCKET_ARGS)
    for key in ("expert_helper_socket", "expert_helper_socket2"):
        cfg.pop(key, None)

    # 3. the frozen tuple in the engine arguments. The VRAM reserves follow the window's
    #    margins mode: every window except W1d writes the frozen 256/192 from TUPLE, while an
    #    engine-default window (W1d, operator directive 2026-11-23 23:00) must carry NEITHER
    #    flag, so both are REMOVED here and the removal is recorded as an operator-directive
    #    delta. What that changes on this rig (measured 2026-11-23 on the live faultdiff leg):
    #    the engine reserves 700 MiB per card instead of 256/192, card0's expert cache comes up
    #    at 4,116 slots instead of 5,739, post-load free VRAM is 728/648 MiB, and the Foresight
    #    swap grant flips from 0 slots to card0:2/card1:8 - so such a boot is NOT "W1 minus the
    #    reserves"; see port_ab_contract.WINDOWS["W1d"].
    if C.margins_mode(window) == "engine-default":
        args = ensure_absent(args, "--vram-reserve-mib", deltas, "operator-directive")
        args = ensure_absent(args, "--vram-reserve-later-mib", deltas, "operator-directive")
    else:
        args = set_arg(args, "--vram-reserve-mib", TUPLE["vram_reserve_mib"], allow_add=True,
                       deltas=deltas, who="tuple")
        args = set_arg(args, "--vram-reserve-later-mib", TUPLE["vram_reserve_later_mib"],
                       allow_add=True, deltas=deltas, who="tuple")
    args = set_arg(args, "--spec", TUPLE["spec"], allow_add=True, deltas=deltas, who="tuple")
    args = set_arg(args, "--dflash-block", TUPLE["dflash_block"], allow_add=True, deltas=deltas,
                   who="tuple")
    args = set_arg(args, "--expert-helper-slots", TUPLE["helper_slots"], allow_add=True,
                   deltas=deltas, who="tuple")
    if spec["spill_flag"]:
        args = set_arg(args, "--conversation-spill-mib", TUPLE["conversation_spill_mib"],
                       allow_add=True, deltas=deltas, who="tuple")
    else:
        args = ensure_absent(args, "--conversation-spill-mib", deltas, "tuple")

    # The layer split is a config key; the server appends --layer-split from it, identically
    # in both trees. A stale copy inside args would make it appear twice.
    args = ensure_absent(args, "--layer-split", deltas, "harness")
    args = ensure_absent(args, "--split-skip-if-fits", deltas, "harness")
    if cfg.get("layer_split") != TUPLE["layer_split"]:
        deltas.append({"field": "layer_split", "from": cfg.get("layer_split"),
                       "to": TUPLE["layer_split"], "kind": "tuple"})
    cfg["layer_split"] = TUPLE["layer_split"]

    # 4. env: the RAM budget is frozen; every other env key is the adopted one and must be
    #    identical across arms (port_ab_prepare.py fails closed on any difference).
    env = dict(cfg.get("env") or {})
    if env.get("STRATA_RAM_BUDGET_MIB") != str(TUPLE["ram_budget_mib"]):
        deltas.append({"field": "env/STRATA_RAM_BUDGET_MIB",
                       "from": env.get("STRATA_RAM_BUDGET_MIB"),
                       "to": str(TUPLE["ram_budget_mib"]), "kind": "tuple"})
    env["STRATA_RAM_BUDGET_MIB"] = str(TUPLE["ram_budget_mib"])
    cfg["env"] = env

    # 5. harness-owned paths (the boot group is part of the key: W2-A-dec-r1 / W2-A-sht-r1)
    paths = leg_paths(out, window, arm, rep, boot)
    retarget = {
        "exe": str(ROOT / spec["engine"]),
        "cwd": str(ROOT / spec["server_tree"]),
        "log": paths["engine_log"],
        "port": int(port if port is not None else TUPLE["port"]),
    }
    for key, value in retarget.items():
        if cfg.get(key) != value:
            deltas.append({"field": key, "from": cfg.get(key), "to": value, "kind": "harness"})
        cfg[key] = value
    cfg["expert_helper_socket"] = paths["socket"]
    cfg["expert_helper_socket2"] = paths["socket2"]
    cfg["expert_helper_slots"] = TUPLE["helper_slots"]
    cfg["expert_helper_timeout"] = TUPLE["expert_helper_timeout"]
    args = set_arg(args, "--conversation-cache-dir", paths["cache_dir"], allow_add=True,
                   deltas=deltas, who="harness")
    cfg["args"] = args
    reserves_note = ("engine-default reserves (both reserve flags absent, operator directive "
                     "2026-11-23 23:00)" if C.margins_mode(window) == "engine-default" else
                     "reserves " + str(TUPLE["vram_reserve_mib"]) + "/"
                     + str(TUPLE["vram_reserve_later_mib"]))
    cfg["trial_note"] = ("port-ab leg " + paths["key"] + ": frozen Phase-2 tuple (" + reserves_note
                         + ", budget " + str(TUPLE["ram_budget_mib"]) + ", spec " + str(TUPLE["spec"])
                         + ", dflash-block " + str(TUPLE["dflash_block"]) + ", layer-split "
                         + str(TUPLE["layer_split"]) + ", helper slots " + str(TUPLE["helper_slots"])
                         + "); engine arm " + arm + "; fresh cache dir; trial sockets. Generated by "
                         "port_ab_configs.py - do not hand-edit.")
    return cfg, deltas, paths


def settings_only(cfg):
    """The config with every harness-owned path blanked, for a positional arm-to-arm diff."""
    out = {k: (PLACEHOLDER if k in HARNESS_FIELDS else v) for k, v in cfg.items()}
    args = list(cfg["args"])
    for flag in HARNESS_ARGS:
        if flag in args:
            args[args.index(flag) + 1] = PLACEHOLDER
    out["args"] = args
    return out


def blank_declared(cfg, who):
    """Remove both DECLARED structural differences from a config copy.

    A cannot carry either: the fork engine has no --conversation-spill-mib, and it has no
    pool MADV_NOHUGEPAGE path (so no STRATA_POOL_NOHUGEPAGE consumer). B carries both - the
    spill tier off, and the pool fix on. They mean "no spill" and "pool pages not huge" in
    both arms; leaving them in a digest or a diff would report the declared pair as a
    settings drift and hide a real one behind the noise.
    """
    args = ensure_absent(list(cfg["args"]), "--conversation-spill-mib", [], who)
    env = {k: v for k, v in (cfg.get("env") or {}).items() if k != "STRATA_POOL_NOHUGEPAGE"}
    return {**cfg, "args": args, "env": env}


def settings_sha256(cfg):
    """Digest of the settings with the harness paths and both declared pairs blanked."""
    blanked = settings_only(blank_declared(cfg, "digest"))
    return hashlib.sha256(json.dumps(blanked, sort_keys=True).encode()).hexdigest()


def structural_difference(a, b):
    """The two DECLARED structural differences between the arms.

    1. arm B carries --conversation-spill-mib 0, arm A cannot (no such option). Both = no spill.
    2. arm B carries STRATA_POOL_NOHUGEPAGE=1 (the promoted pool fix); arm A has no such code
       path. Declared 2026-11-24 with the operator re-pin of arm B to 15d330ea.
    """
    return {"spill_flag": {"A": arg_value(a["args"], "--conversation-spill-mib"),
                           "B": arg_value(b["args"], "--conversation-spill-mib"),
                           "meaning": "no spill tier in A (flag absent), tier present and off in B"},
            "pool_nohuge": {"A": (a.get("env") or {}).get("STRATA_POOL_NOHUGEPAGE"),
                            "B": (b.get("env") or {}).get("STRATA_POOL_NOHUGEPAGE"),
                            "meaning": "A has no pool THP exclusion (no consumer in the fork); "
                                       "B maps the 32 GiB pool with MADV_NOHUGEPAGE"}}


def cross_arm_diffs(a, b):
    """Every setting that is NOT identical after the harness paths and the declared
    structural spill pair are blanked. Empty means only the arm identity differs."""
    if arg_value(b["args"], "--conversation-spill-mib") != "0" or \
            arg_value(a["args"], "--conversation-spill-mib") is not None:
        raise SystemExit("spill flags are not the declared structural pair (A absent, B 0)")
    if (b.get("env") or {}).get("STRATA_POOL_NOHUGEPAGE") != "1" or \
            (a.get("env") or {}).get("STRATA_POOL_NOHUGEPAGE") is not None:
        raise SystemExit("pool knob is not the declared structural pair (A absent, B 1)")
    sa, sb = settings_only(blank_declared(a, "diff")), settings_only(blank_declared(b, "diff"))
    diffs = {}
    for key in sorted(set(sa) | set(sb)):
        if key == "args":
            continue
        if json.dumps(sa.get(key), sort_keys=True) != json.dumps(sb.get(key), sort_keys=True):
            diffs[key] = {"A": sa.get(key), "B": sb.get(key)}
    if len(sa["args"]) != len(sb["args"]):
        diffs["args/length"] = {"A": len(sa["args"]), "B": len(sb["args"])}
    for i in range(min(len(sa["args"]), len(sb["args"]))):
        if sa["args"][i] != sb["args"][i]:
            diffs["args[" + str(i) + "]"] = {"A": sa["args"][i], "B": sb["args"][i]}
    return diffs


def check_tuple(cfg, arm, *, window=None):
    """Fail-closed check that the config the server execs carries the whole frozen tuple.

    window selects the margins mode: an absent window means "explicit" (both reserves written
    from TUPLE), an engine-default window (W1d) must carry NEITHER reserve flag, so their
    presence there is the failure and their absence is what a W1d config must prove.
    """
    problems = []
    args = cfg["args"]
    margins = C.margins_mode(window) if window in C.WINDOWS else "explicit"
    if margins == "engine-default":
        for flag in ("--vram-reserve-mib", "--vram-reserve-later-mib"):
            if arg_value(args, flag) is not None:
                problems.append(arm + ": " + flag + " is " + repr(arg_value(args, flag))
                                + " but the window runs with engine-default margins")
    reserve_tuple = () if margins == "engine-default" else (
        ("--vram-reserve-mib", TUPLE["vram_reserve_mib"]),
        ("--vram-reserve-later-mib", TUPLE["vram_reserve_later_mib"]))
    for flag, value in reserve_tuple + (("--spec", TUPLE["spec"]),
                                        ("--dflash-block", TUPLE["dflash_block"]),
                                        ("--expert-helper-slots", TUPLE["helper_slots"])):
        if arg_value(args, flag) != str(value):
            problems.append(arm + ": " + flag + " is " + repr(arg_value(args, flag))
                            + ", tuple says " + str(value))
    spill = arg_value(args, "--conversation-spill-mib")
    if ARMS[arm]["spill_flag"]:
        if spill != str(TUPLE["conversation_spill_mib"]):
            problems.append(arm + ": spill flag is " + repr(spill) + ", tuple says 0 (tier off)")
    elif spill is not None:
        problems.append(arm + ": spill flag present (" + str(spill)
                        + ") but this engine has no spill tier")
    if cfg.get("layer_split") != TUPLE["layer_split"]:
        problems.append(arm + ": layer_split " + repr(cfg.get("layer_split")))
    if cfg["env"].get("STRATA_RAM_BUDGET_MIB") != str(TUPLE["ram_budget_mib"]):
        problems.append(arm + ": RAM budget " + repr(cfg["env"].get("STRATA_RAM_BUDGET_MIB")))
    # 2026-11-24 re-pin: arm B is the pool-MADV_NOHUGEPAGE build, so its config MUST carry the
    # knob (1) or the binary runs the fix off and the window measures the wrong behaviour. Arm A
    # carries no knob - the fork has no such code path.
    knob = cfg["env"].get("STRATA_POOL_NOHUGEPAGE")
    if ARMS[arm].get("pool_nohuge"):
        if knob != "1":
            problems.append(arm + ": STRATA_POOL_NOHUGEPAGE is " + repr(knob)
                            + " but arm " + arm + " runs the pool fix (must be \"1\")")
    elif knob is not None:
        problems.append(arm + ": STRATA_POOL_NOHUGEPAGE is " + repr(knob)
                        + " but arm " + arm + " has no pool fix")
    if cfg.get("expert_helper_slots") != TUPLE["helper_slots"]:
        problems.append(arm + ": expert_helper_slots key")
    if cfg.get("model_name") != TUPLE["model_name"]:
        problems.append(arm + ": model_name " + repr(cfg.get("model_name")))
    if cfg.get("reasoning_budget_tokens") != TUPLE["reasoning_budget_tokens"]:
        problems.append(arm + ": reasoning_budget_tokens "
                        + repr(cfg.get("reasoning_budget_tokens")))
    if cfg["exe"] != str(ROOT / ARMS[arm]["engine"]):
        problems.append(arm + ": exe " + str(cfg["exe"]))
    for p in (cfg["expert_helper_socket"], cfg["expert_helper_socket2"], cfg["log"],
              arg_value(cfg["args"], "--conversation-cache-dir")):
        if "prod" in p or "port0218-cache" in p or "diet-prod" in p:
            problems.append(arm + ": harness path still points at prod: " + p)
    return problems


def helper_argv(arm, paths):
    """The helper command line both launchers use, with the frozen fill/slots/reserve."""
    spec = ARMS[arm]
    return [str(ROOT / spec["helper"]),
            "--pack", "/mnt/srcds/ai/iq3xxs-ud",
            "--experts", "../Qwen3.8-Flash-Next-UD-IQ3_XXS-00002-of-00004.gguf",
            "--device", "0",
            "--slots", str(TUPLE["helper_slots"]),
            "--reserve-mib", str(TUPLE["helper_reserve_mib"]),
            "--timeout", "63115200000",
            "--begin-timeout", "63115200000",
            "--socket", paths["socket"],
            "--fill-mib", str(TUPLE["helper_fill_mib"]),
            "--socket2", paths["socket2"]]


# The helper's whole environment, frozen. The prod launchers pass env -i with the caller's
# PATH/HOME/TERM and a 600,000 ms reply timeout, but the helper is a native binary that needs
# none of a developer shell's PATH; inheriting the caller's would make the frozen env depend
# on who ran the window. FROZEN_HELPER_PATH is the stock system path, recorded in the manifest
# and in every boot record instead of being implicit in a shell.
FROZEN_HELPER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def helper_env():
    """The helper's environment, fully explicit and machine-independent."""
    return {"STRATA_HELPER_REPLY_TIMEOUT_MS": "600000",
            "PATH": FROZEN_HELPER_PATH,
            "HOME": "/home/srcds",
            "TERM": "dumb"}


def main():
    ap = argparse.ArgumentParser(description="derive the Phase-2 arm configs from the frozen tuple")
    ap.add_argument("--arm", required=True, choices=["A", "B"])
    ap.add_argument("--window", default="W1", choices=sorted(C.WINDOWS))
    ap.add_argument("--boot", default=None, help="boot group (W2: dec or sht)")
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--out-dir", type=pathlib.Path, default=OUT_DEFAULT)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--write", action="store_true", help="write the config under <out>/configs/")
    ap.add_argument("--print", dest="show", action="store_true", help="print the config JSON")
    a = ap.parse_args()
    cfg, deltas, paths = derive(a.arm, window=a.window, rep=a.rep, out=a.out_dir,
                                port=a.port, boot=a.boot)
    problems = check_tuple(cfg, a.arm, window=a.window)
    for p in problems:
        print("PROBLEM: " + p, file=sys.stderr)
    if problems:
        return 1
    if a.write:
        target = pathlib.Path(paths["config"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(cfg, indent=1, sort_keys=True) + "\n")
        print("wrote " + str(target) + " (" + str(len(deltas)) + " deltas)")
    if a.show:
        print(json.dumps(cfg, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

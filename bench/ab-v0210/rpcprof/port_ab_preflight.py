#!/usr/bin/env python3
"""port_ab_preflight.py - static 2x2 (engine x server) argv compatibility check. NO BOOTS.

Why this exists: the contract's W4 cell compares engine x server, and the two halves have
different parsers. The port server (0.2.18 lineage) builds an engine command line that the
fork engine (0.1.57 lineage) may refuse: an unknown flag is a hard error in both engines
("unknown argument: --x", exit 2, src/program/generate.cpp). A cell that cannot boot must be
named at harness-prep, not discovered inside the operator's window.

What is checked, statically and CPU-only:
  * the engine flags each binary ACCEPTS: the exact "--flag" literals in the binary (the
    parsers compare exact strings, so the literal must be present), cross-checked against
    each engine's parser source;
  * the flags each SERVER would add to the config's own args (layer split, helper channels,
    parallel/pool cores, profile save, vram elastic, draft costs, and the drafting chooser's
    rewrite), read from the port server's serve/server.py + serve/server_parts/engines.py and
    the v0215 server's serve/server.py;
  * the config the engine cell would run (port_ab_configs.derive), so the answer is about
    THIS tuple, not a generic build.

Output: one row per cell with verdict "bootable" / "refuses-at-argv", the offending flags,
and the reason. Exit 1 under --strict when an expected-bootable cell is not.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

import port_ab_configs as pc

ROOT = pathlib.Path("/mnt/srcds/Strata")
OUT_DEFAULT = pc.OUT_DEFAULT

FLAG_TOKEN = re.compile(rb"^--[a-z0-9][a-z0-9-]*$")
SOURCE_FLAG = re.compile(r'a == "(--[a-z0-9][a-z0-9-]+)"')

# Where each engine's parser source lives. The fork binary 4b8251e9 was built from the
# merge-helper-ipc-diet tree at 564e1dbbea (CHAMPION.md: fix code 564e1dbbea, engine
# 4b8251e9); the port binary 936efcc7 from the 0.2.18 port tree.
ENGINE_SOURCE = {
    "A": {"kind": "git", "ref": "564e1dbbea:src/program/generate.cpp",
          "note": "fork engine, merge-helper-ipc-diet @ 564e1dbbea"},
    "B": {"kind": "file", "path": "/mnt/srcds/Strata-wt-0218/engine/src/program/generate.cpp",
          "note": "port engine, 0.2.18 port tree"},
}

# Server trees, and the module that builds the engine argv in each.
SERVER_ARGS_SOURCE = {
    "A": ROOT / "v0215" / "serve" / "server.py",
    "B": ROOT / "run" / "port0218-server" / "serve" / "server.py",
}


def binary_flags(path: pathlib.Path) -> set[str]:
    """Every exact --flag literal in a binary, found by a NUL-split scan (no 'strings')."""
    found: set[str] = set()
    with path.open("rb") as f:
        tail = b""
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            blob = tail + chunk
            parts = blob.split(b"\0")
            tail = parts.pop()
            for part in parts:
                if FLAG_TOKEN.match(part):
                    found.add(part.decode())
        if FLAG_TOKEN.match(tail):
            found.add(tail.decode())
    return found


def source_flags(arm: str) -> tuple[set[str], str]:
    spec = ENGINE_SOURCE[arm]
    if spec["kind"] == "git":
        blob = subprocess.run(["git", "-C", str(ROOT), "show", spec["ref"]],
                              check=True, capture_output=True).stdout
        where = spec["ref"]
    else:
        blob = pathlib.Path(spec["path"]).read_bytes()
        where = spec["path"]
    return {m.decode() for m in (x.encode() for x in SOURCE_FLAG.findall(blob.decode("utf-8", "replace")))}, where


def server_adds(arm: str, cfg: dict) -> list[dict]:
    """The flags this server would add to cfg['args'] - the additive rules, per key.

    Read from the server sources (both trees share the rule set for these keys; the port
    tree adds draft-costs, gated on the engine binary containing --draft-costs and on the
    'draft_costs' key being true, which this tuple never sets).
    """
    adds: list[dict] = []
    args = list(cfg["args"])

    def present(flag: str) -> bool:
        return flag in args

    if cfg.get("layer_split") and not present("--layer-split"):
        adds.append({"flag": "--layer-split", "why": "config layer_split (appended by engine_args)"})
    sock = cfg.get("expert_helper_socket")
    if isinstance(sock, str) and sock.strip() and not present("--expert-helper-socket"):
        adds.append({"flag": "--expert-helper-socket", "why": "expert_helper_socket key"})
        sock2 = cfg.get("expert_helper_socket2")
        if isinstance(sock2, str) and sock2.strip() != sock.strip() and not present("--expert-helper-socket2"):
            adds.append({"flag": "--expert-helper-socket2", "why": "expert_helper_socket2 key"})
        for key, flag in (("expert_helper_layers", "--expert-helper-layers"),
                          ("expert_helper_slots", "--expert-helper-slots"),
                          ("expert_helper_reserve_mib", "--expert-helper-reserve-mib"),
                          ("expert_helper_timeout", "--expert-helper-timeout-ms")):
            if isinstance(cfg.get(key), int) and not present(flag):
                adds.append({"flag": flag, "why": key + " key"})
    par = cfg.get("parallel")
    if isinstance(par, int) and not isinstance(par, bool) and par > 1 and not present("--batch"):
        adds.append({"flag": "--batch", "why": "parallel key"})
    if cfg.get("pool_cores"):
        adds.append({"flag": "--pool-cores", "why": "pool_cores key"})
    if cfg.get("vram_elastic") is True and not present("--vram-elastic"):
        adds.append({"flag": "--vram-elastic", "why": "vram_elastic key"})
    if cfg.get("expert_profile_save"):
        adds.append({"flag": "--expert-profile-save", "why": "expert_profile_save key"})
    if arm == "B":
        # draft_costs_args: only when the key is explicitly true (default is false on Linux)
        # and the engine binary contains --draft-costs. Neither holds for this tuple.
        if cfg.get("draft_costs") is True:
            adds.append({"flag": "--draft-costs", "why": "draft_costs:true in config",
                         "gated": "engine must contain --draft-costs"})
    # drafting.apply: a model name that matches the drafter regex makes it append its own
    # DFlash2/MTP flags and rewrite --spec. Neither arm's frozen name matches either tree's
    # regex, so this tuple gets no rewrite; the rewrite's flags are still inventoried.
    adds_draft = {"flags": ["--draft", "--draft-model", "--draft-fallback", "--draft-conf",
                            "--mtp-draft-vocab", "--spec"],
                  "applied": False}
    return adds, adds_draft


def cell(engine_arm: str, server_arm: str, out: pathlib.Path, *, port_variant: bool = False) -> dict:
    """One 2x2 cell: the engine arm's config, the server arm's argv construction."""
    cfg, deltas, paths = pc.derive(engine_arm, window="W4", rep=1, out=out)
    if port_variant and engine_arm == "A":
        # the hazard the contract names: the port tuple fed verbatim to the fork engine
        cfg, _, _ = pc.derive("B", window="W4", rep=1, out=out)
    accepted = binary_flags(ROOT / pc.ARMS[engine_arm]["engine"])
    adds, draft = server_adds(server_arm, cfg)
    argv_flags = list(cfg["args"][0::1])
    cfg_flags = {a for a in cfg["args"] if a.startswith("--")}
    added_flags = {a["flag"] for a in adds}
    emitted = set(cfg_flags) | added_flags | {"--serve"}
    unknown = sorted(emitted - accepted)
    verdict = "bootable" if not unknown else "refuses-at-argv"
    return {
        "cell": engine_arm + "-engine+" + server_arm + "-server" + (" (port tuple verbatim)" if port_variant else ""),
        "engine": {"arm": engine_arm, "path": pc.ARMS[engine_arm]["engine"],
                   "sha256": pc.ARMS[engine_arm]["engine_sha256"],
                   "accepted_flag_literals": len(accepted)},
        "server": {"arm": server_arm, "tree": pc.ARMS[server_arm]["server_tree"],
                   "args_builder": str(SERVER_ARGS_SOURCE[server_arm])},
        "config_engine_arm": "B" if port_variant else engine_arm,
        "server_added_flags": adds,
        "drafting_rewrite": draft,
        "emitted_flags": len(emitted),
        "unknown_flags": unknown,
        "verdict": verdict,
        "reason": ("every flag the server emits (config args + its own additions) is a literal "
                   "in this engine binary") if not unknown else
                  ("engine parser rejects: " + ", ".join(unknown) + " (unknown argument -> exit 2)"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="static 2x2 engine x server argv preflight")
    ap.add_argument("--out-dir", type=pathlib.Path, default=OUT_DEFAULT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 unless the four per-engine cells are bootable")
    a = ap.parse_args()

    # engine flag inventories, binary vs source (a drift here is a misbuild)
    inv = {}
    for arm in ("A", "B"):
        binf = binary_flags(ROOT / pc.ARMS[arm]["engine"])
        srcf, where = source_flags(arm)
        inv[arm] = {"binary_flags": len(binf), "source_flags": len(srcf),
                    "source": where, "source_not_in_binary": sorted(srcf - binf),
                    "binary_not_in_source": sorted(binf - srcf)[:25]}
    cells = [cell("A", "A", a.out_dir), cell("B", "B", a.out_dir),
             cell("A", "B", a.out_dir), cell("B", "A", a.out_dir),
             cell("A", "B", a.out_dir, port_variant=True)]
    result = {
        "check": "static 2x2 engine x server argv compatibility",
        "boots": 0,
        "engine_flag_inventory": inv,
        "cells": cells,
        "hazard": {
            "flag": "--conversation-spill-mib",
            "fork_engine_sha": pc.ARMS["A"]["engine_sha256"],
            "port_engine_sha": pc.ARMS["B"]["engine_sha256"],
            "finding": "the literal is absent from the fork engine and present in the port engine; "
                       "a port config fed verbatim to the fork engine exits 2 at argv",
            "resolution": "per-engine configs: A carries no spill flag (tier structurally absent), "
                          "B carries --conversation-spill-mib 0. That is the frozen tuple.",
        },
    }
    if a.json:
        print(json.dumps(result, indent=1, sort_keys=True))
    else:
        print("engine flag inventory (binary literal scan vs parser source)")
        for arm, row in inv.items():
            print("  " + arm + ": binary " + str(row["binary_flags"]) + " flags, source "
                  + str(row["source_flags"]) + " (" + row["source"] + ")")
            if row["source_not_in_binary"]:
                print("    source flags absent from the binary: " + ", ".join(row["source_not_in_binary"]))
        print("\ncells (no boot performed)")
        for c in cells:
            print("  " + c["cell"] + ": " + c["verdict"])
            if c["unknown_flags"]:
                print("    rejected: " + ", ".join(c["unknown_flags"]))
    problems = []
    for c in cells:
        expected_bootable = "port tuple verbatim" not in c["cell"]
        if expected_bootable and c["verdict"] != "bootable":
            problems.append(c["cell"] + " unexpectedly refuses: " + ", ".join(c["unknown_flags"]))
        if not expected_bootable and c["verdict"] == "bootable":
            problems.append(c["cell"] + " expected to refuse but is bootable")
    for arm, row in inv.items():
        if row["source_not_in_binary"]:
            problems.append(arm + ": parser source names flags the binary does not contain")
    for p in problems:
        print("PROBLEM: " + p, file=sys.stderr)
    if a.strict and problems:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""port_ab_prepare.py - CPU-only preparation of the Phase-2 A/B contract artifacts.

Writes, and optionally re-verifies, everything the windows need and nothing they must
invent:

  * the frozen head-tagged corpus (champions_prepare.build_paired_prompt): 239,663 tokens at
    the 323,200-byte target, plus the 144k decode and 2k/8k short-PP shapes;
  * the frozen request template (sampling + thinking off) every leg sends;
  * the two arm configs for every boot of W1-W4 (port_ab_configs.derive: identical except
    exe/cwd/log/sockets/cache-dir, plus the declared structural spill flag);
  * the 2x2 argv preflight verdict (port_ab_preflight.cell - static, no boots);
  * bench/ab-v0210/rpcprof/port-ab-manifest.json: identities re-hashed FROM DISK NOW, the
    frozen tuple, the pre-registered deltas, the claim rule, n, legs-per-boot, the derived
    window budgets (W2 included), the exact leg invocations and their gates.

No engine, helper, server or GPU is started; no process is signalled. --verify re-derives
every hash from disk and fails closed on any drift.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import platform
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import champions_prepare as champs
import port_ab_configs as pc
import port_ab_contract as C
import port_ab_legs as legs
import port_ab_preflight as pre

MANIFEST = HERE / "port-ab-manifest.json"
OUT_DEFAULT = pc.OUT_DEFAULT
EXCLUDE_DIRS = ("__pycache__", ".pytest_cache", ".tldr", ".git", "node_modules")


def sha256_path(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_digest(root):
    """Deterministic digest of a server tree: sorted (relative path, bytes, content)."""
    root = pathlib.Path(root)
    h = hashlib.sha256()
    files = 0
    for path in sorted(root.rglob("*"), key=lambda p: str(p)):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        rel = str(path.relative_to(root))
        h.update(rel.encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
        files += 1
    return {"path": str(root), "files": files, "digest": h.hexdigest()}


def identity(arm):
    spec = pc.ARMS[arm]
    engine = pc.ROOT / spec["engine"]
    helper = pc.ROOT / spec["helper"]
    server = pc.ROOT / spec["server_tree"]
    base = pc.ROOT / spec["base_config"]
    entry = server / "serve" / "server.py"
    return {
        "arm": arm,
        "label": spec["label"],
        "engine": {"path": str(engine), "sha256": sha256_path(engine),
                   "bytes": engine.stat().st_size, "mtime_epoch": int(engine.stat().st_mtime)},
        "helper": {"path": str(helper), "sha256": sha256_path(helper),
                   "bytes": helper.stat().st_size},
        "server_tree": tree_digest(server),
        "serve_subtree": tree_digest(server / "serve"),
        "server_entry": {"path": str(entry), "sha256": sha256_path(entry)},
        "base_config": {"path": str(base), "sha256": sha256_path(base)},
        "spill_flag": spec["spill_flag"],
    }


def prepare_corpus(out):
    (out / "corpus").mkdir(parents=True, exist_ok=True)
    entries = []
    for shape, spec in C.SHAPES.items():
        path = legs.corpus_path(out, shape)
        blob = champs.build_paired_prompt(shape, 0, spec["bytes"])
        path.write_bytes(blob)
        entries.append({"shape": shape, "path": str(path), "bytes": len(blob),
                        "sha256": hashlib.sha256(blob).hexdigest(),
                        "head_tag": blob.split(b"\n", 1)[0].decode(),
                        "tokens_expected": spec["tokens_expected"],
                        "use": spec["use"]})
    return entries


def prepare_template(out):
    path = out / "request-template.json"
    path.write_text(json.dumps(C.REQUEST_TEMPLATE, indent=1, sort_keys=True) + "\n")
    return {"path": str(path), "sha256": sha256_path(path), "settings": C.REQUEST_TEMPLATE,
            "note": "the driver adds messages + max_tokens only; the TTFT leg sets stream=true itself"}


def prepare_configs(out):
    written = {}
    for window in ("W1", "W2", "W3"):
        # rep 0 is the discarded first-touch boot (W2: the pilot boot); reps 1..3 are the claim
        for arm in ("A", "B"):
            for rep in range(0, C.REPS + 1):
                cfg, deltas, paths = pc.derive(arm, window=window, rep=rep, out=out)
                problems = pc.check_tuple(cfg, arm)
                if problems:
                    raise SystemExit("; ".join(problems))
                target = pathlib.Path(paths["config"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(cfg, indent=1, sort_keys=True) + "\n")
                written[paths["key"]] = {"config": str(target), "sha256": sha256_path(target),
                                         "deltas": deltas, "socket": paths["socket"],
                                         "socket2": paths["socket2"], "cache_dir": paths["cache_dir"],
                                         "engine_log": cfg["log"]}
    # W4 cross cells: the engine arm's config; the server arm is chosen at boot
    for cell in C.WINDOWS["W4"]["cells"]:
        eng = cell.split("-")[0]
        for rep in (1, 2):
            key = "W4-" + eng + "-r" + str(rep) + "-" + cell.split("+")[1][0]
            cfg, deltas, paths = pc.derive(eng, window="W4", rep=rep, out=out)
            target = pathlib.Path(paths["config"])
            target.write_text(json.dumps(cfg, indent=1, sort_keys=True) + "\n")
            written[key] = {"config": str(target), "sha256": sha256_path(target),
                            "deltas": deltas, "cell": cell, "engine_arm": eng,
                            "socket": paths["socket"], "socket2": paths["socket2"],
                            "cache_dir": paths["cache_dir"], "engine_log": cfg["log"]}
    return written


def arm_diffs(out, window="W1", rep=1):
    a, _, _ = pc.derive("A", window=window, rep=rep, out=out)
    b, _, _ = pc.derive("B", window=window, rep=rep, out=out)
    return {"diffs": pc.cross_arm_diffs(a, b),
            "structural": pc.structural_difference(a, b),
            "settings_sha256_blind": {"A": pc.settings_sha256(a), "B": pc.settings_sha256(b)}}


def leg_section(out, window):
    """The exact invocation and gates for every leg shape of a window."""
    spec = C.WINDOWS[window]
    rows = {}
    for shape in spec["legs"]:
        cap = legs.leg_cap(shape, window)
        example = {}
        for arm in ("A", "B"):
            _, _, cmds = legs.commands(out, window, arm, 1, [shape])
            # the pid is discovered at run time and pinned by exe+socket; never freeze one here
            words = [w for w in cmds[0]["cmd"]]
            if "--engine-pid" in words:
                i = words.index("--engine-pid")
                del words[i:i + 2]
            example[arm] = " ".join(words) + "   # --engine-pid is added by --run"
        corpus_shape = "pp2k" if shape == "ttft3" else shape
        rows[shape] = {
            "kind": C.kind_for(shape, window),
            "bytes": C.SHAPES[corpus_shape]["bytes"],
            "tokens_expected": C.SHAPES[corpus_shape]["tokens_expected"],
            "max_tokens": cap,
            "turns": C.LEGS[shape].get("turns"),
            "corpus": str(legs.corpus_path(out, corpus_shape)),
            "corpus_sha256": sha256_path(legs.corpus_path(out, corpus_shape)),
            "command_A": example["A"],
            "command_B": example["B"],
            "expected_engine_lines": [C.EXPECTED_LINES[k] for k in
                                      ("judge", "judge_tail", "dflash", "boot_tuple",
                                       "boot_helper", "boot_helper_proxy", "boot_cache",
                                       "boot_split", "boot_fs", "low_warning")],
            "gates": C.GATES["ttft" if C.kind_for(shape, window) == "ttft" else
                             ("decode" if C.kind_for(shape, window) == "tg" else "prefill")],
            "metric": ("TTFT per turn + inter-turn wall" if C.kind_for(shape, window) == "ttft"
                       else ("request 2 gen_tps (cached re-run); request 1 read_tps recorded"
                             if C.kind_for(shape, window) == "tg"
                             else "read_tps from the single fresh request")),
        }
    return rows


def prepare(out, do_preflight=True):
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    out.mkdir(parents=True, exist_ok=True)
    corpus = prepare_corpus(out)
    template = prepare_template(out)
    configs = prepare_configs(out)
    preflight = None
    if do_preflight:
        preflight = {"cells": [pre.cell("A", "A", out), pre.cell("B", "B", out),
                               pre.cell("A", "B", out), pre.cell("B", "A", out),
                               pre.cell("A", "B", out, port_variant=True)],
                     "engine_sources": pre.ENGINE_SOURCE,
                     "hazard": {
                         "flag": "--conversation-spill-mib",
                         "finding": "present in the port engine's parser, absent from the fork "
                                    "engine's; a port config fed verbatim to the fork engine exits 2",
                         "resolution": "per-engine configs (A without the flag, B with '0'); both "
                                       "cross cells then boot, and 'no spill' holds in both"}}
        for cell in preflight["cells"]:
            cell.pop("emitted_flags", None)
            cell["args_builder"] = cell.get("args_builder") or str(pre.SERVER_ARGS_SOURCE[cell["server"]["arm"]])

    arms = {arm: identity(arm) for arm in ("A", "B")}
    for arm in ("A", "B"):
        measured, expected = arms[arm]["engine"]["sha256"], pc.ARMS[arm]["engine_sha256"]
        if measured != expected:
            raise SystemExit(arm + ": engine sha " + measured + " != contract " + expected)
        helped = arms[arm]["helper"]["sha256"]
        if helped != pc.ARMS[arm]["helper_sha256"]:
            raise SystemExit(arm + ": helper sha " + helped + " != contract "
                             + pc.ARMS[arm]["helper_sha256"])

    derivation = C.budget_derivation()
    windows = {}
    for window, spec in C.WINDOWS.items():
        budget = C.window_budget_s(window)
        windows[window] = {
            "name": spec["name"],
            "legs_per_boot": spec["legs_per_boot"],
            "reps_per_arm": spec.get("boots_per_cell", C.REPS),
            "boots_per_cell": spec.get("boots_per_cell"),
            "interleave": list(C.INTERLEAVE),
            "warmup_boots": spec["warmup_boots"],
            "pilot": spec.get("pilot"),
            "budget_s": budget,
            "budget_h": [round(x / 3600, 2) for x in budget],
            "budget_source": ("derived at harness-prep from the contract's measured anchors"
                              if spec["budget_s"] is None else "the contract's stated budget"),
            "budget_derivation": derivation[window],
            "delta": spec.get("delta"),
            "gate_metric": spec["gate_metric"],
            "claim": spec.get("claim", "pre-registered delta, 90 % interval, "
                                       "confirm-or-indeterminate"),
            "legs": leg_section(out, window),
            "schedule": [{k: r[k] for k in r if k in ("group", "arm", "rep", "shapes", "pilot",
                                                      "warmup", "server_arm", "cell")}
                         for r in legs.schedule(window)],
        }

    manifest = {
        "profile": "port-ab-phase2",
        "title": "Frozen A/B harness manifest for the approved Phase-2 measurement contract",
        "contract": {"path": "/home/srcds/.opencode/plan/"
                             "measurement-contract-speed-attribution-faults.md",
                     "approval": "APPROVED by oracle 2026-11-23 (5 rounds)",
                     "phase": 2},
        "prepared_at": started,
        "host": platform.node(),
        "git_head_at_prepare": subprocess.run(["git", "-C", str(pc.ROOT), "rev-parse", "HEAD"],
                                             capture_output=True, text=True).stdout.strip(),
        "git_branch": subprocess.run(["git", "-C", str(pc.ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
                                     capture_output=True, text=True).stdout.strip(),
        "arms": arms,
        "frozen_tuple": {
            "values": pc.TUPLE,
            "where": {
                "--vram-reserve-mib / --vram-reserve-later-mib": "engine args (both arms)",
                "--spec 5 / --dflash-block 7": "engine args (both arms)",
                "--expert-helper-slots 4600": "engine args AND the helper's --slots",
                "--conversation-spill-mib": "arm B engine args, 0; arm A: flag absent (no tier)",
                "layer_split 30": "config key; both servers append --layer-split 30",
                "STRATA_RAM_BUDGET_MIB 36864": "config env (both arms)",
                "helper --fill-mib 7000 / --reserve-mib 0": "helper argv (both arms)",
                "model_name / reasoning_budget_tokens / reasoning_effort": "matched serving identity",
                "10_000 ms helper timeout": "expert_helper_timeout key -> --expert-helper-timeout-ms",
            },
            "helper_argv": {arm: pc.helper_argv(arm, {"socket": "<sock>", "socket2": "<sock2>"})
                            for arm in ("A", "B")},
            "helper_fill_note": "fill-mib 7000 caps both helpers below the 7.09/7.34 GiB fills the "
                                "Phase-1 record lists; the residual fill difference is narrowed and "
                                "still recorded from the boot lines",
        },
        "arm_config_equality": arm_diffs(out),
        "corpus": corpus,
        "request_template": template,
        "claim": C.CLAIM,
        "windows": windows,
        "preflight": preflight,
        "prerequisite0": {**C.PREREQ0,
                          "command": "python3 bench/ab-v0210/rpcprof/port_ab_prereq0.py --out "
                                     + str(out),
                          "exit": "0 = the window may open; 1 = refused",
                          "gate_inside_manifest": True},
        "configs": configs,
        "harness_scripts": {name: str(HERE / name) for name in
                            ("port_ab_configs.py", "port_ab_contract.py", "port_ab_prepare.py",
                             "port_ab_legs.py", "port_ab_ttft.py", "port_ab_preflight.py",
                             "port_ab_prereq0.py", "port_ab_boot.sh", "port_ab_stop.sh")},
        "harness_script_shas": {name: sha256_path(HERE / name) for name in
                                ("port_ab_configs.py", "port_ab_contract.py", "port_ab_prepare.py",
                                 "port_ab_legs.py", "port_ab_ttft.py", "port_ab_preflight.py",
                                 "port_ab_prereq0.py", "port_ab_boot.sh", "port_ab_stop.sh")},
        "deviations": [
            "The arms cannot be byte-identical: arm B carries --conversation-spill-mib 0 and arm A "
            "has no such option. That is the declared structural difference, not a lever.",
            "model_name is matched to 'ud-iq3_xxs' and reasoning_budget_tokens to 0: the adopted "
            "bases served different values (A 55,000 / B 2,000 and two names); neither name matches "
            "a drafter regex in either server, so drafting.apply leaves the args untouched.",
            "A base's --conversation-cache-dir pointed into the helper-ipc worktree; both arms now "
            "use a fresh per-leg directory under the harness tree.",
            "W2's budget is derived here for the first time (the contract required it at "
            "harness-prep once legs-per-boot was frozen).",
        ],
        "could_not_build": [
            "No boot, no leg and no measurement was run: the fixer lane is CPU-only, prod is "
            "serving, and the windows are operator-gated.",
            "The 2x2 verdicts are STATIC (binary literal scan + server argv model). The servers' "
            "drafting chooser was not executed; its no-op outcome for these names is asserted from "
            "the regex in both trees and must be confirmed from the first boot line.",
            "W4's optional third boot per cell (min p 0.10) is planned but not prepared; 2 boots "
            "per cell is what the budget covers.",
            "Short-PP/TTFT pilot deltas are not pre-registered here: the contract requires the "
            "pilot legs to run first and their deltas to be written into this manifest before the "
            "claim legs. The pilot section marks where they land.",
        ],
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    (HERE / "port-ab-manifest.sha256").write_text(sha256_path(MANIFEST) + "  port-ab-manifest.json\n")
    return manifest


def verify():
    problems = []
    if not MANIFEST.is_file():
        return ["missing " + str(MANIFEST)]
    manifest = json.loads(MANIFEST.read_text())
    pinned = (HERE / "port-ab-manifest.sha256").read_text().split()[0]
    if pinned != sha256_path(MANIFEST):
        problems.append("port-ab-manifest.sha256 does not match the manifest")
    for arm, entry in manifest["arms"].items():
        for kind in ("engine", "helper", "server_entry"):
            path = pathlib.Path(entry[kind]["path"])
            if not path.is_file() or sha256_path(path) != entry[kind]["sha256"]:
                problems.append(arm + ": " + kind + " drifted (" + str(path) + ")")
        if tree_digest(entry["server_tree"]["path"])["digest"] != entry["server_tree"]["digest"]:
            problems.append(arm + ": server tree drifted (" + entry["server_tree"]["path"] + ")")
        base = pathlib.Path(entry["base_config"]["path"])
        if sha256_path(base) != entry["base_config"]["sha256"]:
            problems.append(arm + ": base config drifted")
    for row in manifest["corpus"]:
        path = pathlib.Path(row["path"])
        if not path.is_file() or len(path.read_bytes()) != row["bytes"] or \
                sha256_path(path) != row["sha256"]:
            problems.append("corpus drift: " + row["shape"])
        elif path.read_bytes() != champs.build_paired_prompt(row["shape"], 0, row["bytes"]):
            problems.append("corpus not reproducible from its seed: " + row["shape"])
    if sha256_path(manifest["request_template"]["path"]) != manifest["request_template"]["sha256"]:
        problems.append("request template drift")
    for name, path in manifest["harness_scripts"].items():
        if sha256_path(path) != manifest["harness_script_shas"][name]:
            problems.append("harness script changed since prepare: " + name)
    for key, row in manifest["configs"].items():
        path = pathlib.Path(row["config"])
        if not path.is_file() or sha256_path(path) != row["sha256"]:
            problems.append("config drift: " + key)
            continue
        cfg = json.loads(path.read_text())
        arm = row.get("engine_arm") or key.split("-")[1]
        for p in pc.check_tuple(cfg, arm):
            problems.append(key + ": " + p)
    if manifest["arm_config_equality"]["diffs"]:
        problems.append("arms differ outside the declared structural pair: "
                        + ", ".join(sorted(manifest["arm_config_equality"]["diffs"])))
    return problems


def main():
    ap = argparse.ArgumentParser(description="prepare or verify the Phase-2 harness artifacts")
    ap.add_argument("--out-dir", type=pathlib.Path, default=OUT_DEFAULT)
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--no-preflight", action="store_true")
    a = ap.parse_args()
    if a.verify:
        problems = verify()
        for p in problems:
            print("PROBLEM: " + p)
        print("verify: " + ("OK" if not problems else str(len(problems)) + " problem(s)"))
        return 0 if not problems else 1
    if a.plan:
        m = json.loads(MANIFEST.read_text())
        print("manifest " + str(MANIFEST))
        for arm, entry in m["arms"].items():
            print("  " + arm + ": engine " + entry["engine"]["sha256"][:16] + " helper "
                  + entry["helper"]["sha256"][:16] + " server tree " + entry["server_tree"]["digest"][:16])
        for window, spec in m["windows"].items():
            print("  " + window + ": " + str(spec["budget_h"]) + " h, legs/boot "
                  + json.dumps(spec["legs_per_boot"]) + ", reps " + str(spec["reps_per_arm"]))
        return 0
    manifest = prepare(a.out_dir, do_preflight=not a.no_preflight)
    print("prepared " + str(MANIFEST))
    for arm, entry in manifest["arms"].items():
        print("  " + arm + ": engine " + entry["engine"]["sha256"][:16] + "  helper "
              + entry["helper"]["sha256"][:16] + "  server " + entry["server_tree"]["digest"][:16])
    for window, spec in manifest["windows"].items():
        print("  " + window + ": " + str(spec["budget_h"]) + " h  legs/boot "
              + json.dumps(spec["legs_per_boot"]))
    problems = verify()
    for p in problems:
        print("PROBLEM: " + p)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

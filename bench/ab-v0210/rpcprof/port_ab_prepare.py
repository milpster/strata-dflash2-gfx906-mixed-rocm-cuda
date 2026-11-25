#!/usr/bin/env python3
"""port_ab_prepare.py - CPU-only preparation of the Phase-2 A/B contract artifacts.

Writes, and optionally re-verifies, everything the windows need and nothing they must
invent:

  * the frozen head-tagged corpus (champions_prepare.build_paired_prompt): 239,663 tokens at
    the 323,200-byte target, the 144k decode shape, the 2k/8k short-PP shapes and the TTFT
    leg's own 2k shape;
  * the frozen request template (sampling + thinking off) every leg sends;
  * both arm configs for every BOOT of W1-W4 and W1f (the claim-capable n=3 follow-up), keyed
    by window-arm-bootgroup-rep (the boot
    group is what keeps W2's decode and short boots from sharing a cache dir or a socket);
  * the 2x2 argv preflight verdict (port_ab_preflight.cell - static, no boots);
  * bench/ab-v0210/rpcprof/port-ab-manifest.json: identities re-hashed FROM DISK NOW, the
    frozen tuple including the helper fill band, the pre-registered deltas, the claim rule,
    n, boots-per-group, the derived window budgets (warmup boots included), the W2 pilot
    delta slots, and the exact leg invocations with their gates.

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


TEMPLATE_NOTES = {
    "claim": "the driver adds messages + max_tokens only; the TTFT leg sets stream=true itself",
    "prodshape": "W3p only (prod serving shape): thinking ON, no reasoning_effort override, so "
                 "the config's \"high\" applies. Not the claim template and never swapped with it",
}


def prepare_template(out, name="claim"):
    """Write one frozen request template (its own file, its own pin).

    "claim" is request-template.json - the template every existing record was measured with.
    "prodshape" is request-template-prodshape.json, W3p's thinking-on shape. Two files, two
    pins: a prod-shape run can never overwrite the claim template's bytes or sha.
    """
    spec = C.REQUEST_TEMPLATES[name]
    path = out / spec["file"]
    path.write_text(json.dumps(spec["settings"], indent=1, sort_keys=True) + "\n")
    return {"name": name, "path": str(path), "sha256": sha256_path(path),
            "settings": spec["settings"], "note": TEMPLATE_NOTES[name]}


def prepare_templates(out):
    return {name: prepare_template(out, name) for name in C.REQUEST_TEMPLATES}


def prepare_configs(out):
    """One config per scheduled boot, keyed by window-arm-bootgroup-rep."""
    written = {}
    for window in C.WINDOWS:
        variant = C.config_variant(window)      # None for every claim window
        for row in legs.schedule(window):
            arm, boot, rep = row["arm"], row["boot"], row["rep"]
            cfg, deltas, paths = pc.derive(arm, window=window, rep=rep, out=out, boot=boot,
                                           variant=variant)
            # the window decides the margins mode (a W1d config must prove BOTH reserve flags
            # absent, not carry the tuple's 256/192) AND the serving identity it measures (a
            # W3p config must prove the prod shape's two keys; every claim config the tuple's)
            problems = pc.check_tuple(cfg, arm, window=window, variant=variant)
            if problems:
                raise SystemExit("; ".join(problems))
            target = pathlib.Path(paths["config"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(cfg, indent=1, sort_keys=True) + "\n")
            entry = {"config": str(target), "sha256": sha256_path(target), "deltas": deltas,
                     "window": window, "arm": arm, "boot_group": boot, "rep": rep,
                     "pilot": bool(row.get("pilot")), "warmup": bool(row.get("warmup")),
                     "socket": paths["socket"], "socket2": paths["socket2"],
                     "cache_dir": paths["cache_dir"], "engine_log": cfg["log"],
                     "config_variant": variant}
            if variant:
                # the exact keys this config does NOT take from the claim tuple, so a reader of
                # the manifest sees the serving-shape delta without diffing two configs
                entry["variant_overrides"] = {k: v for k, v in pc.VARIANTS[variant].items()
                                              if k != "note"}
                entry["claim_config_note"] = ("NOT the claim config: this boot measures the "
                                              + variant + " serving shape")
            if row.get("server_arm"):
                entry["server_arm"] = row["server_arm"]
                entry["cell"] = row["cell"]
            written[paths["key"]] = entry
    return written


def arm_diffs(out, window="W1", rep=1):
    a, _, _ = pc.derive("A", window=window, rep=rep, out=out)
    b, _, _ = pc.derive("B", window=window, rep=rep, out=out)
    return {"diffs": pc.cross_arm_diffs(a, b),
            "structural": pc.structural_difference(a, b),
            "settings_sha256_blind": {"A": pc.settings_sha256(a), "B": pc.settings_sha256(b),
                                      "note": "the declared spill flag is blanked before hashing, "
                                              "so two identical-setting arms hash equal"}}


# The pre-registered delta a leg's rate is read against. None (as W3p has) means the leg has
# no pre-registered delta to move against: it is descriptive, never a claim input.
#
# W4 was None until 2026-11-25, which was right while it gated on gen_tps and so had no
# pre-registered decode delta of its own; it is now "prefill_read_tps" because the window's
# gate metric is read_tps (port_ab_contract W4 entry). The key names the CLAIM block's
# pre-registered 5 % prefill delta - the same one W1/W1d/W1f are read against - not a number
# invented for W4. The decode 8 % W2 key stays on W2, where the decode rate is measured and
# claimed; W4 cannot measure it (all four W2 decode boots generated 512-513 tokens at a
# 1,024 cap, under the 1,000-token TG floor). The delta is still CONTEXT here: at 2 boots per
# cell W4 is veto-only, so no interval exists that could clear it (see the window's claim).
DELTA_KEY = {"pp239k": {"W1": "prefill_read_tps", "W1f": "prefill_read_tps",
                       "W3": "decode_gen_tps"},
             "pp239k-prodshape": {"W3p": None},
             # W3s is diagnostic: an instrumented rate has no pre-registered delta to move
             # against, and neither span shape is a claim shape.
             "dec144k-spans": {"W3s": None},
             "pp239k-spans": {"W3s": None},
             "dec144k": {"W2": "decode_gen_tps", "W4": "prefill_read_tps"},
             "pp2k": {"W2": "short_pp_read_tps"},
             "pp8k": {"W2": "short_pp_read_tps"},
             "ttft3": {"W2": "ttft_turn1_s"}}


def leg_section(out, window):
    """The exact invocation and gates for every leg shape of a window."""
    rows = {}
    variant = C.config_variant(window)
    template = C.REQUEST_TEMPLATES[C.request_template(window)]
    for shape in C.shapes_of(window):
        cap = legs.leg_cap(shape, window)
        boot = pc.boot_for_shape(window, shape)
        example = {}
        # only the window's own arms: W3p boots arm B only, and a fabricated arm-A command
        # would read as an arm-A row the window never runs
        for arm in C.window_arms(window):
            _, _, cmds = legs.commands(out, window, arm, 1, [shape], boot=boot)
            words = list(cmds[0]["cmd"])
            if "--engine-pid" in words:
                i = words.index("--engine-pid")
                del words[i:i + 2]
            example[arm] = " ".join(words) + "   # --engine-pid is added by --run"
        cs = C.corpus_shape(shape)
        gates = list(C.GATES["ttft" if C.kind_for(shape, window) == "ttft" else
                             ("decode" if C.kind_for(shape, window) == "tg" else "prefill")])
        if C.margins_mode(window) == "engine-default":
            # the shared gate text names the tuple's 256/192; this window supersedes it
            gates.append("engine-default margins (operator directive 2026-11-23 23:00): "
                         "--vram-reserve-mib and --vram-reserve-later-mib must be ABSENT from "
                         "the engine boot line - a reserve flag here means the wrong boot")
        if variant:
            gates.append("config variant " + variant + " ("
                         + json.dumps({k: v for k, v in pc.VARIANTS[variant].items()
                                       if k != "note"}, sort_keys=True)
                         + "): the booted config MUST carry it and the request MUST be sent "
                         + "with " + template["file"] + " - this boot is NOT the claim shape")
        env_extra = C.window_env(window)
        if env_extra:
            # the diagnostic switch is a GATE, not a note: a span leg whose engine log carries
            # no "rpcprofile:" line measured nothing, and the whole point of the window is that
            # line. The engine log must also show the per-layer lines, not only the pass line.
            gates.append("diagnostic env " + json.dumps(env_extra, sort_keys=True)
                         + " MUST be in the booted config's env (both arms) and the leg's "
                         "engine-log slice MUST carry rpcprofile: chunk / span / pass lines - "
                         "with none, the leg measured no breakdown and is void; the span "
                         "logging adds wall time, so this rate is instrumented, never a speed "
                         "measurement")
        rows[shape] = {
            "kind": C.kind_for(shape, window),
            "boot_group": boot,
            "bytes": C.SHAPES[cs]["bytes"],
            "tokens_expected": C.SHAPES[cs]["tokens_expected"],
            "max_tokens": cap,
            "turns": C.LEGS[shape].get("turns"),
            "corpus": str(legs.corpus_path(out, shape)),
            "corpus_sha256": sha256_path(legs.corpus_path(out, shape)),
            # the request shape this leg is fired with, pinned by path and sha like the corpus
            "request_template": template["file"],
            "request_template_sha256": sha256_path(out / template["file"]),
            "request_settings": template["settings"],
            "config_variant": variant,
            "command_A": example.get("A"),
            "command_B": example.get("B"),
            "expected_engine_lines": [C.EXPECTED_LINES[k] for k in
                                      ("judge", "judge_tail", "dflash", "boot_started",
                                       "boot_helper", "boot_helper_proxy", "boot_cache",
                                       "boot_split", "boot_fs", "low_warning", "restart")],
            "gates": gates,
            "metric": ("TTFT per turn + inter-turn wall (next first token - previous finish)"
                       if C.kind_for(shape, window) == "ttft"
                       else ("request 2 gen_tps (cached re-run); request 1 read_tps recorded"
                             + (" - PROD SHAPE (thinking ON): comparable to prod's serving "
                                "shape, never pooled with a thinking-off prefill leg"
                                if variant else "")
                             # A tg-shaped leg whose window makes NO TG claim (W4): the window
                             # gates on request 1's read rate, and the decode numbers the boot
                             # still records are context only. Named here so a reader of the
                             # manifest never takes the leg's kind for its claim.
                             + ("" if C.claims_tg(shape, window) else
                                "; THIS WINDOW GATES ON " + str(C.WINDOWS[window]["gate_metric"])
                                + ": no TG claim is made on this leg (it cannot clear the "
                                + str(C.TG_CLAIM_MIN_GENERATED) + "-token floor at a "
                                + str(cap) + "-token cap in this harness), so the decode rate "
                                  "is context only")
                             if C.kind_for(shape, window) == "tg"
                             else "read_tps from the single fresh request"
                                  + (" plus gen_tps off the SAME judge line ("
                                     + str(cap) + " generated tokens clear the "
                                     + str(C.TG_CLAIM_MIN_GENERATED) + "-token floor; the "
                                     "prefill claim metric stays read_tps)"
                                     if C.claims_tg(shape, window) else ""))),
            "delta_key": DELTA_KEY.get(shape, {}).get(window),
        }
    return rows


def schedule_interleave(window):
    """The boot order of a window, as concrete boot keys (never a generic 6-boot pattern)."""
    return [pc.boot_key(window, row["arm"], row["rep"], row["boot"])
            for row in legs.schedule(window)]


def prepare(out, do_preflight=True, previous_manifest=None):
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    out.mkdir(parents=True, exist_ok=True)
    corpus = prepare_corpus(out)
    template = prepare_template(out, "claim")      # the claim manifest slot is unchanged
    templates = prepare_templates(out)             # every template, claim + variants
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

    arms = {arm: identity(arm) for arm in ("A", "B")}
    for arm in ("A", "B"):
        measured, expected = arms[arm]["engine"]["sha256"], pc.ARMS[arm]["engine_sha256"]
        if measured != expected:
            raise SystemExit(arm + ": engine sha " + measured + " != contract " + expected)
        helped = arms[arm]["helper"]["sha256"]
        if helped != pc.ARMS[arm]["helper_sha256"]:
            raise SystemExit(arm + ": helper sha " + helped + " != contract "
                             + pc.ARMS[arm]["helper_sha256"])

    # a frozen pilot survives a re-prepare: never silently reset deltas the operator froze
    carried_pilot = None
    if previous_manifest and pathlib.Path(previous_manifest).is_file():
        try:
            old = json.loads(pathlib.Path(previous_manifest).read_text())
            carried_pilot = ((old.get("windows") or {}).get("W2") or {}).get("pilot_deltas")
        except ValueError:
            carried_pilot = None

    derivation = C.budget_derivation()
    windows = {}
    for window, spec in C.WINDOWS.items():
        budget = C.window_budget_s(window)
        contract = C.window_contract_budget_s(window)
        entry = {
            "name": spec["name"],
            # how the window's configs carry the VRAM reserves: "explicit" writes the tuple's
            # 256/192, "engine-default" (W1d, operator directive 2026-11-23 23:00) removes both
            # flags so the engine's own margins apply
            "margins": C.margins_mode(window),
            # the serving SHAPE this window's configs and requests carry: "claim" for every
            # existing window, "prodshape" for W3p. Named in the manifest so no reader has to
            # infer from a config diff whether a rate is poolable with the claim legs.
            "config_variant": C.config_variant(window),
            # the engine-side INSTRUMENTATION the window's configs turn on (W3s:
            # STRATA_ENGINE_SPANS=1). Not a serving-shape variant: it changes what the engine
            # logs, and its rates are instrumented rates, never speed measurements.
            "diagnostic_env": C.window_env(window),
            "request_template": C.REQUEST_TEMPLATES[C.request_template(window)]["file"],
            "arms": list(C.window_arms(window)),
            "poolable_with_claim_legs": spec.get("poolable_with_claim_legs", True),
            "boot_groups": spec["boot_groups"],
            "legs_per_boot": spec["legs_per_boot"],
            "boots_per_arm": spec.get("boots_per_arm"),
            "boots_per_cell": spec.get("boots_per_cell"),
            "boots": derivation[window]["boots"],
            "warmup_boots": spec["warmup_boots"],
            "pilot": spec.get("pilot"),
            "decode_warmup": spec.get("decode_warmup"),
            "interleave": schedule_interleave(window),
            "budget_s": budget,
            "budget_h": [round(x / 3600, 2) for x in budget],
            "budget_source": "derived at harness-prep from the contract's measured anchors, "
                             "including the discarded first-touch boots",
            "contract_budget_s": contract,
            "contract_budget_h": None if contract is None else [round(x / 3600, 2) for x in contract],
            "budget_derivation": derivation[window],
            "delta": spec.get("delta"),
            "delta_note": spec.get("delta_note"),
            "gate_metric": spec["gate_metric"],
            # what the gate metric is measured on, when the window needs to say (W4: request 1's
            # read_tps, the one number its 144k legs yield that can be judged)
            "metric_note": spec.get("metric_note"),
            # W1f carries its own claim text (it is the n=3 follow-up); every other window
            # gets the n=2/arm descriptive-only statement the contract's claim rule requires
            "claim": spec.get("claim", "descriptive only at n=2/arm (per-leg rates, ranges, "
                                       "memory, an UNCONFIRMED direction); any confirm / "
                                       "no-difference / within-delta statement needs a "
                                       "follow-up window at n>=3 - see the top-level claim block"),
            "legs": leg_section(out, window),
            "schedule": [{k: r[k] for k in r if k in ("group", "boot", "arm", "rep", "shapes",
                                                      "pilot", "warmup", "server_arm", "cell")}
                         for r in legs.schedule(window)],
        }
        if window == "W2":
            entry["pilot_deltas"] = (carried_pilot if (carried_pilot or {}).get("frozen")
                                     else C.pilot_deltas_template())
        if spec.get("measurement_caveat"):
            # the caveat the window's rates must always be reported with, carried by the
            # manifest rather than left to whoever writes the summary
            entry["measurement_caveat"] = spec["measurement_caveat"]
        windows[window] = entry

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
                "helper env": "STRATA_HELPER_REPLY_TIMEOUT_MS 600000, frozen PATH/HOME/TERM",
                "model_name / reasoning_budget_tokens / reasoning_effort": "matched serving identity",
                "10_000 ms helper timeout": "expert_helper_timeout key -> --expert-helper-timeout-ms",
            },
            "helper_argv": {arm: pc.helper_argv(arm, {"socket": "<sock>", "socket2": "<sock2>"})
                            for arm in ("A", "B")},
            "helper_env": pc.helper_env(),
            "helper_fill_band": C.HELPER_FILL_BAND,
            "helper_fill_note": "fill-mib 7000 caps both helpers below the 7.09/7.34 GiB fills the "
                                "Phase-1 record lists; the band above is what the boot lines are "
                                "checked against, and it is never widened after the fact",
        },
        "arm_config_equality": arm_diffs(out),
        "corpus": corpus,
        # "request_template" stays the CLAIM template at the same key and shape, so every
        # existing pin and reader is untouched; "request_templates" names each window's own
        # file and sha (W3p's prod-shape template lives here, at its own path).
        "request_template": {k: v for k, v in template.items() if k != "name"},
        "request_templates": templates,
        "claim": C.CLAIM,
        "windows": windows,
        "preflight": preflight,
        "prerequisite0": {**C.PREREQ0,
                          "command": "python3 bench/ab-v0210/rpcprof/port_ab_prereq0.py --out "
                                     + str(out),
                          "exit": "0 = the window may open; 1 = refused",
                          "gate_inside_manifest": True},
        "configs": configs,
        "harness_scripts": {name: str(HERE / name) for name in HARNESS_SCRIPTS},
        "harness_script_shas": {name: sha256_path(HERE / name) for name in HARNESS_SCRIPTS},
        "deviations": [
            "The arms cannot be byte-identical: arm B carries --conversation-spill-mib 0 and arm A "
            "has no such option. That is the declared structural difference, not a lever.",
            "model_name is matched to 'ud-iq3_xxs' and reasoning_budget_tokens to 0: the adopted "
            "bases served different values (A 55,000 / B 2,000 and two names); neither name matches "
            "a drafter regex in either server, so drafting.apply leaves the args untouched.",
            "A base's --conversation-cache-dir pointed into the helper-ipc worktree; both arms now "
            "use a fresh per-boot directory under the harness tree.",
            "The boot key includes the boot group (W2-A-dec-r1 / W2-A-sht-r1): W2's two boot "
            "shapes must never share a cache dir, sockets, logs or state file.",
            "The helper's environment is frozen (PATH/HOME/TERM/STRATA_HELPER_REPLY_TIMEOUT_MS) and "
            "recorded per boot, instead of inheriting the caller's shell.",
            "W2 now has a discarded decode warmup boot per arm as W1/W3 do, and the budgets count "
            "every discarded boot; the contract's numbers (1.5-2.5 h W1, 1.2-2 h W3) are kept "
            "beside the derived planning ranges.",
            "Per-boot conversation caches are REMOVED at stop (port_ab_stop.sh, operator directive "
            "2026-11-24): every boot insists on an empty cache dir and never reuses one, so a "
            "cache left behind was 1.4-2.8 GiB of pure waste that filled the disk (16 boots = "
            "25 GiB) until prerequisite 0's 40 GiB gate refused a boot. Nothing a leg needs lives "
            "there - the record is records/<key>/*.json and the log is logs/<key>.*.log. A cache "
            "dir missing after a completed leg is therefore expected; --keep-cache on "
            "port_ab_stop.sh parks it, and port_ab_prune.py lists or sweeps leftovers (it never "
            "touches a cache whose boot has no ended_epoch, no record, or a live socket/pid).",
        ],
        "could_not_build": [
            "No boot, no leg and no measurement was run: the fixer lane is CPU-only, prod is "
            "serving, and the windows are operator-gated.",
            "The 2x2 verdicts are STATIC (binary literal scan + server argv model). The servers' "
            "drafting chooser was not executed; its no-op outcome for these names is asserted from "
            "the regex in both trees and must be confirmed from the first boot line.",
            "W4's optional third boot per cell (min p 0.10) is planned but not prepared; 2 boots "
            "per cell is what the budget covers.",
            "The W2 pilot deltas are not filled yet: the pilot boots must run first, then "
            "port_ab_legs.py --freeze-pilot W2 writes them here. Claim legs refuse until then.",
            "The helper fill band is derived from the 7,000 MiB cap and the Phase-1 row size, not "
            "measured under this tuple; the first boot's fill is recorded and checked against it.",
        ],
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    (HERE / "port-ab-manifest.sha256").write_text(sha256_path(MANIFEST) + "  port-ab-manifest.json\n")
    return manifest


HARNESS_SCRIPTS = ("port_ab_configs.py", "port_ab_contract.py", "port_ab_prepare.py",
                   "port_ab_legs.py", "port_ab_ttft.py", "port_ab_preflight.py",
                   "port_ab_prereq0.py", "port_ab_boot_plan.py", "port_ab_boot.sh",
                   "port_ab_stop.sh", "port_ab_prune.py")


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
    # every window's template is pinned at its own path with its own sha: the prod-shape one is
    # a separate artifact, so a prod-shape run can never pass by having rewritten the claim one
    templates = manifest.get("request_templates") or {}
    for name, spec in C.REQUEST_TEMPLATES.items():
        row = templates.get(name)
        if not row:
            problems.append("request template " + name + " is not pinned in the manifest")
            continue
        path = pathlib.Path(row["path"])
        if path.name != spec["file"]:
            problems.append("request template " + name + " is " + path.name + ", the contract "
                            "names " + spec["file"])
        if not path.is_file() or sha256_path(path) != row["sha256"]:
            problems.append("request template drift: " + name + " (" + str(path) + ")")
        elif path.read_bytes() != (json.dumps(spec["settings"], indent=1,
                                              sort_keys=True) + "\n").encode():
            problems.append("request template " + name + " does not match the contract's settings")
        if row.get("settings") != spec["settings"]:
            problems.append("request template " + name + " settings drifted from the contract")
    claim_tpl = templates.get("claim") or {}
    if claim_tpl.get("sha256") != manifest["request_template"]["sha256"]:
        problems.append("the claim template's two manifest slots disagree")
    prod_tpl = templates.get("prodshape") or {}
    if prod_tpl.get("sha256") and prod_tpl.get("sha256") == claim_tpl.get("sha256"):
        problems.append("the prod-shape template has the claim template's sha: they are not "
                        "two artifacts")
    for name, path in manifest["harness_scripts"].items():
        if sha256_path(path) != manifest["harness_script_shas"][name]:
            problems.append("harness script changed since prepare: " + name)

    # every scheduled boot has exactly one frozen config, and no two boots share a key
    scheduled = set()
    for window in manifest["windows"]:
        for key in manifest["windows"][window].get("interleave") or []:
            if key in scheduled:
                problems.append("two boots share the key " + key)
            scheduled.add(key)
    for key, row in manifest["configs"].items():
        path = pathlib.Path(row["config"])
        if not path.is_file() or sha256_path(path) != row["sha256"]:
            problems.append("config drift: " + key)
            continue
        cfg = json.loads(path.read_text())
        arm = row.get("arm") or key.split("-")[1]
        window = row.get("window") or key.split("-")[0]
        # a frozen engine-default config (W1d) must prove both reserve flags ABSENT, not carry
        # the tuple's 256/192; an explicit-margins window (W1, W1f, ...) must carry both. The
        # serving identity follows the same rule: a variant window proves the variant's keys,
        # every claim window proves the tuple's (a leaked 2,000-token budget fails here).
        variant = row.get("config_variant", C.config_variant(window)
                          if window in C.WINDOWS else None)
        for p in pc.check_tuple(cfg, arm, window=window, variant=variant):
            problems.append(key + ": " + p)
        if variant:
            if cfg.get("reasoning_budget_tokens") == pc.TUPLE["reasoning_budget_tokens"]:
                problems.append(key + ": config_variant says " + variant + " but the config "
                                "carries the claim tuple's budget")
            if row.get("deltas") is not None:
                # a variant key that MOVED away from the tuple must be recorded as a variant
                # delta (that is the config proving what it changed). A variant key whose value
                # IS the tuple's value (prodshape's reasoning_effort "high") records nothing,
                # because nothing changed - check_tuple above already proves the value it holds.
                seen = {d.get("field") for d in row["deltas"] if d.get("kind") == "variant"}
                want = {k for k, v in pc.VARIANTS[variant].items()
                        if k != "note" and v != pc.TUPLE.get(k)}
                if seen != want:
                    problems.append(key + ": variant delta set " + str(sorted(seen))
                                    + " != the keys the variant actually moves " + str(sorted(want)))
        elif any(d.get("kind") == "variant" for d in (row.get("deltas") or [])):
            problems.append(key + ": a claim config carries variant deltas")
        # a diagnostic-env window (W3s) must differ from its own uninstrumented twin in the
        # env keys it names and NOTHING else: that is what makes its span breakdown a
        # breakdown of the leg the rate was measured on. The trial_note names the window, so
        # the note and the deltas it is made of are excluded from the comparison.
        env_extra = C.window_env(window) if window in C.WINDOWS else {}
        if env_extra:
            want_deltas = {"env/" + k for k in env_extra}
            seen = {d.get("field") for d in (row.get("deltas") or [])
                    if d.get("kind") == "instrument"}
            if seen != want_deltas:
                problems.append(key + ": instrument delta set " + str(sorted(seen))
                                + " != the env this window names " + str(sorted(want_deltas)))
            # rep 0 is the discarded first-touch boot's own key (W1's warmup is r0 too), so
            # it must be passed as 0: "row.get('rep') or 1" would re-derive r1's paths and
            # report the boot's own socket/log/cache as a difference the window moved.
            diffs = pc.instrument_only_difference(cfg, window, arm, out=OUT_DEFAULT,
                                                 rep=(row.get("rep")
                                                      if row.get("rep") is not None else 1),
                                                 boot=row.get("boot_group"))
            moved = {k: v for k, v in diffs.items() if k not in ("env", "trial_note")}
            if moved:
                problems.append(key + ": the diagnostic window moved more than its env "
                                "(fields " + ", ".join(sorted(moved)) + ")")
            env_diff = {k for k in ((diffs.get("env") or {}).get("instrumented") or {})
                        if ((diffs.get("env") or {}).get("instrumented") or {}).get(k)
                        != ((diffs.get("env") or {}).get("plain") or {}).get(k)}
            if env_diff != set(env_extra):
                problems.append(key + ": the diagnostic env moved " + str(sorted(env_diff))
                                + ", the window names " + str(sorted(env_extra)))
    for key in scheduled:
        if key not in manifest["configs"]:
            problems.append("scheduled boot has no frozen config: " + key)

    # budgets are derived, so a drifted derivation is a defect in the manifest
    for window, spec in manifest["windows"].items():
        if spec.get("budget_s") != C.window_budget_s(window):
            problems.append(window + ": budget_s does not match the current derivation")
        if spec.get("contract_budget_s") != C.window_contract_budget_s(window):
            problems.append(window + ": contract_budget_s does not match the contract table")
        if spec.get("interleave") != schedule_interleave(window):
            problems.append(window + ": interleave does not match the schedule")
        # The window's GATE DECISION, not just its schedule: which number it is judged on and
        # against which delta. These were written into the manifest but never re-derived, so a
        # contract change to gate_metric/delta could leave a manifest (and every report that
        # reads it) still judging a window on the old metric. W4's move from gen_tps to read_tps
        # (2026-11-25) is exactly that kind of change, so it is pinned here.
        for field in ("gate_metric", "delta", "delta_note", "metric_note"):
            if spec.get(field) != C.WINDOWS[window].get(field):
                problems.append(window + ": " + field + " in the manifest does not match the "
                                "contract (" + repr(spec.get(field)) + " vs "
                                + repr(C.WINDOWS[window].get(field)) + ")")
        for row in spec.get("schedule") or []:
            if row.get("boot") not in C.WINDOWS[window]["boot_groups"]:
                problems.append(window + ": scheduled boot group " + str(row.get("boot"))
                                + " is unknown")

    band = (manifest.get("frozen_tuple") or {}).get("helper_fill_band") or {}
    if band != C.HELPER_FILL_BAND:
        problems.append("helper fill band is missing or differs from the contract table")
    pilot = ((manifest.get("windows") or {}).get("W2") or {}).get("pilot_deltas")
    if not pilot or "deltas" not in pilot:
        problems.append("W2 pilot delta slots are missing")
    elif pilot.get("frozen"):
        for shape, slots in pilot["deltas"].items():
            for name, value in slots.items():
                if not isinstance(value, (int, float)):
                    problems.append("W2 pilot delta " + shape + "." + name + " is not a number "
                                    "although frozen=true")
    if manifest.get("claim") != C.CLAIM:
        problems.append("claim block drifted from port_ab_contract.CLAIM")
    if manifest["arm_config_equality"]["diffs"]:
        problems.append("arms differ outside the declared structural pair: "
                        + ", ".join(sorted(manifest["arm_config_equality"]["diffs"])))
    blind = manifest["arm_config_equality"]["settings_sha256_blind"]
    if blind.get("A") != blind.get("B"):
        problems.append("settings_sha256_blind differs across arms although the settings are equal")
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
            print("  " + window + ": " + str(spec["budget_h"]) + " h planning"
                  + ("" if spec["contract_budget_h"] is None
                     else " (contract " + str(spec["contract_budget_h"]) + " h)")
                  + ", boots " + json.dumps(spec["boots"]))
        print("  W2 pilot deltas frozen: "
              + str(((m["windows"].get("W2") or {}).get("pilot_deltas") or {}).get("frozen")))
        return 0
    manifest = prepare(a.out_dir, do_preflight=not a.no_preflight, previous_manifest=MANIFEST)
    print("prepared " + str(MANIFEST))
    for arm, entry in manifest["arms"].items():
        print("  " + arm + ": engine " + entry["engine"]["sha256"][:16] + "  helper "
              + entry["helper"]["sha256"][:16] + "  server " + entry["server_tree"]["digest"][:16])
    for window, spec in manifest["windows"].items():
        print("  " + window + ": " + str(spec["budget_h"]) + " h planning  boots "
              + json.dumps(spec["boots"]))
    problems = verify()
    for p in problems:
        print("PROBLEM: " + p)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""port_ab_legs.py - the Phase-2 leg invocations (print, run, or gate-check).

One place that turns the frozen contract into exact commands, so no window script invents a
flag, a byte target or a timeout:
  * --runbook W1            the whole window in boot order (boot + legs + stop per leg)
  * --arm A --rep 1 --run   fire that leg's requests against the ALREADY BOOTED server
  * --check W1              the window's gates over every leg record (0 reused, prompt-N equal
                            across arms, no fault/restart, helper timeouts=0)

The boot itself is port_ab_boot.sh (one boot per leg, fresh cache dir, trial sockets); this
script never stops a boot and never boots one - stop and boot stay separate invocations.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import port_ab_configs as pc
import port_ab_contract as C
import records as ledger

OUT_DEFAULT = pc.OUT_DEFAULT
URL = "http://127.0.0.1:8009/v1/chat/completions"
SERVER_URL = "http://127.0.0.1:8009"
PREREQ0 = HERE / "port_ab_prereq0.py"


def leg_cap(shape, window):
    entry = C.LEGS[shape]["max_tokens"]
    return int(entry) if isinstance(entry, int) else int(entry.get(window) or entry["W2"])


def corpus_path(out, shape):
    return out / "corpus" / (shape + ".ctx" + str(C.SHAPES[shape]["bytes"]) + ".bin")


def template_path(out):
    return out / "request-template.json"


def boot_key(window, arm, rep):
    return window + "-" + arm + "-r" + str(rep)


def engine_pid(exe_path, socket=None):
    """The PID of THIS leg's engine: the arm's exe AND the leg's own socket in its argv.

    Never a first-match scan: prod serves from the same exe path as arm B, so the socket the
    engine was started with is what tells the leg's engine apart from prod's.
    """
    exe_path = str(exe_path)
    want = str(socket) if socket else None
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            exe = os.readlink("/proc/" + entry + "/exe")
        except OSError:
            continue
        if exe != exe_path:
            continue
        try:
            cmd = open("/proc/" + entry + "/cmdline", "rb").read().split(b"\0")
        except OSError:
            continue
        if b"--serve" not in cmd:
            continue
        if want and want.encode() not in cmd:
            continue
        return int(entry)
    return None


def schedule(window):
    """The boots of a window in interleave order, per boot shape.

    W2 carries two boot shapes (a decode boot and a short boot); each is interleaved A B B A A B
    independently, and the pilot/warmup boot (rep 0) comes first for the arm.
    """
    spec = C.WINDOWS[window]
    if window == "W4":
        rows = []
        # two cross cells, two boots each, interleaved: A-cell, B-cell, B-cell, A-cell
        for eng, boot in (("A", 1), ("B", 1), ("B", 2), ("A", 2)):
            other = "B" if eng == "A" else "A"
            rows.append({"group": "cell", "arm": eng, "rep": boot, "server_arm": other,
                         "shapes": list(spec["legs"]), "pilot": False,
                         "cell": eng + "-engine+" + other + "-server"})
        return rows
    groups = spec["legs_per_boot"].get("_boot_shapes") or {"boot": list(spec["legs"])}
    rows = []
    if spec["warmup_boots"] and not spec.get("pilot"):
        for arm in ("A", "B"):
            rows.append({"group": "warmup", "arm": arm, "rep": 0,
                         "shapes": list(spec["legs"]), "pilot": False, "warmup": True})
    if spec.get("pilot"):
        for arm in sorted({a for a in C.INTERLEAVE}):
            rows.append({"group": "pilot", "arm": arm, "rep": 0,
                         "shapes": list(groups[spec["pilot"]["shape"]]), "pilot": True})
    for name, shapes in groups.items():
        for arm, rep in C.INTERLEAVE_PAIRS:
            rows.append({"group": name, "arm": arm, "rep": rep, "shapes": list(shapes),
                         "pilot": False})
    return rows


def commands(out, window, arm, rep, shapes, *, port=8009):
    """The exact commands of one boot's legs, with the engine pid pinned at call time."""
    cfg, _, paths = pc.derive(arm, window=window, rep=rep, out=out, port=port)
    exe = pc.ROOT / pc.ARMS[arm]["engine"]
    pid = engine_pid(exe, cfg["expert_helper_socket"])
    cmds = []
    for shape in shapes:
        if C.LEGS[shape]["kind"] == "ttft":
            cmd = ["python3", str(HERE / "port_ab_ttft.py"),
                   "--arm", arm, "--leg", shape, "--url", SERVER_URL,
                   "--corpus", str(corpus_path(out, "pp2k")),
                   "--turns", str(C.LEGS[shape]["turns"]),
                   "--max-tokens", str(C.LEGS[shape]["max_tokens"]),
                   "--template", str(template_path(out)),
                   "--out", paths["records"], "--elog", cfg["log"], "--hlog", paths["helper_log"]]
            if pid:
                cmd += ["--engine-pid", str(pid)]
            cmds.append({"shape": shape, "cmd": cmd})
            continue
        cap = leg_cap(shape, window)
        corpus = corpus_path(out, shape)
        digest = hashlib_sha(corpus)
        cmd = ["python3", str(HERE / "ab9_driver.py"),
               "--arm", arm, "--leg", shape, "--kind", C.kind_for(shape, window),
               "--ctx", str(C.SHAPES[shape]["bytes"]),
               "--max-tokens", str(cap),
               "--out", paths["records"], "--elog", cfg["log"], "--hlog", paths["helper_log"],
               "--url", URL,
               "--prompt-file", str(corpus), "--prompt-sha256", digest,
               "--request-template", str(template_path(out)),
               "--request-template-sha256", hashlib_sha(template_path(out)),
               "--timeout", "5400",
               "--champs-shape", shape, "--champs-pair", "0", "--champs-measured"]
        if pid:
            cmd += ["--engine-pid", str(pid)]
        cmds.append({"shape": shape, "cmd": cmd})
    return cfg, paths, cmds


def hashlib_sha(path):
    import hashlib
    path = pathlib.Path(path)
    if not path.is_file():
        raise SystemExit("missing frozen artifact: " + str(path) + " (run port_ab_prepare.py --prepare)")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runbook(out, window):
    rows = schedule(window)
    lines = ["# " + window + ": " + C.WINDOWS[window]["name"],
             "# budget " + str([round(x / 3600, 2) for x in C.window_budget_s(window)]) + " h"
             + ("  (derived: " + C.WINDOWS[window].get("delta_note", "pre-registered delta") + ")"
                if C.WINDOWS[window].get("delta_note") else ""),
             "# boot: bench/ab-v0210/rpcprof/port_ab_boot.sh W A R   (prerequisite 0 runs inside it)",
             "# stop: bench/ab-v0210/rpcprof/port_ab_stop.sh W A R"]
    for row in rows:
        key = boot_key(window, row["arm"], row["rep"])
        lines.append("")
        label = ("pilot, excluded from the claim" if row["pilot"] else
                 ("discarded first-touch warmup" if row.get("warmup") else row["group"]))
        lines.append("## " + key + " (" + label + ")")
        extra_arm = (" " + row["server_arm"]) if row.get("server_arm") else ""
        lines.append("bench/ab-v0210/rpcprof/port_ab_boot.sh " + window + " " + row["arm"] + " "
                     + str(row["rep"]) + extra_arm)
        for shape in row["shapes"]:
            cap = leg_cap(shape, window)
            extra = (" --turns " + str(C.LEGS[shape]["turns"])) if C.LEGS[shape]["kind"] == "ttft" else ""
            lines.append("  python3 bench/ab-v0210/rpcprof/port_ab_legs.py --window " + window
                         + " --arm " + row["arm"] + " --rep " + str(row["rep"])
                         + " --shape " + shape + " --run")
            corpus_shape = "pp2k" if shape == "ttft3" else shape
            lines.append("    # " + shape + ": " + str(C.SHAPES[corpus_shape]["bytes"]) + " B (~"
                         + str(C.SHAPES[corpus_shape]["tokens_expected"]) + " tokens), max_tokens "
                         + str(cap) + extra)
        lines.append("bench/ab-v0210/rpcprof/port_ab_stop.sh " + window + " " + row["arm"] + " " + str(row["rep"]))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase-2 leg invocations: print, run, or check gates")
    ap.add_argument("--window", default="W1", choices=["W1", "W2", "W3", "W4"])
    ap.add_argument("--arm", choices=["A", "B"])
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--shape", action="append", help="leg shape (default: the boot group's shapes)")
    ap.add_argument("--out-dir", type=pathlib.Path, default=OUT_DEFAULT)
    ap.add_argument("--port", type=int, default=8009)
    ap.add_argument("--runbook", metavar="WINDOW", help="print the whole window plan and exit")
    ap.add_argument("--run", action="store_true", help="execute the leg command(s)")
    ap.add_argument("--check", metavar="WINDOW", help="gate-check every leg record of a window")
    a = ap.parse_args()

    if a.runbook:
        print(runbook(a.out_dir, a.runbook))
        return 0
    if a.check:
        return check(a.out_dir, a.check)
    if not a.arm:
        ap.error("--arm is required unless --runbook/--check is given")

    shapes = a.shape or next((row["shapes"] for row in schedule(a.window)
                              if row["arm"] == a.arm and row["rep"] == a.rep), None)
    if not shapes:
        ap.error("no boot scheduled for " + a.window + " " + a.arm + " r" + str(a.rep))
    cfg, paths, cmds = commands(a.out_dir, a.window, a.arm, a.rep, shapes, port=a.port)
    leg_cfg, _, _ = pc.derive(a.arm, window=a.window, rep=a.rep, out=a.out_dir, port=a.port)
    if not engine_pid(pc.ROOT / pc.ARMS[a.arm]["engine"], leg_cfg["expert_helper_socket"]):
        print("WARNING: no engine for " + a.arm + " holds this leg's socket "
              + leg_cfg["expert_helper_socket"] + "; the commands will fail until it boots",
              file=sys.stderr)
    failed = 0
    for item in cmds:
        print("# " + item["shape"] + "  (" + paths["key"] + ")", flush=True)
        print(" ".join(item["cmd"]), flush=True)
        if not a.run:
            continue
        out_dir = a.out_dir / "legs"
        out_dir.mkdir(parents=True, exist_ok=True)
        log = out_dir / (paths["key"] + "-" + item["shape"] + ".out")
        with log.open("wb") as sink:
            code = subprocess.call(item["cmd"], stdout=sink, stderr=subprocess.STDOUT)
        print("  exit " + str(code) + " (" + str(log) + ")", flush=True)
        if code != 0:
            failed = 1
    return failed


# ------------------------------------------------------------------------------- gate check
JUDGE_TAIL = re.compile(C.EXPECTED_LINES["judge_tail"])


def slice_text(rec):
    elog = pathlib.Path(rec.get("elog", ""))
    if not elog.is_file():
        return ""
    with elog.open("rb") as f:
        f.seek(int(rec.get("elog_start") or 0))
        blob = f.read(max(0, int(rec.get("elog_end") or 0) - int(rec.get("elog_start") or 0)))
    return blob.replace(b"\0", b"").decode("utf-8", "replace")


def check(out, window):
    rows, problems = [], []
    recdirs = sorted(p for p in (out / "records").glob(window + "-*") if p.is_dir())
    if not recdirs:
        print("no records under " + str(out / "records") + " for " + window)
        return 2
    by_shape = {}
    for d in recdirs:
        for jf in sorted(d.glob("*.json")):
            rec = json.loads(jf.read_text())
            arm, leg = rec.get("arm"), rec.get("leg")
            if rec.get("kind") == "ttft":
                turns = rec.get("turn_rows") or []
                ttft = [t.get("ttft_client_s") for t in turns]
                rows.append({"shape": leg, "arm": arm, "boot": d.name, "kind": "ttft",
                             "ttft_s": ttft, "wall_s": [t.get("wall_s") for t in turns],
                             "inter_turn_s": [t.get("inter_turn_wall_s") for t in turns],
                             "reused": 0, "prompt_tok": None,
                             "mem": rec.get("mem") or {}})
                if any(x is None for x in ttft):
                    problems.append(d.name + "/" + leg + ": a turn never produced a first token")
                continue
            metrics = ledger.leg_metrics(rec)
            text = slice_text(rec)
            judge = next(iter(metrics.values()), [])
            row = judge[0] if judge else None
            decode = judge[1] if len(judge) > 1 else None
            tail = JUDGE_TAIL.search(text)
            entry = {"shape": leg, "arm": arm, "boot": d.name, "kind": rec.get("kind"),
                     "prompt_tok": row and row["prompt_tok"], "reused": row and row["reused"],
                     "read_tps": row and row["read_tps"],
                     "gen_tps": (decode or row) and (decode or row)["gen_tps"],
                     "gen_tok": row and row["gen_tok"],
                     "decode_from_request": 2 if decode else 1,
                     "reused_second": decode and decode["reused"],
                     "helper": tail.groups() if tail else None,
                     "non_finite": len(re.findall(C.EXPECTED_LINES["non_finite"], text)),
                     "gpu_fault": len(re.findall(C.EXPECTED_LINES["gpu_fault"], text)),
                     "mem": rec.get("mem") or {}}
            rows.append(entry)
            by_shape.setdefault(leg, {}).setdefault(arm, []).append(entry["prompt_tok"])
            if row is None:
                problems.append(d.name + "/" + leg + ": no judge line")
            else:
                if row["reused"] != 0:
                    problems.append(d.name + "/" + leg + ": reused " + str(row["reused"]) + " != 0")
                if decode is not None and decode["reused"] in (0, None):
                    problems.append(d.name + "/" + leg + ": request 2 was not served from the "
                                    "cache (reused 0): the decode leg did not measure a warm re-run")
                expected = C.SHAPES.get(leg, {}).get("tokens_expected")
                if expected and row["prompt_tok"] < expected * 0.9:
                    problems.append(d.name + "/" + leg + ": served " + str(row["prompt_tok"])
                                    + " tokens, expected ~" + str(expected))
            if tail and (tail.group(7) != "0" or tail.group(8) != "0"):
                problems.append(d.name + "/" + leg + ": helper timeouts=" + tail.group(7)
                                + " fallbacks=" + tail.group(8))
            if entry["non_finite"]:
                problems.append(d.name + "/" + leg + ": non-finite logits")
            if entry["gpu_fault"]:
                problems.append(d.name + "/" + leg + ": GPU memory-access fault in the slice")
    for shape, arms in by_shape.items():
        values = {a: sorted(set(v)) for a, v in arms.items()}
        if len({tuple(v) for v in values.values()}) > 1:
            problems.append(shape + ": served prompt-N differs across arms: " + str(values))

    print(("%-8s %-2s %-22s %8s %6s %8s %8s %7s" % ("shape", "arm", "boot", "promptN", "reused",
                                                    "read_tps", "gen_tps", "RSS MiB")))
    for r in sorted(rows, key=lambda x: (x["shape"], x["boot"])):
        rss = (r["mem"] or {}).get("max_rss_mib")
        print(("%-8s %-2s %-22s %8s %6s %8s %8s %7s" % (
            r["shape"], r["arm"], r["boot"], r.get("prompt_tok") or "-", r.get("reused") if r.get("reused") is not None else "-",
            r.get("read_tps") or (r.get("ttft_s") and "ttft " + str(r["ttft_s"][0])) or "-",
            r.get("gen_tps") or "-", rss or "-")))
    for p in problems:
        print("FAIL: " + p)
    print("VERDICT: " + ("PASS" if not problems else str(len(problems)) + " problem(s)"))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())

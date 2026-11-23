#!/usr/bin/env python3
"""port_ab_legs.py - the Phase-2 leg invocations (print, run, gate-check, freeze pilot).

One place that turns the frozen contract into exact commands, so no window script invents a
flag, a byte target or a timeout:
  * --runbook W1              the whole window in boot order (boot + legs + stop per boot)
  * --window W2 --arm A --rep 1 --shape dec144k --run
                              fire that leg against the ALREADY BOOTED server
  * --check W1                the window's gates over every leg record + boot record, and
                              the per-boot evidence written beside each boot record
  * --bookend KEY             (port_ab_stop.sh) record ended_epoch + final generation in
                              the boot record, so the kernel slice ends with the boot
  * --freeze-pilot W2         compute the pilot deltas from the pilot records and write them
                              into the manifest (the claim legs refuse until this is done)

The boot itself is port_ab_boot.sh (one boot per leg group, fresh cache dir, trial sockets);
this script never stops a boot and never boots one - stop and boot stay separate invocations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import statistics
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import port_ab_configs as pc
import port_ab_contract as C
import records as ledger

OUT_DEFAULT = pc.OUT_DEFAULT
URL = "http://127.0.0.1:8009/v1/chat/completions"
SERVER_URL = "http://127.0.0.1:8009"
MANIFEST = HERE / "port-ab-manifest.json"


def leg_cap(shape, window):
    entry = C.LEGS[shape]["max_tokens"]
    return int(entry) if isinstance(entry, int) else int(entry.get(window) or entry["W2"])


def corpus_path(out, shape):
    """The frozen corpus file of a leg OR of a bare corpus shape (ttft2k is only a shape)."""
    shape = shape if shape in C.SHAPES else C.corpus_shape(shape)
    return out / "corpus" / (shape + ".ctx" + str(C.SHAPES[shape]["bytes"]) + ".bin")


def template_path(out):
    return out / "request-template.json"


def hashlib_sha(path):
    path = pathlib.Path(path)
    if not path.is_file():
        raise SystemExit("missing frozen artifact: " + str(path)
                         + " (run port_ab_prepare.py --prepare)")
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    """The boots of a window in interleave order, per boot group.

    Every row names its boot group, because the group is part of the boot key. W2's short
    group pilot (rep 0) and decode warmup (rep 0) come first; the claim boots follow,
    interleaved A B B A A B inside each group.
    """
    spec = C.WINDOWS[window]
    rows = []
    if window == "W4":
        # two cross cells, two boots each, interleaved: A-cell, B-cell, B-cell, A-cell
        for eng, boot in (("A", 1), ("B", 1), ("B", 2), ("A", 2)):
            other = "B" if eng == "A" else "A"
            rows.append({"group": "cell", "boot": pc.default_boot(window), "arm": eng, "rep": boot,
                         "server_arm": other, "shapes": list(spec["boot_groups"][pc.default_boot(window)]),
                         "pilot": False, "cell": eng + "-engine+" + other + "-server"})
        return rows
    groups = spec["boot_groups"]
    pilot = spec.get("pilot")
    warm = spec.get("decode_warmup")
    if pilot:
        for arm in ("A", "B"):
            rows.append({"group": "pilot", "boot": pilot["group"], "arm": arm, "rep": 0,
                         "shapes": list(groups[pilot["group"]]), "pilot": True})
    if warm:
        for arm in ("A", "B"):
            rows.append({"group": "warmup", "boot": warm["group"], "arm": arm, "rep": 0,
                         "shapes": list(groups[warm["group"]]), "pilot": False, "warmup": True})
    elif spec["warmup_boots"]:
        for arm in ("A", "B"):
            boot = next(iter(groups))
            rows.append({"group": "warmup", "boot": boot, "arm": arm, "rep": 0,
                         "shapes": list(groups[boot]), "pilot": False, "warmup": True})
    for name, shapes in groups.items():
        for arm, rep in C.INTERLEAVE_PAIRS:
            rows.append({"group": name, "boot": name, "arm": arm, "rep": rep,
                         "shapes": list(shapes), "pilot": False})
    return rows


def commands(out, window, arm, rep, shapes, *, port=8009, boot=None):
    """The exact commands of one boot's legs, with the engine pid pinned at call time."""
    boot = boot or pc.boot_for_shape(window, shapes[0])
    cfg, _, paths = pc.derive(arm, window=window, rep=rep, out=out, port=port, boot=boot)
    exe = pc.ROOT / pc.ARMS[arm]["engine"]
    pid = engine_pid(exe, cfg["expert_helper_socket"])
    cmds = []
    for shape in shapes:
        if C.LEGS[shape]["kind"] == "ttft":
            cmd = ["python3", str(HERE / "port_ab_ttft.py"),
                   "--arm", arm, "--leg", shape, "--url", SERVER_URL,
                   "--corpus", str(corpus_path(out, shape)),
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
        cmd = ["python3", str(HERE / "ab9_driver.py"),
               "--arm", arm, "--leg", shape, "--kind", C.kind_for(shape, window),
               "--ctx", str(C.SHAPES[C.corpus_shape(shape)]["bytes"]),
               "--max-tokens", str(cap),
               "--out", paths["records"], "--elog", cfg["log"], "--hlog", paths["helper_log"],
               "--url", URL,
               "--prompt-file", str(corpus), "--prompt-sha256", hashlib_sha(corpus),
               "--request-template", str(template_path(out)),
               "--request-template-sha256", hashlib_sha(template_path(out)),
               "--timeout", "5400",
               "--champs-shape", shape, "--champs-pair", "0", "--champs-measured"]
        if pid:
            cmd += ["--engine-pid", str(pid)]
        cmds.append({"shape": shape, "cmd": cmd})
    return cfg, paths, cmds


def runbook(out, window):
    rows = schedule(window)
    budget = C.window_budget_s(window)
    contract = C.window_contract_budget_s(window)
    lines = ["# " + window + ": " + C.WINDOWS[window]["name"],
             "# planning budget " + str([round(x / 3600, 2) for x in budget]) + " h (derived, "
             + "includes the discarded boots)"
             + (("  [contract: " + str([round(x / 3600, 2) for x in contract])
                 + " h, claim boots only]") if contract else "  [the contract had no budget here]"),
             "# boot: port_ab_boot.sh WINDOW ARM BOOT REP [--server-arm A|B]",
             "# stop: port_ab_stop.sh WINDOW ARM BOOT REP"]
    for row in rows:
        key = pc.boot_key(window, row["arm"], row["rep"], row["boot"])
        label = ("pilot, excluded from the claim" if row["pilot"] else
                 ("discarded first-touch warmup" if row.get("warmup") else row["group"]))
        lines.append("")
        lines.append("## " + key + " (" + label + ")")
        extra = (" --server-arm " + row["server_arm"]) if row.get("server_arm") else ""
        lines.append("bench/ab-v0210/rpcprof/port_ab_boot.sh " + window + " " + row["arm"] + " "
                     + row["boot"] + " " + str(row["rep"]) + extra)
        for shape in row["shapes"]:
            cap = leg_cap(shape, window)
            extra_turns = (" --turns " + str(C.LEGS[shape]["turns"])) if C.LEGS[shape]["kind"] == "ttft" else ""
            lines.append("  python3 bench/ab-v0210/rpcprof/port_ab_legs.py --window " + window
                         + " --arm " + row["arm"] + " --boot " + row["boot"]
                         + " --rep " + str(row["rep"]) + " --shape " + shape + " --run")
            cs = C.corpus_shape(shape)
            lines.append("    # " + shape + ": " + str(C.SHAPES[cs]["bytes"]) + " B (~"
                         + str(C.SHAPES[cs]["tokens_expected"]) + " tokens), max_tokens "
                         + str(cap) + extra_turns)
        lines.append("bench/ab-v0210/rpcprof/port_ab_stop.sh " + window + " " + row["arm"] + " "
                     + row["boot"] + " " + str(row["rep"]))
    return "\n".join(lines)


# ------------------------------------------------------------------------ boot evidence
def load_manifest(path=None):
    path = pathlib.Path(path or MANIFEST)
    if not path.is_file():
        raise SystemExit("missing manifest " + str(path) + " (run port_ab_prepare.py --prepare)")
    return json.loads(path.read_text())


def write_manifest(manifest, path=None):
    path = pathlib.Path(path or MANIFEST)
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    path.with_name("port-ab-manifest.sha256").write_text(
        hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n")


def pilot_frozen(manifest):
    """True only when every W2 pilot delta slot carries a number."""
    pilot = ((manifest.get("windows") or {}).get("W2") or {}).get("pilot_deltas") or {}
    if not pilot.get("frozen"):
        return False
    for shape, slots in (pilot.get("deltas") or {}).items():
        for name, value in (slots or {}).items():
            if not isinstance(value, (int, float)):
                return False
    return True


def page_cache_now():
    cached = None
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("Cached:"):
                cached = int(line.split()[1])
                break
    majfault = None
    with open("/proc/vmstat") as f:
        for line in f:
            if line.startswith("pgmajfault "):
                majfault = int(line.split()[1])
                break
    return {"cached_mib": None if cached is None else cached // 1024, "pgmajfault": majfault,
            "ts": round(time.time(), 3)}


def boot_json(out, key):
    path = pathlib.Path(out) / "boots" / (key + ".json")
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None


def next_boot_start(out, key, started):
    """The earliest started_epoch of another boot that began after this one (None if none)."""
    best = None
    for path in sorted((pathlib.Path(out) / "boots").glob("*.json")):
        if path.name.endswith(".evidence.json") or path.stem == key:
            continue
        try:
            stamp = json.loads(path.read_text()).get("started_epoch")
        except (OSError, ValueError):
            continue
        if isinstance(stamp, (int, float)) and stamp > started and (best is None or stamp < best):
            best = stamp
    return best


def boot_window_end(out, key, record):
    """Where this boot's kernel-journal slice ends: its stop time, else the next boot's start.

    Never time.time(): at window end every fault of every later boot would be attributed to
    all earlier boots (a false FAIL). port_ab_stop.sh writes ended_epoch into the boot record;
    a record without one is bounded by the next boot's start, and the last unterminated boot
    by its own start - an empty window, which can miss a fault but can never invent one.
    """
    started = record.get("started_epoch") or 0
    ended = record.get("ended_epoch")
    if isinstance(ended, (int, float)) and ended > 0:
        return float(ended), "ended_epoch"
    later = next_boot_start(out, key, started)
    if later is not None:
        return float(later), "next boot start"
    return float(started), "no end recorded"


def supervisor_generation(out, key):
    path = pathlib.Path(out) / "run" / (key + ".supervisor.json")
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text()).get("generation")
    except ValueError:
        return None


SUPERVISOR_RESTART = re.compile(r"recover|restart|exited")


def supervisor_events(out, key):
    """Restart evidence from the supervisor's own events log.

    pair_supervisor.append_event writes state.with_name(state.stem + "-events.log"), and the
    state file is run/<key>.supervisor.json - so the log is run/<key>.supervisor-events.log,
    not run/<key>-events.log. Only recover/restart/exited lines count: a normal shutdown
    appends "stopped: stop requested" on every clean stop and must never fail a boot.
    """
    path = pathlib.Path(out) / "run" / (key + ".supervisor-events.log")
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        if SUPERVISOR_RESTART.search(line):
            rows.append(line.strip()[:200])
    return rows


def argv_flags(text):
    """flag -> value (or True) from an engine argv line. Order is never assumed."""
    flags = {}
    tokens = text.split()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("--"):
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                flags[token] = tokens[i + 1]
                i += 2
            else:
                flags[token] = True
                i += 1
            continue
        i += 1
    return flags


def boot_prefix(rec):
    """The engine log from byte 0 to the leg's start: every boot line is BEFORE the slice.

    The judge lines live in [elog_start, elog_end); the 'engine started:' argv, the helper
    fill and the FORESIGHT lines live before it. A gate that reads only the slice can never
    see the tuple it is supposed to check (the defect the oracle demonstrated).
    """
    elog = pathlib.Path(rec.get("elog", ""))
    if not elog.is_file():
        return ""
    with elog.open("rb") as f:
        blob = f.read(max(0, int(rec.get("elog_start") or 0)))
    return blob.replace(b"\0", b"").decode("utf-8", "replace")


def slice_text(rec):
    elog = pathlib.Path(rec.get("elog", ""))
    if not elog.is_file():
        return ""
    with elog.open("rb") as f:
        f.seek(int(rec.get("elog_start") or 0))
        blob = f.read(max(0, int(rec.get("elog_end") or 0) - int(rec.get("elog_start") or 0)))
    return blob.replace(b"\0", b"").decode("utf-8", "replace")


TUPLE_ARGV = ("--spec", "--vram-reserve-mib", "--vram-reserve-later-mib", "--dflash-block",
              "--expert-helper-slots", "--layer-split")
FAULT_CLASS = (r"amdgpu .*(reset|BACO|hang|timeout|GPU reset)|KFD.*(quiesce|reset)|"
               r"device wedged|ring .* timeout|Memory access fault by GPU")


def kernel_fingerprint(since, until):
    """Read-only kernel journal slice for the boot window; no process is touched."""
    try:
        proc = subprocess.run(["journalctl", "-k", "--since", "@" + str(int(since)),
                               "--until", "@" + str(int(until)), "--no-pager", "-q", "-o", "cat"],
                              capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError) as error:
        return {"available": False, "reason": str(error), "matches": [], "count": 0}
    if proc.returncode != 0:
        return {"available": False, "reason": (proc.stderr or "journalctl exit " + str(proc.returncode)).strip()[:200],
                "matches": [], "count": 0}
    matches = [line for line in proc.stdout.splitlines() if re.search(FAULT_CLASS, line)]
    return {"available": True, "reason": "", "matches": [m[:200] for m in matches],
            "count": len(matches)}


def boot_checks(out, key, rec):
    """Everything the boot lines and the boot record must prove, computed once per boot."""
    prefix = boot_prefix(rec)
    record = boot_json(out, key) or {}
    checks = {"key": key, "problems": [], "warnings": [], "tuple": None, "fill": None,
              "restart": None, "low": 0, "fs_slots": [], "cache_slots": None,
              "fingerprint": None, "page_cache": None}

    started = [line for line in prefix.splitlines() if C.EXPECTED_LINES["boot_started"] in line]
    if not started:
        checks["problems"].append(key + ": no 'engine started:' line in the boot prefix")
    else:
        flags = argv_flags(started[-1].split("engine started:", 1)[1])
        checks["tuple"] = {flag: flags.get(flag) for flag in TUPLE_ARGV}
        expected = {"--spec": str(pc.TUPLE["spec"]),
                    "--vram-reserve-mib": str(pc.TUPLE["vram_reserve_mib"]),
                    "--vram-reserve-later-mib": str(pc.TUPLE["vram_reserve_later_mib"]),
                    "--dflash-block": str(pc.TUPLE["dflash_block"]),
                    "--expert-helper-slots": str(pc.TUPLE["helper_slots"]),
                    "--layer-split": str(pc.TUPLE["layer_split"])}
        for flag, want in expected.items():
            got = flags.get(flag)
            if str(got) != want:
                checks["problems"].append(key + ": engine argv " + flag + "=" + repr(got)
                                          + ", tuple says " + want)

    fills = re.findall(C.EXPECTED_LINES["boot_helper"], prefix)
    if not fills:
        checks["problems"].append(key + ": no helper fill line in the boot prefix")
    else:
        rows, gib = int(fills[-1][0]), float(fills[-1][1])
        checks["fill"] = {"rows": rows, "gib": gib}
        band = C.HELPER_FILL_BAND
        if not (band["rows"][0] <= rows <= band["rows"][1]):
            checks["problems"].append(key + ": helper fill " + str(rows) + " rows outside the "
                                      "frozen band " + str(band["rows"]))
        if not (band["gib"][0] <= gib <= band["gib"][1]):
            checks["problems"].append(key + ": helper fill " + str(gib) + " GiB outside the "
                                      "frozen band " + str(band["gib"]))

    generation = supervisor_generation(out, key)
    events = supervisor_events(out, key)
    restarts = (generation - 1) if isinstance(generation, int) and generation > 1 else 0
    matches = len(re.findall(C.EXPECTED_LINES["restart"], prefix))
    checks["restart"] = {"generation": generation, "restarts": restarts,
                         "supervisor_events": events, "engine_lines": matches}
    if restarts or events or matches:
        checks["problems"].append(key + ": boot contains a restart "
                                  "(generation " + str(generation) + ", "
                                  + str(len(events)) + " supervisor event(s), "
                                  + str(matches) + " engine line(s))")

    checks["low"] = len(re.findall(C.EXPECTED_LINES["low_warning"], prefix))
    checks["fs_slots"] = [int(m.group(1)) for m in re.finditer(C.EXPECTED_LINES["boot_fs"], prefix)]
    cache = re.search(C.EXPECTED_LINES["boot_cache"], prefix)
    checks["cache_slots"] = int(cache.group(1)) if cache else None
    if cache is None:
        checks["problems"].append(key + ": no 'expert cache N slots' line in the boot prefix")

    faults = len(re.findall(C.EXPECTED_LINES["gpu_fault"], prefix))
    if faults:
        checks["problems"].append(key + ": " + str(faults) + " GPU memory-access fault line(s)")

    if record:
        since = record.get("started_epoch")
        if since:
            until, bound_by = boot_window_end(out, key, record)
            checks["fingerprint"] = kernel_fingerprint(since, until)
            checks["fingerprint"]["window"] = {"since": since, "until": until, "bound_by": bound_by}
            if bound_by == "no end recorded":
                checks["warnings"].append(key + ": boot record has no ended_epoch and no later "
                                          "boot started, so the kernel fingerprint window is empty")
            if checks["fingerprint"].get("count"):
                checks["problems"].append(key + ": kernel journal fingerprint is not clean: "
                                          + checks["fingerprint"]["matches"][0])
        start_cache = record.get("page_cache") or {}
        now = page_cache_now()
        checks["page_cache"] = {"cached_mib_at_boot": start_cache.get("cached_mib"),
                                "cached_mib_now": now["cached_mib"],
                                "cached_delta_mib": (None if not start_cache.get("cached_mib")
                                                     or now["cached_mib"] is None
                                                     else now["cached_mib"] - start_cache["cached_mib"]),
                                "majfault_delta": (None if start_cache.get("pgmajfault") is None
                                                   or now["pgmajfault"] is None
                                                   else now["pgmajfault"] - start_cache["pgmajfault"])}
    else:
        checks["warnings"].append(key + ": no boot record under boots/ (tuple checked from the log)")
    return checks


def bookend(out, key):
    """Record where a boot ended: ended_epoch and the final supervisor generation.

    port_ab_stop.sh calls this at stop time, after the pair is gone. The kernel-journal
    fingerprint is then bounded by the boot's own end instead of by the window's end. The
    first stop wins: a repeated stop script must not move a boot's end forward.
    """
    path = pathlib.Path(out) / "boots" / (key + ".json")
    if not path.is_file():
        print("bookend: no boot record " + str(path) + " (was this boot recorded?)",
              file=sys.stderr)
        return 1
    try:
        record = json.loads(path.read_text())
    except ValueError as error:
        print("bookend: " + str(path) + " is not JSON: " + str(error), file=sys.stderr)
        return 1
    if "ended_epoch" not in record:
        record["ended_epoch"] = round(time.time(), 3)
        record["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    record["final_generation"] = supervisor_generation(out, key)
    record["ended_by"] = "port_ab_stop.sh"
    temp = path.with_suffix(path.suffix + ".new")
    temp.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    temp.replace(path)
    print("bookend: " + key + " ended_epoch " + str(record["ended_epoch"])
          + ", final generation " + str(record["final_generation"]))
    return 0


def write_boot_evidence(out, key, checks):
    """Persist the computed per-boot evidence beside the boot record (boots/<key>.evidence.json).

    The journal's claim is that every boot carries the page-cache proxy, the kernel-journal
    fingerprint, the LOW count, the granted FS slots, the cache slot count and the restart
    flag. check() computes them; without this they die with the console.
    """
    evidence = {
        "key": key, "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "written_by": "port_ab_legs.py --check",
        "kernel_fingerprint": checks["fingerprint"], "page_cache": checks["page_cache"],
        "low_warning_count": checks["low"], "granted_fs_slots": checks["fs_slots"],
        "cache_slots": checks["cache_slots"], "restart": checks["restart"],
        "fill": checks["fill"], "tuple": checks["tuple"],
        "problems": checks["problems"], "warnings": checks["warnings"],
    }
    path = pathlib.Path(out) / "boots" / (key + ".evidence.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".new")
    temp.write_text(json.dumps(evidence, indent=1, sort_keys=True) + "\n")
    temp.replace(path)
    return path


# ------------------------------------------------------------------------------- gate check
JUDGE_TAIL = re.compile(C.EXPECTED_LINES["judge_tail"])


def parse_boot_key(key):
    """W2-A-sht-r1 -> (window, arm, boot, rep)."""
    m = re.match(r"^(W\d)-([AB])-([a-z]+)-r(\d+)$", key)
    return m.groups() if m else None


def check(out, window, manifest_path=None):
    manifest = load_manifest(manifest_path)
    frozen = pilot_frozen(manifest)
    rows, problems, warnings = [], [], []
    boot_seen = {}
    recdirs = sorted(p for p in (out / "records").glob(window + "-*") if p.is_dir())
    if not recdirs:
        print("no records under " + str(out / "records") + " for " + window)
        return 2
    by_shape = {}
    fills_by_group = {}
    for d in recdirs:
        parsed = parse_boot_key(d.name)
        for jf in sorted(d.glob("*.json")):
            rec = json.loads(jf.read_text())
            arm, leg = rec.get("arm"), rec.get("leg")
            bkey = d.name
            if bkey not in boot_seen:
                boot_seen[bkey] = boot_checks(out, bkey, rec)
                write_boot_evidence(out, bkey, boot_seen[bkey])
                problems.extend(boot_seen[bkey]["problems"])
                warnings.extend(boot_seen[bkey]["warnings"])
                fill = boot_seen[bkey]["fill"]
                if fill and parsed:
                    group = parsed[2]
                    fills_by_group.setdefault(group, {}).setdefault(arm, []).append(fill["rows"])
            boot = boot_seen[bkey]
            if rec.get("kind") == "ttft":
                turns = rec.get("turn_rows") or []
                ttft = [t.get("ttft_client_s") for t in turns]
                rows.append({"shape": leg, "arm": arm, "boot": bkey, "kind": "ttft",
                             "ttft_s": ttft, "wall_s": [t.get("wall_s") for t in turns],
                             "inter_turn_s": [t.get("inter_turn_wall_s") for t in turns],
                             "reused": 0, "prompt_tok": None, "restart": boot["restart"],
                             "low": boot["low"], "mem": rec.get("mem") or {}})
                if any(x is None for x in ttft):
                    problems.append(bkey + "/" + leg + ": a turn never produced a first token")
                continue
            metrics = ledger.leg_metrics(rec)
            text = slice_text(rec)
            judge = next(iter(metrics.values()), [])
            row = judge[0] if judge else None
            decode = judge[1] if len(judge) > 1 else None
            tail = JUDGE_TAIL.search(text)
            entry = {"shape": leg, "arm": arm, "boot": bkey, "kind": rec.get("kind"),
                     "prompt_tok": row and row["prompt_tok"], "reused": row and row["reused"],
                     "read_tps": row and row["read_tps"],
                     "gen_tps": (decode or row) and (decode or row)["gen_tps"],
                     "gen_tok": row and row["gen_tok"],
                     "decode_from_request": 2 if decode else 1,
                     "reused_second": decode and decode["reused"],
                     "helper": tail.groups() if tail else None,
                     "non_finite": len(re.findall(C.EXPECTED_LINES["non_finite"], text)),
                     "gpu_fault": len(re.findall(C.EXPECTED_LINES["gpu_fault"], text)),
                     "restart": boot["restart"], "low": boot["low"],
                     "fs_slots": boot["fs_slots"], "cache_slots": boot["cache_slots"],
                     "fill": boot["fill"], "fingerprint": boot["fingerprint"],
                     "page_cache": boot["page_cache"], "tuple": boot["tuple"],
                     "mem": rec.get("mem") or {}}
            rows.append(entry)
            by_shape.setdefault(leg, {}).setdefault(arm, []).append(entry["prompt_tok"])
            if row is None:
                problems.append(bkey + "/" + leg + ": no judge line")
            else:
                if row["reused"] != 0:
                    problems.append(bkey + "/" + leg + ": reused " + str(row["reused"]) + " != 0")
                if decode is not None and decode["reused"] in (0, None):
                    problems.append(bkey + "/" + leg + ": request 2 was not served from the "
                                    "cache (reused 0): the decode leg did not measure a warm re-run")
                expected = C.SHAPES.get(C.corpus_shape(leg), {}).get("tokens_expected")
                if expected and row["prompt_tok"] < expected * 0.9:
                    problems.append(bkey + "/" + leg + ": served " + str(row["prompt_tok"])
                                    + " tokens, expected ~" + str(expected))
            if tail and (tail.group(7) != "0" or tail.group(8) != "0"):
                problems.append(bkey + "/" + leg + ": helper timeouts=" + tail.group(7)
                                + " fallbacks=" + tail.group(8))
            if entry["non_finite"]:
                problems.append(bkey + "/" + leg + ": non-finite logits")

    # claim legs must never run on an unfrozen pilot delta
    if window == "W2" and not frozen:
        claim = [r for r in rows if parse_boot_key(r["boot"]) and int(parse_boot_key(r["boot"])[3]) > 0]
        if claim:
            problems.append("W2 claim record(s) exist while the pilot deltas are NOT frozen in the "
                            "manifest (port_ab_legs.py --freeze-pilot W2 first)")

    for group, by_arm in fills_by_group.items():
        if len(by_arm) < 2:
            continue
        lo = min(min(v) for v in by_arm.values())
        hi = max(max(v) for v in by_arm.values())
        if lo and (hi - lo) / lo > C.HELPER_FILL_BAND["arm_drift_max"]:
            problems.append("group " + group + ": helper fill drifts across arms by "
                            + str(round(100.0 * (hi - lo) / lo, 1)) + " % (limit "
                            + str(100 * C.HELPER_FILL_BAND["arm_drift_max"]) + " %)")
    for shape, arms in by_shape.items():
        values = {a: sorted(set(v)) for a, v in arms.items()}
        if len({tuple(v) for v in values.values()}) > 1:
            problems.append(shape + ": served prompt-N differs across arms: " + str(values))

    header = ("%-8s %-2s %-16s %8s %6s %8s %8s %5s %5s %4s" %
              ("shape", "arm", "boot", "promptN", "reused", "read_tps", "gen_tps", "fill",
               "slots", "LOW"))
    print(header)
    for r in sorted(rows, key=lambda x: (x["shape"], x["boot"])):
        fill = r.get("fill") or {}
        print(("%-8s %-2s %-16s %8s %6s %8s %8s %5s %5s %4s" % (
            r["shape"], r["arm"], r["boot"], r.get("prompt_tok") or "-",
            r.get("reused") if r.get("reused") is not None else "-",
            r.get("read_tps") or (r.get("ttft_s") and "ttft " + str(r["ttft_s"][0])) or "-",
            r.get("gen_tps") or "-", fill.get("rows") or "-", r.get("cache_slots") or "-",
            r.get("low", "-"))))
    for r in sorted(rows, key=lambda x: (x["shape"], x["boot"])):
        if r.get("fingerprint") and r["fingerprint"].get("available") is False:
            warnings.append(r["boot"] + ": kernel fingerprint unavailable: "
                            + str(r["fingerprint"].get("reason")))
    for w in warnings:
        print("WARN: " + w)
    for p in problems:
        print("FAIL: " + p)
    print("VERDICT: " + ("PASS" if not problems else str(len(problems)) + " problem(s)"))
    return 0 if not problems else 1


def _mean(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return statistics.fmean(values) if values else None


def freeze_pilot(out, window="W2", manifest_path=None):
    """Compute the pilot deltas from the pilot records and freeze them in the manifest."""
    manifest = load_manifest(manifest_path)
    if window != "W2":
        raise SystemExit("--freeze-pilot is only defined for W2 (the window with a pilot)")
    samples = {}
    for arm in ("A", "B"):
        pilot_dir = pathlib.Path(out) / "records" / pc.boot_key(window, arm, 0,
                                                               C.WINDOWS[window]["pilot"]["group"])
        if not pilot_dir.is_dir():
            raise SystemExit("missing pilot records for arm " + arm + ": " + str(pilot_dir))
        for jf in sorted(pilot_dir.glob("*.json")):
            rec = json.loads(jf.read_text())
            leg = rec.get("leg")
            if rec.get("kind") == "ttft":
                turns = rec.get("turn_rows") or []
                samples.setdefault("ttft3", {}).setdefault(arm, {}).update(
                    {"ttft_turn1_s": turns[0].get("ttft_client_s") if turns else None,
                     "inter_turn_s": _mean([t.get("inter_turn_wall_s") for t in turns[1:]])})
                continue
            metrics = ledger.leg_metrics(rec)
            judge = next(iter(metrics.values()), [])
            if not judge:
                raise SystemExit(str(jf) + ": no judge line in the pilot record")
            samples.setdefault(leg, {}).setdefault(arm, {}).update(
                {"read_tps": judge[0]["read_tps"], "gen_tps": judge[0]["gen_tps"]})
    deltas, problems = {}, []
    for shape, arm_values in samples.items():
        a, b = arm_values.get("A") or {}, arm_values.get("B") or {}
        deltas[shape] = {}
        for name in sorted(set(a) | set(b)):
            av, bv = a.get(name), b.get(name)
            if not isinstance(av, (int, float)) or not isinstance(bv, (int, float)) or av == 0:
                problems.append(shape + "." + name + ": no usable pilot samples (" + repr(av)
                                + " / " + repr(bv) + ")")
                deltas[shape][name] = None
            else:
                deltas[shape][name] = round((bv - av) / av, 6)
    for expected in C.PILOT_DELTA_KEYS:
        if expected not in deltas:
            problems.append("no pilot samples for " + expected)
    if problems:
        for p in problems:
            print("PROBLEM: " + p)
        return 1
    manifest.setdefault("windows", {}).setdefault("W2", {})["pilot_deltas"] = {
        "frozen": True, "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "window": "W2", "source": "records of the W2 pilot boots (group sht, rep 0)",
        "deltas": deltas, "samples": samples}
    write_manifest(manifest, manifest_path)
    print("froze W2 pilot deltas: " + json.dumps(deltas, sort_keys=True))
    return 0


# ------------------------------------------------------------------------------ self-test
# The oracle demonstrated a PASS on a record with a restart line and a wrong tuple. These
# fixtures keep that from coming back: the boot lines sit BEFORE the leg's elog slice, exactly
# as they do on a real boot, so a checker that reads only the slice cannot pass them.
SELFTEST_BOOT = ("[strata] engine started: /x/run/fsclamp-bin/rpcw-hip/strata --serve --gpu 0,1 "
                 "--spec {spec} --spec-min-p 0.5 --vram-reserve-mib {r1} "
                 "--vram-reserve-later-mib {r2} --max-context 262144 --dflash /x.gguf "
                 "--dflash-block {block} --expert-helper-slots {slots} --layer-split 30\n"
                 "strata generate: expert cache 5739 slots\n"
                 "expert-helper: {rows} rows on /s.sock (mirrored {rows}/4600 slots, {gib} GiB, "
                 "device free at hello 7516 MiB)\n")
SELFTEST_JUDGE = ("strata serve: prompt {n} tokens = {reused} reused + {n} read in 834764 ms "
                  "(287.1 tok/s), {gen} generated in 260 ms (30.8 tok/s), drafts accepted 12 of 20, "
                  "3 checkpoints helper=reqs=295 rows=2322 bytes=23777280 timeouts=0 fallbacks=0\n")
SELFTEST_JUDGE2 = ("strata serve: prompt {n} tokens = {reused} reused + 10 read in 900 ms "
                   "(11.1 tok/s), 1024 generated in 46500 ms (22.0 tok/s), drafts accepted 910 of "
                   "1400, 4 checkpoints helper=reqs=300 rows=2400 bytes=24000000 timeouts=0 "
                   "fallbacks=0\n")


def _fixture(root, key, arm, shape, *, spec="5", rows=3680, gib="6.84", restart_line="",
             second_reused=None, ttft_first=True, generation=None, started=1.0, ended=None,
             supervisor_events=None, legacy_events=None):
    import tempfile  # noqa: F401 - the caller owns the temp dir
    d = pathlib.Path(root) / "records" / key
    d.mkdir(parents=True, exist_ok=True)
    (pathlib.Path(root) / "boots").mkdir(parents=True, exist_ok=True)
    boot = SELFTEST_BOOT.format(spec=spec, r1=256, r2=192, block=7, slots=4600, rows=rows, gib=gib)
    boot += restart_line
    body = boot
    if shape == "ttft3":
        rec = {"arm": arm, "leg": shape, "kind": "ttft", "turns": 3, "max_tokens": 64, "ts": 1,
               "turn_rows": [{"ttft_client_s": 2.0 if ttft_first else None, "wall_s": 4.0},
                             {"ttft_client_s": 0.6, "wall_s": 2.5, "inter_turn_wall_s": 0.3}],
               "mem": {"max_rss_mib": 40000}}
    else:
        n = 239663 if shape == "pp239k" else 144000
        body += SELFTEST_JUDGE.format(n=n, reused=0, gen=(1024 if shape == "dec144k" else 8))
        if second_reused is not None:
            body += SELFTEST_JUDGE2.format(n=n, reused=second_reused)
        rec = {"arm": arm, "leg": shape, "kind": "tg" if second_reused is not None else "prefill",
               "ctx": n, "max_tokens": 8, "ts": 1, "walls": [1], "wall_s": 1,
               "content_from": "content", "prompt_bytes": n, "prompt_sha256": "x", "requests": [],
               "sha256": "y", "elog": str(pathlib.Path(root) / (key + "-" + shape + ".engine.log")),
               "elog_start": len(boot.encode()), "elog_end": len(body.encode()), "hlog": "",
               "hlog_start": 0, "hlog_end": 0,
               "mem": {"max_rss_mib": 40000, "cached_start_mib": 1000, "cached_end_mib": 2000}}
        pathlib.Path(rec["elog"]).write_text(body)
    (d / (arm + "-" + shape + ".json")).write_text(json.dumps(rec))
    boot = {"key": key, "started_epoch": started,
            "page_cache": {"cached_mib": 1000, "pgmajfault": 10}}
    if ended is not None:
        boot["ended_epoch"] = ended
    (pathlib.Path(root) / "boots" / (key + ".json")).write_text(json.dumps(boot))
    if generation or supervisor_events is not None or legacy_events is not None:
        run = pathlib.Path(root) / "run"
        run.mkdir(parents=True, exist_ok=True)
        if generation:
            (run / (key + ".supervisor.json")).write_text(json.dumps({"generation": generation}))
        # pair_supervisor.append_event writes <state stem>-events.log; the old path was <key>-events.log
        if supervisor_events is not None:
            (run / (key + ".supervisor-events.log")).write_text(supervisor_events)
        if legacy_events is not None:
            (run / (key + "-events.log")).write_text(legacy_events)


def selftest():
    """PASS on a clean pair; FAIL on restart, wrong tuple, bad fill, cold request 2, no TTFT."""
    import contextlib
    import io
    import tempfile

    def run(build):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode" if pathlib.Path("/tmp/opencode").is_dir() else None) as root:
            build(root)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = check(pathlib.Path(root), "W1")
            return rc, buf.getvalue()

    def clean(root, **kw):
        _fixture(root, "W1-A-pp-r1", "A", "pp239k", **kw)
        _fixture(root, "W1-B-pp-r1", "B", "pp239k", **kw)

    cases = [
        ("clean", lambda r: clean(r), 0),
        ("restart", lambda r: clean(r, generation=2), 1),
        ("restart-line", lambda r: (_fixture(r, "W1-A-pp-r1", "A", "pp239k",
                                            restart_line="pair supervisor: gen 2 recovering\n"),
                                    _fixture(r, "W1-B-pp-r1", "B", "pp239k")), 1),
        ("wrong-spec", lambda r: (_fixture(r, "W1-A-pp-r1", "A", "pp239k", spec="3"),
                                  _fixture(r, "W1-B-pp-r1", "B", "pp239k")), 1),
        ("bad-fill", lambda r: (_fixture(r, "W1-A-pp-r1", "A", "pp239k"),
                                _fixture(r, "W1-B-pp-r1", "B", "pp239k", rows=4300)), 1),
    ]
    failures = []
    for name, build, want in cases:
        rc, text = run(build)
        ok = (rc == want)
        print(("ok   " if ok else "FAIL ") + name + ": rc " + str(rc) + " (want " + str(want) + ")")
        if not ok:
            failures.append(name + ": " + text.strip().splitlines()[-2:][0] if text else name)
    # the decode gate: request 2 must be a cache hit
    with tempfile.TemporaryDirectory(dir="/tmp/opencode" if pathlib.Path("/tmp/opencode").is_dir() else None) as root:
        _fixture(root, "W2-A-dec-r1", "A", "dec144k", second_reused=0)
        _fixture(root, "W2-B-dec-r1", "B", "dec144k", second_reused=144000)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = check(pathlib.Path(root), "W2", MANIFEST)
        ok = rc == 1 and "request 2 was not served from the cache" in buf.getvalue()
        print(("ok   " if ok else "FAIL ") + "decode-cold-request2: rc " + str(rc))
        if not ok:
            failures.append("decode-cold-request2")
    # the TTFT gate: every turn must produce a first token
    with tempfile.TemporaryDirectory(dir="/tmp/opencode" if pathlib.Path("/tmp/opencode").is_dir() else None) as root:
        _fixture(root, "W1-A-ttft3", "A", "ttft3", ttft_first=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = check(pathlib.Path(root), "W1")
        ok = rc == 1 and "never produced a first token" in buf.getvalue()
        print(("ok   " if ok else "FAIL ") + "ttft-missing-first-token: rc " + str(rc))
        if not ok:
            failures.append("ttft-missing-first-token")
    # the restart source: pair_supervisor writes <state stem>-events.log, and only
    # recover/restart/exited lines are restarts ("stopped: stop requested" is a clean stop)
    with tempfile.TemporaryDirectory(dir=_tmpdir()) as root:
        _fixture(root, "W1-A-pp-r1", "A", "pp239k",
                 supervisor_events="2026-11-23T20:00:00 gen 2 recovering: helper did not attach\n")
        _fixture(root, "W1-B-pp-r1", "B", "pp239k")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = check(pathlib.Path(root), "W1")
        ok = rc == 1 and "supervisor event(s)" in buf.getvalue()
        print(("ok   " if ok else "FAIL ") + "supervisor-events-restart: rc " + str(rc))
        if not ok:
            failures.append("supervisor-events-restart")
    with tempfile.TemporaryDirectory(dir=_tmpdir()) as root:
        _fixture(root, "W1-A-pp-r1", "A", "pp239k", ended=6.0,
                 supervisor_events="2026-11-23T20:00:00 gen 1 stopped: stop requested\n",
                 legacy_events="2026-11-23T20:00:00 gen 2 recovering: wrong path\n")
        _fixture(root, "W1-B-pp-r1", "B", "pp239k", ended=6.0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = check(pathlib.Path(root), "W1")
        no_restart = "restart" not in buf.getvalue() and "supervisor event(s)" not in buf.getvalue()
        ok = rc == 0 and no_restart
        print(("ok   " if ok else "FAIL ") + "supervisor-normal-stop: rc " + str(rc)
              + " (a clean stop and the superseded <key>-events.log path must not count)")
        if not ok:
            failures.append("supervisor-normal-stop")

    # the kernel window ends with the boot: a fault AFTER a boot's end belongs to no earlier boot
    def fingerprint_case(name, boot_ended, fault_at, want_rc, want_until):
        calls = []

        def fake(since, until):
            calls.append((since, until))
            hit = fault_at is not None and since <= fault_at < until
            return {"available": True, "reason": "",
                    "matches": ["amdgpu ring gfx timeout"] if hit else [], "count": 1 if hit else 0}

        with tempfile.TemporaryDirectory(dir=_tmpdir()) as root:
            real = globals()["kernel_fingerprint"]
            globals()["kernel_fingerprint"] = fake
            try:
                _fixture(root, "W1-A-pp-r1", "A", "pp239k", started=1.0, ended=boot_ended)
                _fixture(root, "W1-B-pp-r1", "B", "pp239k", started=100.0, ended=110.0)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = check(pathlib.Path(root), "W1")
            finally:
                globals()["kernel_fingerprint"] = real
            untils = sorted(round(c[1], 3) for c in calls)
            ok = rc == want_rc and want_until in untils
            print(("ok   " if ok else "FAIL ") + name + ": rc " + str(rc) + " (want "
                  + str(want_rc) + "), untils " + str(untils))
            if not ok:
                failures.append(name)

    fingerprint_case("fingerprint-after-end", 2.0, 50.0, 0, 2.0)
    fingerprint_case("fingerprint-no-end-falls-back", None, 50.0, 1, 100.0)

    # the evidence file: what check() computes is written beside the boot record
    with tempfile.TemporaryDirectory(dir=_tmpdir()) as root:
        _fixture(root, "W1-A-pp-r1", "A", "pp239k", ended=6.0)
        _fixture(root, "W1-B-pp-r1", "B", "pp239k", ended=6.0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            check(pathlib.Path(root), "W1")
        path = pathlib.Path(root) / "boots" / "W1-A-pp-r1.evidence.json"
        evidence = json.loads(path.read_text()) if path.is_file() else {}
        missing = [k for k in ("kernel_fingerprint", "page_cache", "low_warning_count",
                               "granted_fs_slots", "cache_slots", "restart") if k not in evidence]
        ok = path.is_file() and not missing and evidence["cache_slots"] == 5739
        print(("ok   " if ok else "FAIL ") + "boot-evidence-file: missing " + str(missing))
        if not ok:
            failures.append("boot-evidence-file")

    if failures:
        print("SELFTEST FAILED: " + ", ".join(failures))
        return 1
    print("selftest OK: gates pass on a clean pair and fail on restart, wrong tuple, bad fill, "
          "cold request 2 and a missing first token; the restart source is the supervisor's own "
          "events log filtered to recover/restart/exited, the kernel window ends with the boot, "
          "and the per-boot evidence is written beside the boot record")
    return 0


def _tmpdir():
    return "/tmp/opencode" if pathlib.Path("/tmp/opencode").is_dir() else None


def main():
    ap = argparse.ArgumentParser(description="Phase-2 leg invocations: print, run, check, freeze pilot")
    ap.add_argument("--window", default="W1", choices=["W1", "W2", "W3", "W4"])
    ap.add_argument("--arm", choices=["A", "B"])
    ap.add_argument("--boot", default=None, help="boot group (W2: dec or sht); default from --shape")
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--shape", action="append", help="leg shape (default: the boot group's shapes)")
    ap.add_argument("--out-dir", type=pathlib.Path, default=OUT_DEFAULT)
    ap.add_argument("--manifest", type=pathlib.Path, default=MANIFEST)
    ap.add_argument("--port", type=int, default=8009)
    ap.add_argument("--runbook", metavar="WINDOW", help="print the whole window plan and exit")
    ap.add_argument("--run", action="store_true", help="execute the leg command(s)")
    ap.add_argument("--check", metavar="WINDOW", help="gate-check every leg record of a window")
    ap.add_argument("--freeze-pilot", metavar="WINDOW", help="freeze the W2 pilot deltas")
    ap.add_argument("--bookend", metavar="KEY",
                    help="record ended_epoch + final generation in boots/KEY.json (stop time)")
    ap.add_argument("--selftest", action="store_true", help="exercise the gates on synthetic records")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if a.runbook:
        print(runbook(a.out_dir, a.runbook))
        return 0
    if a.check:
        return check(a.out_dir, a.check, a.manifest)
    if a.freeze_pilot:
        return freeze_pilot(a.out_dir, a.freeze_pilot, a.manifest)
    if a.bookend:
        return bookend(a.out_dir, a.bookend)
    if not a.arm:
        ap.error("--arm is required unless --runbook/--check/--freeze-pilot is given")

    boot = a.boot or (pc.boot_for_shape(a.window, a.shape[0]) if a.shape else None)
    shapes = a.shape or next((row["shapes"] for row in schedule(a.window)
                              if row["arm"] == a.arm and row["rep"] == a.rep
                              and (boot is None or row["boot"] == boot)), None)
    if not shapes:
        ap.error("no boot scheduled for " + a.window + " " + a.arm + " r" + str(a.rep))
    boot = boot or pc.boot_for_shape(a.window, shapes[0])
    if a.window == "W2" and a.rep > 0 and not pilot_frozen(load_manifest(a.manifest)):
        print("REFUSING: W2 claim legs need the pilot deltas frozen first "
              "(python3 port_ab_legs.py --freeze-pilot W2)", file=sys.stderr)
        return 1
    cfg, paths, cmds = commands(a.out_dir, a.window, a.arm, a.rep, shapes, port=a.port, boot=boot)
    if not engine_pid(pc.ROOT / pc.ARMS[a.arm]["engine"], cfg["expert_helper_socket"]):
        print("WARNING: no engine for " + a.arm + " holds this leg's socket "
              + cfg["expert_helper_socket"] + "; the commands will fail until it boots",
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


if __name__ == "__main__":
    raise SystemExit(main())

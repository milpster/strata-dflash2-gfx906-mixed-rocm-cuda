#!/usr/bin/env python3
"""port_ab_prereq0.py - the pre-window gate (prerequisite 0). Exit 0 = the window may open.

Why this exists: on 2026-11-23 evening the box lost ~7.2 GiB to a stuck pytest process (99.6 %
CPU, 108 min) whose 'timeout' wrapper was itself wedged; a window that had opened on it would
have measured the box's starvation, not the engine. The contract's prerequisite 0 makes the
pre-boot state a gate:

  * MemAvailable >= 15 GiB (pre-boot only; during a leg the memory sampler records);
  * total non-engine RSS <= 8 GiB, AND no single non-engine process above 2 GiB left
    unresolved (five 1.9 GiB stragglers must not pass one-per-process checks). This SUM gate is
    the one gate the operator may waive per invocation (--waive-nonengine or
    PREREQ0_WAIVE_NONENGINE=1) when the box is running the operator's normal desktop; the waiver
    is recorded in the snapshot with who gave it and the reading, and every other gate stays
    fail-closed;
  * no non-pinned process above ~50 % of a core on the stage cores 3,5,7,9,11,13,15;
  * loadavg recorded;
  * no stratum serving process alive (the gate is PRE-BOOT: prod is stopped);
  * >= 40 GiB free where the leg cache dirs will be written (DISK, never compared to RAM).

Read-only: /proc, ps, statvfs. No signals, no GPU, no writes outside --out (the snapshot).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import port_ab_contract as C

# Processes of the serving lane itself. Pre-boot there should be none; if one is alive the
# window has not started from a clean state.
SERVING = ("strata", "strata-expert-helper", "strata-expert-helper-patient", "strata-vision")
GIB = 1 << 20    # KiB per GiB (meminfo)
MIB = 1024       # MiB per GiB (VmsumRSS and the ps snapshot)

# `ps` reports %CPU as cputime/elapsed, which is unbounded as elapsed approaches zero: the
# gate's own `ps` child shows 300 % for microseconds of work and lands on a stage core by
# chance (observed 2026-11-25, 1 sample in 15). A process younger than this cannot have
# competed with a prefill leg either, so age is the honest test, and such rows are reported
# as `young` instead of being flagged.
HOT_MIN_AGE_S = 5


def meminfo():
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, value = line.split(":", 1)
            out[key] = int(value.split()[0])
    return out


def rss_mib(pid):
    try:
        with open("/proc/" + str(pid) + "/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def cmdline(pid):
    try:
        return open("/proc/" + str(pid) + "/cmdline", "rb").read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def classify(pid):
    """'serving' for the engine/helper/server lane, 'other' for everything else."""
    try:
        exe = os.readlink("/proc/" + str(pid) + "/exe")
    except OSError:
        exe = ""
    base = exe.rsplit("/", 1)[-1]
    if base in SERVING:
        return "serving"
    cmd = cmdline(pid)
    if base.startswith("python") and "serve/server.py" in cmd:
        return "serving"
    return "other"


def processes():
    rows = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        rows.append({"pid": pid, "rss_mib": rss_mib(pid), "kind": classify(pid),
                     "cmd": cmdline(pid)[:160]})
    return rows


def parse_core_snapshot(text, classify_fn=None, self_pid=None):
    """Flag the hot threads on the stage cores from 'ps -eLo pid,tid,psr,pcpu,etimes,comm'.

    The pinned threads are the ENGINE's stage threads (--stage-cpus 3,5,7,9,11,13,15): their
    using those cores is the design. Everything else on a stage core is competition and is
    flagged — except a process younger than HOT_MIN_AGE_S, whose %CPU is an artefact of a
    near-zero lifetime rather than a rate (the gate's own ps child is the usual case).
    Split out from the ps call so the rule can be tested without a hot process.
    """
    classify_fn = classify_fn or classify
    self_pid = os.getpid() if self_pid is None else self_pid
    hot, young, threads = [], [], {}
    stage = set(C.PREREQ0["stage_cores"])
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        pid, tid, psr, pcpu = int(parts[0]), parts[1], int(parts[2]), float(parts[3])
        # etimes is bare seconds; a non-numeric elapsed column (the etime form) is unknown age
        age = int(parts[4]) if parts[4].isdigit() else -1
        comm = parts[5].strip()
        threads[psr] = threads.get(psr, 0) + 1
        if pid == self_pid or psr not in stage:
            continue
        if pcpu < C.PREREQ0["core_hot_fraction"] * 100:
            continue
        kind = classify_fn(pid)
        if kind == "serving":
            continue          # the engine's own stage threads: pinned there on purpose
        row = {"pid": pid, "tid": tid, "psr": psr, "pcpu": pcpu, "comm": comm,
               "kind": kind, "age_s": age, "cmd": cmdline(pid)[:120]}
        if 0 <= age < HOT_MIN_AGE_S:
            young.append(row)  # %CPU is not a rate at this age: the ps child at 300 %
            continue
        hot.append(row)
    return {"threads_per_cpu": {str(k): v for k, v in sorted(threads.items())},
            "hot": hot, "young": young}


def temp_core_snapshot():
    """ps -eLo psr,pcpu for every thread; the stage-core rule is parse_core_snapshot's."""
    try:
        text = subprocess.run(["ps", "-eLo", "pid,tid,psr,pcpu,etimes,comm"],
                              capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError) as error:
        return {"error": str(error), "hot": []}
    snap = parse_core_snapshot(text)
    for row in snap["hot"]:
        row["cmd"] = cmdline(row["pid"])[:120]
    return snap


# The four RAM/CPU gates the pre-boot state owns. Alive serving processes make them
# meaningless (the numbers describe the serving lane, not the box a leg will run on), so in
# that state they are reported as NOT EVALUATED and never as FAIL lines. The disk gate is the
# filesystem the leg caches are written to and stays measured in both states.
PREBOOT_GATES = ("memory", "nonengine_rss", "single_process", "stage_cores")

# The one waivable gate. The operator's normal desktop (browser, editor, Hindsight, agent host)
# is the box's idle baseline for a window and yields ~12.5-13.3 GiB of non-engine RSS; the sum
# gate therefore refuses a box that is otherwise idle. The waiver is per invocation, applies to
# the SUM only, and is written into the snapshot instead of being silently dropped.
WAIVER = {
    "gate": "nonengine_rss",
    "by": "operator",
    "directive": "operator directive 2026-11-23: the ~12.5-13.3 GiB of non-engine RSS (browser, "
                 "editor, Hindsight, agent host) is normal desktop usage and is waived as the "
                 "idle baseline for the window",
    "journal": "journal/port-ab-harness-prep-2026-11-23.md (section 'Window W1 open "
               "(2026-11-23 21:1x)')",
    "scope": "the non-engine RSS SUM gate only; MemAvailable, per-process >= 2 GiB, stage-core "
             "load, serving-process and disk gates still fail closed",
    "flag": "--waive-nonengine / PREREQ0_WAIVE_NONENGINE=1",
}


def env_waiver():
    """PREREQ0_WAIVE_NONENGINE=1 (also true/yes/on) as the flag's default."""
    return os.environ.get("PREREQ0_WAIVE_NONENGINE", "").strip().lower() in ("1", "true", "yes", "on")


def gate_verdict(avail_kib, other_mib, others, cores, serving, free_gib, waive_nonengine=None):
    """The prerequisite-0 failures, split from the probes so the prod-up state is testable.

    Two states, one refusal: with serving processes alive the only failure is 'stop prod
    first' (the gate is PRE-BOOT), the four RAM/CPU gates are skipped instead of appending
    FAIL lines about a box that is currently serving, and the exit code is unchanged (1).

    One waivable gate, per invocation: when the non-engine RSS SUM exceeds its limit and
    waive_nonengine is set (the WAIVER record), that failure moves to 'waived' -- it is not
    dropped, it is reported with the reading and the operator who waived it. Every other gate
    still appends to 'failures' and still refuses. The prod-up state waives nothing.
    """
    failures, evaluated, skipped, waived = [], [], [], []
    if serving:
        failures.append("serving processes alive (" + ", ".join(str(p["pid"]) for p in serving)
                        + "): the gate is PRE-BOOT, stop prod first")
        skipped.extend(PREBOOT_GATES)
    else:
        evaluated.extend(PREBOOT_GATES)
        if avail_kib < C.PREREQ0["mem_available_gib_min"] * GIB:
            failures.append("MemAvailable " + str(round(avail_kib / GIB, 1)) + " GiB < "
                            + str(C.PREREQ0["mem_available_gib_min"]) + " GiB")
        if other_mib > C.PREREQ0["nonengine_rss_gib_max"] * MIB:
            reading = ("non-engine RSS " + str(round(other_mib / MIB, 2)) + " GiB > "
                       + str(C.PREREQ0["nonengine_rss_gib_max"]) + " GiB")
            if waive_nonengine:
                waived.append({**waive_nonengine, "reading": reading,
                               "nonengine_gib": round(other_mib / MIB, 2),
                               "gate_max_gib": C.PREREQ0["nonengine_rss_gib_max"]})
            else:
                failures.append(reading)
        big = sorted((p for p in others
                      if p["rss_mib"] >= C.PREREQ0["single_process_rss_gib_warn"] * 1024),
                     key=lambda p: -p["rss_mib"])[:10]
        if big:
            failures.append("process(es) >= " + str(C.PREREQ0["single_process_rss_gib_warn"])
                            + " GiB RSS must be resolved or reported: "
                            + ", ".join(str(p["pid"]) + " " + p["cmd"].split()[0] for p in big[:3]))
        if cores.get("hot"):
            failures.append("hot non-pinned thread(s) on the stage cores: "
                            + ", ".join(str(h["pid"]) + "/" + h["comm"] + " " + str(h["pcpu"]) + "%"
                                        for h in cores["hot"][:5]))
    if free_gib is not None:
        evaluated.append("disk")
        if free_gib < C.PREREQ0["cache_free_gib_min"]:
            failures.append("cache filesystem free " + str(round(free_gib, 1)) + " GiB < "
                            + str(C.PREREQ0["cache_free_gib_min"]) + " GiB (disk, not RAM)")
    else:
        skipped.append("disk")
    return {"failures": failures, "evaluated": evaluated, "skipped": skipped, "waived": waived}


def verdict_label(failures, waived):
    """OPEN, OPEN with the waived gate(s) named, or REFUSED. Any failure refuses, always."""
    if failures:
        return "REFUSED"
    if waived:
        return ("OPEN (WAIVED: " + ", ".join(str(w.get("gate") or "unknown") for w in waived)
                + " by operator)")
    return "OPEN"


def selftest():
    """The rule, on a synthetic ps snapshot: engine threads pass, an intruder is flagged.

    Also the two states of the gate: a clean pre-boot passes, a starved one names memory, and
    a prod-up box refuses with 'stop prod first' alone while the RAM/CPU gates are skipped.
    """
    text = (
        "  PID     TID PSR %CPU ELAPSED COMMAND\n"
        " 1001    1001   0  1.0  100:00 hostapp\n"
        " 2002    2003   5 99.0  010:00 strata\n"      # the engine's pinned stage thread
        " 3003    3003   7 97.5  108:00 pytest\n"      # the incident shape: a stuck test
        " 3004    3004   9 12.0  108:00 python\n"      # low load: not hot
        " 4004    4004   3 51.0  001:00 kworker/3:1\n" # just over the line
    )
    kinds = {2002: "serving", 3003: "other", 3004: "other", 4004: "other", 1001: "other"}
    snap = parse_core_snapshot(text, classify_fn=lambda pid: kinds.get(pid, "other"), self_pid=1)
    flagged = sorted(row["pid"] for row in snap["hot"])
    assert flagged == [3003, 4004], flagged
    assert snap["threads_per_cpu"]["5"] == 1 and snap["threads_per_cpu"]["0"] == 1, snap

    # 2026-11-25: the gate's own `ps` child appears in its own listing at a meaningless
    # 300 % (%CPU is cputime/elapsed, and elapsed is ~0). Age decides: young rows are
    # reported, never flagged; an old hot process on the same cores still is.
    young_text = (
        "  501   5010  11 300.0     0 ps\n"        # our own ps child, on a stage core
        " 5002   5002   5  88.0   900 borg\n"      # old and genuinely hot
        " 5003   5003   7  51.0     4 ps\n"        # young, just under the age floor
    )
    young_snap = parse_core_snapshot(young_text, classify_fn=lambda pid: "other", self_pid=1)
    assert [r["pid"] for r in young_snap["hot"]] == [5002], young_snap
    assert [r["pid"] for r in young_snap["young"]] == [501, 5003], young_snap

    procs = [{"pid": 4001, "rss_mib": 900, "kind": "other", "cmd": "python one.py"}]
    clean = gate_verdict(40 * GIB, 900, procs, {"hot": []}, [], 100.0)
    assert clean["failures"] == [] and clean["skipped"] == [], clean
    starved = gate_verdict(10 * GIB, 900, procs, {"hot": []}, [], 100.0)
    assert len(starved["failures"]) == 1 and "MemAvailable" in starved["failures"][0], starved
    hot = [{"pid": 3003, "comm": "pytest", "pcpu": 97.5}]
    serving = [{"pid": 2002, "rss_mib": 4000, "kind": "serving", "cmd": "strata --serve"}]
    prod_up = gate_verdict(10 * GIB, 20000, procs, {"hot": hot}, serving, 100.0)
    assert len(prod_up["failures"]) == 1 and "stop prod first" in prod_up["failures"][0], prod_up
    assert prod_up["skipped"] == list(PREBOOT_GATES), prod_up
    assert prod_up["evaluated"] == ["disk"], prod_up
    assert prod_up["waived"] == [], prod_up

    # The waived sum gate: the operator's desktop as the idle baseline. 13,300 MiB is the
    # reading recorded at 21:23; the waiver names the reading and the operator, and exits 0.
    desktop_mib = 13300
    over = gate_verdict(40 * GIB, desktop_mib, procs, {"hot": []}, [], 100.0)
    assert len(over["failures"]) == 1 and "non-engine RSS" in over["failures"][0], over
    assert over["waived"] == [] and verdict_label(over["failures"], over["waived"]) == "REFUSED"
    waived = gate_verdict(40 * GIB, desktop_mib, procs, {"hot": []}, [], 100.0,
                          waive_nonengine=WAIVER)
    assert waived["failures"] == [], waived
    assert len(waived["waived"]) == 1, waived
    entry = waived["waived"][0]
    assert entry["gate"] == "nonengine_rss" and entry["by"] == "operator", entry
    assert entry["nonengine_gib"] == round(desktop_mib / MIB, 2), entry
    assert entry["gate_max_gib"] == C.PREREQ0["nonengine_rss_gib_max"], entry
    assert "non-engine RSS" in entry["reading"], entry
    assert "journal/port-ab-harness-prep-2026-11-23.md" in entry["journal"], entry
    assert verdict_label(waived["failures"], waived["waived"]) == "OPEN (WAIVED: nonengine_rss by operator)"
    # ... and the waiver covers nothing else: memory, the stage cores and the disk still refuse
    still = gate_verdict(10 * GIB, desktop_mib, procs, {"hot": hot}, [], 30.0,
                         waive_nonengine=WAIVER)
    assert len(still["failures"]) == 3, still
    assert all("non-engine RSS" not in f for f in still["failures"]), still
    assert len(still["waived"]) == 1, still
    assert verdict_label(still["failures"], still["waived"]) == "REFUSED", still
    # the prod-up state waives nothing: the gate is not evaluated there at all
    prod_up_w = gate_verdict(10 * GIB, desktop_mib, procs, {"hot": hot}, serving, 100.0,
                             waive_nonengine=WAIVER)
    assert prod_up_w["waived"] == [] and len(prod_up_w["failures"]) == 1, prod_up_w
    print("selftest OK: engine stage threads pass, hot intruders flagged, low load ignored; "
          "clean pre-boot passes, a starved one fails on memory, and a prod-up box refuses only "
          "with 'stop prod first' (RAM/CPU gates not evaluated); the non-engine RSS sum refuses "
          "unwaived, is WAIVED (operator, reading + journal recorded) per invocation, and memory, "
          "stage-core, disk and prod-up still refuse with the waiver set")





def main() -> int:
    ap = argparse.ArgumentParser(description="prerequisite-0 pre-window gate")
    ap.add_argument("--out", type=pathlib.Path, default=HERE / "out" / "port-ab-20261123")
    ap.add_argument("--cache-dir", help="filesystem the leg cache dirs live on (default: --out)")
    ap.add_argument("--json", action="store_true", help="print the snapshot JSON only")
    ap.add_argument("--selftest", action="store_true", help="exercise the stage-core rule, no ps")
    ap.add_argument("--waive-nonengine", action="store_true", default=env_waiver(),
                    help="waive the non-engine RSS SUM gate only, for this invocation "
                         "(default: PREREQ0_WAIVE_NONENGINE=1). The reading, the operator and the "
                         "journal are recorded in the snapshot; every other gate still refuses")
    ap.add_argument("--no-waive-nonengine", dest="waive_nonengine", action="store_false",
                    help="force the sum gate back on even when PREREQ0_WAIVE_NONENGINE is set")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return 0
    waive = dict(WAIVER) if a.waive_nonengine else None

    mi = meminfo()                      # /proc/meminfo values are KiB
    avail_kib = mi.get("MemAvailable", 0)
    swap_used_kib = (mi.get("SwapTotal", 0) - mi.get("SwapFree", 0))
    mem_total_kib = mi.get("MemTotal", 0)

    procs = processes()
    serving = [p for p in procs if p["kind"] == "serving"]
    others = [p for p in procs if p["kind"] == "other"]
    other_mib = sum(p["rss_mib"] for p in others)
    big = sorted((p for p in others if p["rss_mib"] >= C.PREREQ0["single_process_rss_gib_warn"] * 1024),
                 key=lambda p: -p["rss_mib"])[:10]
    top = sorted(others, key=lambda p: -p["rss_mib"])[:10]

    cores = temp_core_snapshot()
    loadavg = [round(float(x), 2) for x in open("/proc/loadavg").read().split()[:3]]

    cache_probe = pathlib.Path(a.cache_dir or a.out)
    try:
        free_gib = shutil.disk_usage(cache_probe if cache_probe.exists() else cache_probe.parent).free / (1 << 30)
    except OSError:
        free_gib = None

    verdict = gate_verdict(avail_kib, other_mib, others, cores, serving, free_gib, waive)
    failures = verdict["failures"]
    waived = verdict["waived"]
    prod_up = bool(serving)
    mem_gate = ("not evaluated (prod up)" if prod_up else
                "gate >= " + str(C.PREREQ0["mem_available_gib_min"]))
    rss_gate = ("not evaluated (prod up)" if prod_up else
                "gate <= " + str(C.PREREQ0["nonengine_rss_gib_max"])
                + (" [WAIVED by operator: " + WAIVER["journal"] + "]" if waived else ""))

    snapshot = {
        "ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mem": {"total_gib": round(mem_total_kib / GIB, 1), "available_gib": round(avail_kib / GIB, 2),
                "swap_used_gib": round(swap_used_kib / GIB, 2)},
        "rss": {"nonengine_gib": round(other_mib / MIB, 2), "engine_gib": round(sum(p["rss_mib"] for p in serving) / MIB, 2),
                "top": top, "over_2_gib": big},
        "cpu": {"loadavg": loadavg, "threads_per_cpu": cores.get("threads_per_cpu"),
                "hot_stage_core_threads": cores.get("hot"),
                "young_stage_core_threads": cores.get("young"),
                "stage_cores": C.PREREQ0["stage_cores"]},
        "disk": {"probe": str(cache_probe), "free_gib": None if free_gib is None else round(free_gib, 1)},
        "gates": {"evaluated": verdict["evaluated"], "not_evaluated": verdict["skipped"],
                  "waived": [w["gate"] for w in waived]},
        "waived": waived,
        "verdict": verdict_label(failures, waived),
        "failures": failures,
    }
    if a.json:
        print(json.dumps(snapshot, indent=1, sort_keys=True))
    else:
        print("prerequisite 0 @ " + snapshot["iso"])
        print("  memory      : MemAvailable " + str(snapshot["mem"]["available_gib"]) + " GiB / "
              + str(snapshot["mem"]["total_gib"]) + " GiB, swap used "
              + str(snapshot["mem"]["swap_used_gib"]) + " GiB  (" + mem_gate + ")")
        print("  non-engine  : " + str(snapshot["rss"]["nonengine_gib"]) + " GiB  (" + rss_gate + ")")
        for p in top[:5]:
            print("      " + str(p["pid"]).rjust(7) + " " + str(p["rss_mib"]).rjust(7) + " MiB  " + p["cmd"])
        hot = cores.get("hot") or []
        print("  cpu         : loadavg " + " ".join(str(x) for x in loadavg)
              + "; hot stage-core threads: "
              + ((str(len(hot)) + " (" + ", ".join(str(h["pid"]) + "/" + str(h["pcpu"]) + "%"
                                                   for h in hot[:5]) + ")")
                 if hot else "none")
              + ("  [not evaluated (prod up)]" if prod_up else ""))
        print("  disk        : " + str(snapshot["disk"]["free_gib"]) + " GiB free on "
              + snapshot["disk"]["probe"] + "  (gate >= " + str(C.PREREQ0["cache_free_gib_min"]) + ")")
        print("  serving     : " + (str(len(serving)) + " process(es) alive" if serving else "none (pre-boot OK)"))
        for f in failures:
            print("  FAIL: " + f)
        for w in waived:
            print("  WAIVED (" + w["by"] + "): " + w["reading"])
            print("      directive: " + w["directive"])
            print("      record   : " + w["journal"] + "  [" + w["flag"] + "]")
            print("      scope    : " + w["scope"])
        print("VERDICT: " + snapshot["verdict"])

    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        path = a.out / ("prereq0-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
        path.write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n")
        if not a.json:
            print("snapshot: " + str(path))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

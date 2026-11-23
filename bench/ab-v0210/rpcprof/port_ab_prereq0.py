#!/usr/bin/env python3
"""port_ab_prereq0.py - the pre-window gate (prerequisite 0). Exit 0 = the window may open.

Why this exists: on 2026-11-23 evening the box lost ~7.2 GiB to a stuck pytest process (99.6 %
CPU, 108 min) whose 'timeout' wrapper was itself wedged; a window that had opened on it would
have measured the box's starvation, not the engine. The contract's prerequisite 0 makes the
pre-boot state a gate:

  * MemAvailable >= 15 GiB (pre-boot only; during a leg the memory sampler records);
  * total non-engine RSS <= 8 GiB, AND no single non-engine process above 2 GiB left
    unresolved (five 1.9 GiB stragglers must not pass one-per-process checks);
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
    flagged. Split out from the ps call so the rule can be tested without a hot process.
    """
    classify_fn = classify_fn or classify
    self_pid = os.getpid() if self_pid is None else self_pid
    hot, threads = [], {}
    stage = set(C.PREREQ0["stage_cores"])
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        pid, tid, psr, pcpu = int(parts[0]), parts[1], int(parts[2]), float(parts[3])
        comm = parts[5].strip()
        threads[psr] = threads.get(psr, 0) + 1
        if pid == self_pid or psr not in stage:
            continue
        if pcpu < C.PREREQ0["core_hot_fraction"] * 100:
            continue
        kind = classify_fn(pid)
        if kind == "serving":
            continue          # the engine's own stage threads: pinned there on purpose
        hot.append({"pid": pid, "tid": tid, "psr": psr, "pcpu": pcpu, "comm": comm,
                    "kind": kind, "cmd": cmdline(pid)[:120]})
    return {"threads_per_cpu": {str(k): v for k, v in sorted(threads.items())}, "hot": hot}


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


def selftest():
    """The rule, on a synthetic ps snapshot: engine threads pass, an intruder is flagged."""
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
    print("selftest OK: engine stage threads pass, hot intruders flagged, low load ignored")





def main() -> int:
    ap = argparse.ArgumentParser(description="prerequisite-0 pre-window gate")
    ap.add_argument("--out", type=pathlib.Path, default=HERE / "out" / "port-ab-20261123")
    ap.add_argument("--cache-dir", help="filesystem the leg cache dirs live on (default: --out)")
    ap.add_argument("--json", action="store_true", help="print the snapshot JSON only")
    ap.add_argument("--selftest", action="store_true", help="exercise the stage-core rule, no ps")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return 0

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

    failures = []
    if serving:
        failures.append("serving processes alive (" + ", ".join(str(p["pid"]) for p in serving)
                        + "): the gate is PRE-BOOT, stop prod first")
    if avail_kib < C.PREREQ0["mem_available_gib_min"] * GIB:
        failures.append("MemAvailable " + str(round(avail_kib / GIB, 1)) + " GiB < "
                        + str(C.PREREQ0["mem_available_gib_min"]) + " GiB")
    if other_mib > C.PREREQ0["nonengine_rss_gib_max"] * MIB:
        failures.append("non-engine RSS " + str(round(other_mib / MIB, 2)) + " GiB > "
                        + str(C.PREREQ0["nonengine_rss_gib_max"]) + " GiB")
    if big:
        failures.append("process(es) >= " + str(C.PREREQ0["single_process_rss_gib_warn"])
                        + " GiB RSS must be resolved or reported: "
                        + ", ".join(str(p["pid"]) + " " + p["cmd"].split()[0] for p in big[:3]))
    if cores.get("hot"):
        failures.append("hot non-pinned thread(s) on the stage cores: "
                        + ", ".join(str(h["pid"]) + "/" + h["comm"] + " " + str(h["pcpu"]) + "%" for h in cores["hot"][:5]))
    if free_gib is not None and free_gib < C.PREREQ0["cache_free_gib_min"]:
        failures.append("cache filesystem free " + str(round(free_gib, 1)) + " GiB < "
                        + str(C.PREREQ0["cache_free_gib_min"]) + " GiB (disk, not RAM)")

    snapshot = {
        "ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mem": {"total_gib": round(mem_total_kib / GIB, 1), "available_gib": round(avail_kib / GIB, 2),
                "swap_used_gib": round(swap_used_kib / GIB, 2)},
        "rss": {"nonengine_gib": round(other_mib / MIB, 2), "engine_gib": round(sum(p["rss_mib"] for p in serving) / MIB, 2),
                "top": top, "over_2_gib": big},
        "cpu": {"loadavg": loadavg, "threads_per_cpu": cores.get("threads_per_cpu"),
                "hot_stage_core_threads": cores.get("hot"), "stage_cores": C.PREREQ0["stage_cores"]},
        "disk": {"probe": str(cache_probe), "free_gib": None if free_gib is None else round(free_gib, 1)},
        "verdict": "OPEN" if not failures else "REFUSED",
        "failures": failures,
    }
    if a.json:
        print(json.dumps(snapshot, indent=1, sort_keys=True))
    else:
        print("prerequisite 0 @ " + snapshot["iso"])
        print("  memory      : MemAvailable " + str(snapshot["mem"]["available_gib"]) + " GiB / "
              + str(snapshot["mem"]["total_gib"]) + " GiB, swap used "
              + str(snapshot["mem"]["swap_used_gib"]) + " GiB  (gate >= "
              + str(C.PREREQ0["mem_available_gib_min"]) + ")")
        print("  non-engine  : " + str(snapshot["rss"]["nonengine_gib"]) + " GiB  (gate <= "
              + str(C.PREREQ0["nonengine_rss_gib_max"]) + ")")
        for p in top[:5]:
            print("      " + str(p["pid"]).rjust(7) + " " + str(p["rss_mib"]).rjust(7) + " MiB  " + p["cmd"])
        print("  cpu         : loadavg " + " ".join(str(x) for x in loadavg)
              + "; hot stage-core threads: " + (str(len(cores.get("hot") or [])) + " ("
              + ", ".join(str(h["pid"]) + "/" + str(h["pcpu"]) + "%" for h in (cores.get("hot") or [])[:5]) + ")"
              if cores.get("hot") else "none"))
        print("  disk        : " + str(snapshot["disk"]["free_gib"]) + " GiB free on "
              + snapshot["disk"]["probe"] + "  (gate >= " + str(C.PREREQ0["cache_free_gib_min"]) + ")")
        print("  serving     : " + (str(len(serving)) + " process(es) alive" if serving else "none (pre-boot OK)"))
        for f in failures:
            print("  FAIL: " + f)
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

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
  * no non-pinned process above ~50 % of a core on the stage cores 3,5,7,9,11,13,15, and no
    non-pinned, non-serving stage-core thread that consumed more than ~0.5 s of CPU across a
    2 s interval (a duty-cycled hog reads under the line on any single sample, so the gate
    takes a second sample and subtracts);
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
# chance (observed 2026-11-25, 1 sample in 15). Three rules keep that artefact and every
# other near-zero-lifetime row out, without hiding a real burst:
#
#   * the gate's own pid AND the ps child that produced the listing are excluded by pid. The
#     child's pid is the Popen pid (not guessed), so the self-match is structural; the
#     age/CPU rule below is only the second line of defence when a pid is not known;
#   * a row younger than HOT_MIN_AGE_S is exempt ONLY when it has also consumed less than
#     YOUNG_CPU_S of CPU (ps `cputimes`). The ps child has ~0 s and is exempt; a 3 s build
#     step or cron burst has ~3 s and is flagged. (The previous age-only rule made such a
#     process permanently invisible: at 3 s old its etimes is 3 for its whole life.)
#   * a second snapshot SAMPLE_LAG_S later flags any non-pinned, non-serving stage-core thread
#     whose utime+stime (from /proc/<pid>/task/<tid>/stat, clock ticks) advanced by more than
#     SAMPLE_CPU_S across the interval, which catches a duty-cycled hog a single instantaneous
#     sample misses.
#
# `ps cputimes` is whole seconds, so the young-row test has 1 s of resolution; that is coarser
# than %CPU but it is a consumed-time measurement, which is what a prefill leg competes with.
# The interval rule reads /proc instead and so keeps 10 ms resolution.
HOT_MIN_AGE_S = 5
YOUNG_CPU_S = 0.5
SAMPLE_LAG_S = 2.0
SAMPLE_CPU_S = 0.5
PS_FIELDS = "pid,tid,psr,pcpu,etimes,cputimes,comm"


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


def parse_etime(text):
    """Age in seconds from `ps`'s [[dd-]hh:]mm:ss column, or None when unparseable.

    `etimes` is bare seconds and is what PS_FIELDS asks for, but the parser accepts the
    `etime` form too: if a ps change ever drops or renames the column the value lands here
    instead of silently becoming 'no age' and reverting the young-row exemption. None means
    UNKNOWN, and unknown age is not exempt (fail closed); the caller counts and prints those
    rows so the format change is visible.
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    days = 0
    rest = text
    if "-" in rest:
        head, _, rest = rest.partition("-")
        if not head.isdigit():
            return None
        days = int(head)
    parts = rest.split(":")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    seconds = 0
    for part in parts:                       # [[dd-]hh:]mm:ss and mm:ss both land here
        seconds = seconds * 60 + int(part)
    return days * 86400 + seconds


def parse_cpu_seconds(text):
    """Consumed CPU seconds from `ps cputimes` (whole seconds), or None when unparseable."""
    text = (text or "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def parse_core_snapshot(text, classify_fn=None, self_pid=None, ps_pid=None):
    """Flag the hot threads on the stage cores from 'ps -eLo pid,tid,psr,pcpu,etimes,cputimes,comm'.

    The pinned threads are the ENGINE's stage threads (--stage-cpus 3,5,7,9,11,13,15): their
    using those cores is the design. Everything else on a stage core is competition and is
    flagged — except the gate's own pid and the ps child that produced this listing (both
    excluded by pid, structurally), and a row that is both younger than HOT_MIN_AGE_S and
    under YOUNG_CPU_S of consumed CPU, whose %CPU is an artefact of a near-zero lifetime
    rather than a rate (the gate's own ps child is the usual case). A young row that has
    consumed real CPU — a 3 s build step — is flagged: age alone is not an excuse.
    Split out from the ps call so the rule can be tested without a hot process.
    """
    classify_fn = classify_fn or classify
    self_pid = os.getpid() if self_pid is None else self_pid
    exempt = {self_pid}
    if ps_pid is not None:
        exempt.add(int(ps_pid))
    hot, young, threads = [], [], {}
    unknown_age, unparsed_age = [], []
    rows = 0
    stage = set(C.PREREQ0["stage_cores"])
    for line in text.splitlines():
        parts = line.split(None, 6)
        if not parts or not parts[0].isdigit():
            continue                        # header
        if len(parts) < 7:
            continue                        # truncated row: PS_FIELDS changed or ps raced
        try:
            pid, tid, psr = int(parts[0]), int(parts[1]), int(parts[2])
            pcpu = float(parts[3])
        except ValueError:
            continue
        rows += 1
        cputimes = parse_cpu_seconds(parts[5])
        age = parse_etime(parts[4])
        comm = parts[6].strip()
        if age is None:
            # unknown age is NOT exempt: fail closed. Counted for every row, not only hot ones,
            # so a ps format change shows up in the text even on an idle box.
            unparsed_age.append({"pid": pid, "tid": tid, "comm": comm, "age_raw": parts[4]})
        threads[psr] = threads.get(psr, 0) + 1
        if pid in exempt or psr not in stage:
            continue
        if pcpu < C.PREREQ0["core_hot_fraction"] * 100:
            continue
        kind = classify_fn(pid)
        if kind == "serving":
            continue          # the engine's own stage threads: pinned there on purpose
        row = {"pid": pid, "tid": tid, "psr": psr, "pcpu": pcpu, "comm": comm,
               "kind": kind, "age_s": -1 if age is None else age,
               "cputimes_s": cputimes, "cmd": cmdline(pid)[:120]}
        if age is None:
            unknown_age.append(row)         # fail closed: not exempt, but named
            hot.append(row)
            continue
        if age < HOT_MIN_AGE_S and cputimes is not None and cputimes < YOUNG_CPU_S:
            young.append(row)               # %CPU is not a rate at this age: the ps child
            continue
        hot.append(row)
    return {"threads_per_cpu": {str(k): v for k, v in sorted(threads.items())},
            "hot": hot, "young": young, "unknown_age": unknown_age,
            "unparsed_age": unparsed_age, "rows": rows}


def _ps_snapshot():
    """One ps listing, with the child's pid captured so the self-match is excluded by parentage.

    Popen rather than run() because run() discards the pid: the listing contains the ps child
    (its %CPU is cputime/elapsed and meaningless at ~0 s), and excluding that exact pid is
    structural. read() then wait() so the child is reaped before its pid can be reused.
    """
    proc = subprocess.Popen(["ps", "-eLo", PS_FIELDS],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    if proc.returncode != 0:
        raise OSError("ps exited " + str(proc.returncode) + ": " + (err or "").strip()[:200])
    return out, proc.pid


def parse_cputimes_snapshot(text):
    """{key: cputimes} from a PS_FIELDS listing, keyed by (pid, tid).

    The key is the THREAD, not the process: a duty-cycled single thread of a busy process is
    the shape the sampling pass is for, and interleaved threads would cancel each other out in
    a per-process sum. `ps cputimes` is whole seconds, so this is the fallback measure; the
    primary one is thread_cpu_seconds' /proc utime+stime (jiffy precision).
    """
    times = {}
    for line in text.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        try:
            key = (int(parts[0]), int(parts[1]))
        except ValueError:
            continue
        value = parse_cpu_seconds(parts[5])
        if value is not None:
            times[key] = value
    return times


def thread_cpu_seconds(keys):
    """{key: utime+stime in seconds} from /proc/<pid>/task/<tid>/stat, jiffy precision.

    `ps cputimes` is whole seconds: 0.5 s of duty-cycled work in a 2 s interval would round to
    0 or 1 and the > 0.5 s test would flicker. /proc carries the accounting in clock ticks
    (10 ms here), which is what 'utime+stime advanced' actually means. Unreadable threads are
    simply absent (they exited, or /proc is restricted); the caller then has nothing to compare
    and does not flag them.
    """
    ticks = os.sysconf("SC_CLK_TCK") or 100
    times = {}
    for pid, tid in keys:
        try:
            with open("/proc/" + str(pid) + "/task/" + str(tid) + "/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            times[(pid, tid)] = (int(fields[11]) + int(fields[12])) / ticks
        except (OSError, IndexError, ValueError):
            continue
    return times


def stage_core_keys(text, classify_fn=None, self_pid=None, ps_pid=None):
    """(pid, tid) of non-exempt, non-serving threads whose last CPU was a stage core."""
    classify_fn = classify_fn or classify
    self_pid = os.getpid() if self_pid is None else self_pid
    exempt = {self_pid}
    if ps_pid is not None:
        exempt.add(int(ps_pid))
    stage = set(C.PREREQ0["stage_cores"])
    keys = []
    for line in text.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        try:
            pid, tid, psr = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if pid in exempt or psr not in stage:
            continue
        if classify_fn(pid) == "serving":
            continue
        keys.append((pid, tid))
    return keys


def sampled_hot(first_text, second_text, classify_fn=None, self_pid=None, ps_pid=None,
                cpu_first=None, cpu_second=None):
    """Threads on a stage core whose consumed CPU advanced > SAMPLE_CPU_S across two samples.

    A duty-cycled hog (busy 200 ms in every 500 ms, say) can read 40 % or 0 % on any single
    sample and slip past the instantaneous rule; its utime+stime still advances by the
    interval's worth of CPU. cpu_first/cpu_second are thread_cpu_seconds maps (jiffy
    precision); without them the ps cputimes columns are differenced instead (whole seconds, a
    coarser but honest fallback). Threads that vanished between samples are dropped (nothing to
    compare), and the stage-core and serving/pinned rules are the instantaneous rule's.
    """
    classify_fn = classify_fn or classify
    self_pid = os.getpid() if self_pid is None else self_pid
    exempt = {self_pid}
    if ps_pid is not None:
        exempt.add(int(ps_pid))
    at_first = parse_cputimes_snapshot(first_text) if cpu_first is None else dict(cpu_first)
    at_second = parse_cputimes_snapshot(second_text) if cpu_second is None else dict(cpu_second)
    stage = set(C.PREREQ0["stage_cores"])
    cores_now, comm_now = {}, {}
    second_cols = {}
    for line in second_text.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        try:
            key = (int(parts[0]), int(parts[1]))
            cores_now[key] = int(parts[2])
            second_cols[key] = (float(parts[3]), parts[4], parse_cpu_seconds(parts[5]))
        except ValueError:
            continue
        comm_now[key] = parts[6].strip()
    found = []
    for key, before in at_first.items():
        after = at_second.get(key)
        if after is None:
            continue
        advanced = after - before
        if advanced <= SAMPLE_CPU_S:
            continue
        pid, tid = key
        if pid in exempt or cores_now.get(key) not in stage:
            continue
        kind = classify_fn(pid)
        if kind == "serving":
            continue
        pcpu, age_raw, cputimes = second_cols.get(key, (0.0, "", None))
        age = parse_etime(age_raw)
        found.append({"pid": pid, "tid": tid, "psr": cores_now.get(key),
                      "comm": comm_now.get(key, ""), "kind": kind, "pcpu": pcpu,
                      "age_s": -1 if age is None else age, "cputimes_s": cputimes,
                      "cpu_advanced_s": round(advanced, 2),
                      "found_by": "sampling", "cmd": cmdline(pid)[:120]})
    found.sort(key=lambda r: (r["pid"], r["tid"]))
    return found


def temp_core_snapshot(lag_s=SAMPLE_LAG_S):
    """Two stage-core samples ~lag_s apart: the instantaneous rule, then the differenced one.

    Kept to roughly lag_s of added wall time (the gate runs once, before a window). The second
    sample is one more ps; the interval is measured, not assumed, so the snapshot says what
    really elapsed.
    """
    try:
        first_text, first_pid = _ps_snapshot()
    except (OSError, subprocess.SubprocessError) as error:
        return {"error": str(error), "hot": [], "sampled": []}
    first = parse_core_snapshot(first_text, ps_pid=first_pid)
    # jiffy-precision CPU for the threads the interval rule cares about, taken at both ends
    first_keys = stage_core_keys(first_text, ps_pid=first_pid)
    cpu_first = thread_cpu_seconds(first_keys)
    started = time.monotonic()
    time.sleep(lag_s)
    second_error = None
    try:
        second_text, second_pid = _ps_snapshot()
    except (OSError, subprocess.SubprocessError) as error:
        second_text, second_pid = None, None
        second_error = str(error)
    interval = round(time.monotonic() - started, 2)
    sampled = []
    if second_text is not None:
        cpu_second = thread_cpu_seconds(first_keys)
        if cpu_first and not cpu_second:        # /proc restricted: fall back to ps cputimes
            cpu_first = cpu_second = None
        sampled = sampled_hot(first_text, second_text, ps_pid=first_pid,
                              cpu_first=cpu_first, cpu_second=cpu_second)
        # a thread hot in EITHER instant keeps the instantaneous finding; the second listing is
        # also parsed by the instantaneous rule so a burst that started during the interval is
        # not missed by sample 1 alone
        second = parse_core_snapshot(second_text, ps_pid=second_pid)
        seen = {(r["pid"], r["tid"]) for r in first["hot"]}
        for row in second["hot"]:
            if (row["pid"], row["tid"]) not in seen:
                row["found_by"] = "second sample"
                first["hot"].append(row)
        seen_y = {(r["pid"], r["tid"]) for r in first["young"]}
        for row in second["young"]:
            if (row["pid"], row["tid"]) not in seen_y:
                first["young"].append(row)
        if len(second["unknown_age"]) > len(first["unknown_age"]):
            first["unknown_age"] = second["unknown_age"]
        if len(second["unparsed_age"]) > len(first["unparsed_age"]):
            first["unparsed_age"] = second["unparsed_age"]
        # the second instant's own findings, for the live selftest's "found only by sampling"
        first["hot_pids_second"] = [(r["pid"], r["tid"]) for r in second["hot"]]
    for row in sampled:
        row["lag_s"] = interval
    merged = []
    seen = set()
    for row in first["hot"] + sampled:
        key = (row["pid"], row["tid"])
        if key in seen:
            continue
        seen.add(key)
        row.setdefault("found_by", "instantaneous")
        merged.append(row)
    for row in merged:
        row["cmd"] = row.get("cmd") or cmdline(row["pid"])[:120]
    first["hot"] = merged
    first["sampled"] = sampled
    first["sample"] = {"lag_s": interval, "requested_lag_s": lag_s,
                       "cpu_advanced_limit_s": SAMPLE_CPU_S,
                       "young_cpu_limit_s": YOUNG_CPU_S, "min_age_s": HOT_MIN_AGE_S,
                       # the ps children excluded by parentage, for audit: neither may ever
                       # appear in hot/young/sampled
                       "excluded_ps_pids": [first_pid] + ([second_pid] if second_pid else []),
                       # a failed second sample disables the interval rule; said out loud
                       "second_sample_error": second_error}
    return first


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
        if cores.get("error"):
            # a listing we could not take is not evidence of a quiet box: fail closed
            failures.append("stage-core snapshot failed (" + str(cores["error"]) + "): "
                            "cannot rule out a hog on the stage cores")
        if cores.get("hot"):
            failures.append("hot non-pinned thread(s) on the stage cores: "
                            + ", ".join(_hot_label(h) for h in cores["hot"][:5]))
    if free_gib is not None:
        evaluated.append("disk")
        if free_gib < C.PREREQ0["cache_free_gib_min"]:
            failures.append("cache filesystem free " + str(round(free_gib, 1)) + " GiB < "
                            + str(C.PREREQ0["cache_free_gib_min"]) + " GiB (disk, not RAM)")
    else:
        skipped.append("disk")
    return {"failures": failures, "evaluated": evaluated, "skipped": skipped, "waived": waived}


def _hot_label(row):
    """One flagged thread in a FAIL line: pid/comm, the reading that flagged it, and how."""
    if row.get("found_by") == "sampling":
        interval = (" in the " + str(row["lag_s"]) + "s sample") if row.get("lag_s") else ""
        return (str(row["pid"]) + "/" + str(row.get("comm", "")) + " +"
                + str(row.get("cpu_advanced_s")) + "s CPU" + interval + " (sampled)")
    if row.get("age_s") == -1:
        return str(row["pid"]) + "/" + str(row.get("comm", "")) + " " + str(row["pcpu"]) + "% (age unknown)"
    return str(row["pid"]) + "/" + str(row.get("comm", "")) + " " + str(row.get("pcpu")) + "%"


def cpu_text_lines(cores, loadavg, prod_up=False):
    """The two 'cpu' lines of the text report. A function so the render path is in the selftest."""
    hot = cores.get("hot") or []
    young = cores.get("young") or []
    unparsed = cores.get("unparsed_age") or []
    sample = cores.get("sample") or {}
    sampled_rows = [h for h in hot if h.get("found_by") == "sampling"]
    line = ("  cpu         : loadavg " + " ".join(str(x) for x in loadavg)
            + "; hot stage-core threads: "
            + ((str(len(hot)) + " (" + ", ".join(_hot_label(h) for h in hot[:5]) + ")")
               if hot else "none")
            + "  young: " + str(len(young)) + " (pid "
            + (", ".join(str(h["pid"]) + " age " + str(h.get("age_s")) + "s cpu "
                         + str(h.get("cputimes_s")) + "s" for h in young[:5])
               if young else "none")
            + ")"
            + "  unparsed-age rows: " + str(len(unparsed))
            + ((" (NOT exempt, e.g. " + ", ".join(str(h["pid"]) + " '" + str(h["age_raw"])
                                                  + "'" for h in unparsed[:3]) + ")")
               if unparsed else "")
            + ("  [not evaluated (prod up)]" if prod_up else ""))
    lines = [line]
    if sample:
        lines.append("  cpu sample  : " + str(len(sampled_rows))
                     + " thread(s) flagged only by the " + str(sample.get("lag_s"))
                     + "s interval (> " + str(sample.get("cpu_advanced_limit_s"))
                     + "s CPU advanced), young exempt only under "
                     + str(sample.get("young_cpu_limit_s")) + "s CPU")
    return lines


def verdict_label(failures, waived):
    """OPEN, OPEN with the waived gate(s) named, or REFUSED. Any failure refuses, always."""
    if failures:
        return "REFUSED"
    if waived:
        return ("OPEN (WAIVED: " + ", ".join(str(w.get("gate") or "unknown") for w in waived)
                + " by operator)")
    return "OPEN"


BURN_PRELUDE = (
    "import time\n"
    "def burn(s):\n"
    "    t = time.process_time()\n"
    "    while time.process_time() - t < s: pass\n"
)


def _spawn_pinned(code, core):
    """A userspace child pinned to one stage core. No engine, no GPU, seconds of life."""
    return subprocess.Popen(["taskset", "-c", str(core), sys.executable, "-c", code])


def _quiet_stage_core(text=None):
    """The stage core carrying the least %CPU in a listing (this box also runs a game).

    Only used by the selftest, to give its synthetic hogs a core that is actually free.
    """
    if text is None:
        text = _ps_snapshot()[0]
    stage = sorted(C.PREREQ0["stage_cores"])
    load = {core: 0.0 for core in stage}
    for line in text.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        try:
            psr, pcpu = int(parts[2]), float(parts[3])
        except ValueError:
            continue
        if psr in load:
            load[psr] += pcpu
    return min(stage, key=lambda c: (load[c], c))


def live_stage_core_checks():
    """The rule on REAL ps output: the cases the 2026-11-25 fix is about.

    Two userspace processes pinned to one stage core, nothing served or booted:
      * a burst that has consumed 3 s of CPU at 3 s of age must be FLAGGED (the old age-only
        rule made it permanently invisible);
      * the gate's own ps child, which shows ~300 % in its own listing, must never appear --
        excluded by the pid ps was launched with, structurally;
      * a duty-cycled thread that advances >0.5 s of CPU across the 2 s interval while
        reading under the 50 % line (%CPU is a lifetime average) must be flagged by sampling.
    """
    if not shutil.which("taskset"):
        print("selftest: live stage-core checks SKIPPED (no taskset)")
        return
    # this box also runs a game, so the least-loaded stage core is used; the assertion below
    # is on the real row's real cputimes and age, not on %CPU (which the game depresses)
    stage = _quiet_stage_core()
    me = os.getpid()
    burst = _spawn_pinned(BURN_PRELUDE + "while True: pass\n", stage)
    duty = None
    try:
        # wait until the burst has really consumed 3 s of CPU, then take one real listing
        deadline = time.time() + 20
        used = 0.0
        while time.time() < deadline:
            used = thread_cpu_seconds([(burst.pid, burst.pid)]).get((burst.pid, burst.pid), 0.0)
            if used >= 3.0:
                break
            time.sleep(0.05)
        text, ps_pid = _ps_snapshot()
        label = str(burst.pid)
        raw = next((r for r in text.splitlines() if r.split() and r.split()[0] == label), "")
        # the real row: a 3 s burst at ~3-6 s of age. %CPU is whatever the box left it (the
        # game steals the stage core), so the row's own pcpu column is raised to 100 for the
        # rule's test -- age and cputimes stay the measured values. That is exactly the case
        # the old age-only rule hid: young AND 3 s of CPU.
        assert raw, "the burst process was not in the ps listing: " + label
        cols = raw.split(None, 6)
        assert int(cols[2]) == stage, "burst not on stage core " + str(stage) + ": " + raw
        assert float(cols[5]) >= YOUNG_CPU_S and int(cols[4]) < HOT_MIN_AGE_S, raw
        forced = " ".join([label, cols[1], cols[2], "100.0", cols[4], cols[5], "python3"]) + "\n"
        snap = parse_core_snapshot(forced, ps_pid=ps_pid, self_pid=me)
        found = [r for r in snap["hot"] if r["pid"] == burst.pid]
        assert found, ("a stage-core burst at " + str(round(used, 2)) + " s CPU / age "
                       + str(cols[4]) + " s was NOT flagged; row: " + raw)
        assert found[0]["age_s"] < HOT_MIN_AGE_S and found[0]["cputimes_s"] >= YOUNG_CPU_S, found
        # the same row with the REAL %CPU must not be exempt by age alone
        real_snap = parse_core_snapshot(text, ps_pid=ps_pid, self_pid=me)
        young_ids = {r["pid"] for r in real_snap["young"]}
        assert burst.pid not in young_ids, ("the burst was exempted as young: "
                                            + raw + " " + repr(real_snap["young"]))
        # our own ps child: present in its own listing at a meaningless %CPU, never exempt-by-pid
        assert ps_pid not in {r["pid"] for r in snap["hot"] + snap["young"]}, snap
        assert me not in {r["pid"] for r in snap["hot"] + snap["young"]}, snap
        burst.kill()
        burst.wait(timeout=5)

        # duty cycle: idle 4.5 s so %CPU (a lifetime average) reads well under the 50 % line,
        # then half a core continuously -- burn 0.15 s, sleep 0.15 s. Any 2 s window carries
        # ~0.65 s of CPU and no single instant is a hog, yet the interval difference finds it.
        # A fresh least-loaded core, so the burst above cannot have queued behind it.
        stage = _quiet_stage_core()
        duty = _spawn_pinned(BURN_PRELUDE
                             + "time.sleep(4.5)\n"
                               "t = time.time()\n"
                               "while time.time() - t < 9.0:\n"
                               "    burn(0.15)\n"
                               "    time.sleep(0.15)\n",
                             stage)
        started = time.time()
        while time.time() - started < 5.0:
            time.sleep(0.05)
        cores = temp_core_snapshot(SAMPLE_LAG_S)
        instant_second = {(r["pid"], r["tid"]) for r in cores["hot"]
                          if r.get("found_by") != "sampling"}
        instant_second |= {tuple(x) for x in cores.get("hot_pids_second") or []}
        sampled = [r for r in cores["sampled"] if r["pid"] == duty.pid]
        assert sampled, ("duty-cycled thread (pid " + str(duty.pid) + ") not found by sampling: "
                         + repr(cores["sample"]))
        assert sampled[0]["cpu_advanced_s"] > SAMPLE_CPU_S, sampled
        assert (duty.pid, duty.pid) not in instant_second, instant_second
        assert cores["sample"]["lag_s"] >= 1.5, cores["sample"]
        print("selftest: live stage-core checks OK (burst " + str(round(used, 2)) + " s CPU flagged; "
              "ps child " + str(ps_pid) + " excluded by pid; duty-cycled thread advanced "
              + str(sampled[0]["cpu_advanced_s"]) + " s CPU in " + str(cores["sample"]["lag_s"])
              + " s, found only by sampling)")
    finally:
        for proc in (burst, duty):
            if proc is None or proc.poll() is not None:
                continue
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def selftest():
    """The rule, on a synthetic ps snapshot: engine threads pass, an intruder is flagged.

    Also the two states of the gate: a clean pre-boot passes, a starved one names memory, and
    a prod-up box refuses with 'stop prod first' alone while the RAM/CPU gates are skipped.
    """
    text = (
        "    PID     TID PSR %CPU ELAPSED     TIME COMMAND\n"
        " 1001    1001   0  1.0  100:00      99 hostapp\n"
        " 2002    2003   5 99.0  010:00    5900 strata\n"   # the engine's pinned stage thread
        " 3003    3003   7 97.5  108:00   63000 pytest\n"   # the incident shape: a stuck test
        " 3004    3004   9 12.0  108:00    9400 python\n"   # low load: not hot
        " 4004    4004   3 51.0  001:00      31 kworker/3:1\n"  # just over the line
    )
    kinds = {2002: "serving", 3003: "other", 3004: "other", 4004: "other", 1001: "other"}
    snap = parse_core_snapshot(text, classify_fn=lambda pid: kinds.get(pid, "other"), self_pid=1)
    flagged = sorted(row["pid"] for row in snap["hot"])
    assert flagged == [3003, 4004], flagged
    assert snap["threads_per_cpu"]["5"] == 1 and snap["threads_per_cpu"]["0"] == 1, snap

    # etime forms: etimes seconds, [[dd-]hh:]mm:ss, and unparseable -> None (fail closed)
    assert parse_etime("7") == 7 and parse_etime("00:07") == 7
    assert parse_etime("01:00") == 60 and parse_etime("108:00") == 6480
    assert parse_etime("2-01:02:03") == 2 * 86400 + 3723 and parse_etime("1:02:03") == 3723
    assert parse_etime("") is None and parse_etime("xx:yy") is None and parse_etime("-1") is None

    # 2026-11-25: the gate's own `ps` child appears in its own listing at a meaningless
    # 300 % (%CPU is cputime/elapsed, and elapsed is ~0). The ps pid is excluded by parentage
    # (ps_pid, captured from Popen), AND a young row is exempt only when it consumed under
    # 0.5 s of CPU: a 3 s build step that burned 3 s of CPU is flagged, age notwithstanding.
    young_text = (
        "    PID   TID PSR %CPU ELAPSED TIME COMMAND\n"
        "  501   5010  11 300.0       0    0 ps\n"          # our own ps child, on a stage core
        " 5002   5002   5  88.0   15:00  720 borg\n"        # old and hot (etime form)
        " 5003   5003   7  51.0       4    0 ps\n"          # young, negligible CPU: exempt
        " 5004   5004   9 120.0       3    3 ninja\n"       # 3 s old AND 3 s of CPU: a burst
        " 5006   5006  13  60.0       2    0 cron\n"        # young, negligible CPU: exempt
        " 5005   5005  15  70.0   nope   40 mystery\n"      # age unparseable: fail closed
    )
    young_snap = parse_core_snapshot(young_text, classify_fn=lambda pid: "other",
                                     self_pid=1, ps_pid=501)
    assert [r["pid"] for r in young_snap["hot"]] == [5002, 5004, 5005], young_snap
    assert [r["pid"] for r in young_snap["young"]] == [5003, 5006], young_snap
    assert [r["pid"] for r in young_snap["unknown_age"]] == [5005], young_snap
    assert [r["pid"] for r in young_snap["unparsed_age"]] == [5005], young_snap
    # an idle box whose ps format changed is still visible: the unparsed count is global
    fmt = "  1 1 0 0.0 nope 0 systemd\n  2 2 0 0.0 7358 2 kthreadd\n"
    fmt_snap = parse_core_snapshot(fmt, classify_fn=lambda pid: "other", self_pid=1)
    assert [r["pid"] for r in fmt_snap["unparsed_age"]] == [1], fmt_snap
    assert fmt_snap["hot"] == [] and fmt_snap["rows"] == 2, fmt_snap
    # the ps child is excluded by parentage even when it has consumed CPU and is old
    old_ps = "  501   5010  11 300.0 900 900 ps\n 5002 5002 5 88.0 900 720 borg\n"
    old_ps_snap = parse_core_snapshot(old_ps, classify_fn=lambda pid: "other",
                                      self_pid=1, ps_pid=501)
    assert [r["pid"] for r in old_ps_snap["hot"]] == [5002], old_ps_snap

    # the exact case from 2026-11-25: the gate's own ps child, 300 % on a stage core, 0.01 s of
    # CPU. Exempt by pid (structural); also exempt by age+cputimes if the pid were unknown,
    # and never in the hot list either way.
    child = "  777   7770  13 300.0       0    0 ps\n"
    by_pid = parse_core_snapshot(child, classify_fn=lambda pid: "other", self_pid=1, ps_pid=777)
    assert by_pid["hot"] == [] and by_pid["young"] == [], by_pid
    by_rule = parse_core_snapshot(child, classify_fn=lambda pid: "other", self_pid=1)
    assert by_rule["hot"] == [] and [r["pid"] for r in by_rule["young"]] == [777], by_rule
    # ... and an OLD ps child is still excluded by pid, so age never has to cover for it
    assert parse_core_snapshot("  777 7770 13 300.0 900 900 ps\n",
                               classify_fn=lambda pid: "other", self_pid=1,
                               ps_pid=777)["hot"] == []

    # the 2 s interval at 1.2 s of advance: found by sampling, with the ps child and the engine
    # excluded even though both advanced
    dur_first = "  801 8010 5 1 100 100 dutyhog\n  802 8020 11 1 0 0 ps\n 2002 2003 7 1 100 100 strata\n"
    dur_second = "  801 8010 5 1 102 101.2 dutyhog\n  802 8020 11 1 0 0 ps\n 2002 2003 7 1 102 106 strata\n"
    dur = sampled_hot(dur_first, dur_second, classify_fn=lambda pid: "serving" if pid == 2002 else "other",
                      self_pid=1, ps_pid=802)
    assert [(r["pid"], r["cpu_advanced_s"]) for r in dur] == [(801, 1.2)], dur

    # the second, time-differenced sample: a duty-cycled thread reads under the line at both
    # instants (40 % of a core, 0.8 s of CPU each way in 2 s) but advanced 1.2 s: sampling
    # finds it. A thread that advanced 0.2 s, one on a non-stage core and the ps child do not.
    first_text = (
        "    PID   TID PSR %CPU ELAPSED TIME COMMAND\n"
        "  601   6010   3  40.0      60   24 dutyhog\n"      # +1.2 s over the interval
        "  602   6020   5  10.0      60    6 mild\n"        # +0.2 s: under the limit
        "  603   6030   8  40.0      60   24 offstage\n"    # advanced, but not a stage core
        "  604   6040  11 300.0       0    0 ps\n"          # our own ps child
        " 2002   2003   7  99.0      60  120 strata\n"      # the engine, pinned: never flagged
    )
    second_text = (
        "    PID   TID PSR %CPU ELAPSED TIME COMMAND\n"
        "  601   6010   3  40.0      62    25 dutyhog\n"    # 24 -> 25 (cputimes is seconds)
        "  602   6020   5  10.0      62     6 mild\n"
        "  603   6030   8  40.0      62    25 offstage\n"
        "  605   6050  11  90.0       0     0 ps\n"          # the second sample's own child
        " 2002   2003   7  99.0      62  1900 strata\n"
    )
    # /proc jiffy maps: the interval rule at 0.01 s resolution
    cpu_first = {(601, 6010): 100.00, (602, 6020): 50.00, (603, 6030): 100.00,
                 (2002, 2003): 100.00}
    cpu_second = {(601, 6010): 101.20, (602, 6020): 50.20, (603, 6030): 101.20,
                  (2002, 2003): 190.00}
    kinds2 = {601: "other", 602: "other", 603: "other", 2002: "serving"}
    sampled = sampled_hot(first_text, second_text,
                          classify_fn=lambda pid: kinds2.get(pid, "other"),
                          self_pid=1, ps_pid=604,
                          cpu_first=cpu_first, cpu_second=cpu_second)
    assert [r["pid"] for r in sampled] == [601], sampled
    assert sampled[0]["cpu_advanced_s"] == 1.2 and sampled[0]["found_by"] == "sampling", sampled
    # the ps cputimes fallback agrees on the coarse case (24 -> 25 s advanced = 1 s)
    fb = sampled_hot("  601 6010 3 90 60 24 dutyhog\n", "  601 6010 3 90 62 25 dutyhog\n",
                     classify_fn=lambda pid: "other", self_pid=1)
    assert [r["pid"] for r in fb] == [601], fb

    # the text renderer: every row shape goes through it (a sampled row used to KeyError here,
    # which the selftest caught only after a live run did)
    rendered = cpu_text_lines(
        {"hot": young_snap["hot"] + sampled, "young": young_snap["young"],
         "unparsed_age": young_snap["unparsed_age"],
         "sample": {"lag_s": 2.11, "cpu_advanced_limit_s": SAMPLE_CPU_S,
                    "young_cpu_limit_s": YOUNG_CPU_S}},
        [1.0, 0.5, 0.4])
    assert "young: 2 (pid 5003 age 4s cpu 0.0s, 5006 age 2s cpu 0.0s)" in rendered[0], rendered
    assert "unparsed-age rows: 1 (NOT exempt, e.g. 5005 'nope')" in rendered[0], rendered
    assert "hot stage-core threads: 4 (" in rendered[0], rendered
    assert "601/dutyhog +1.2s CPU (sampled)" in rendered[0], rendered
    assert rendered[1].startswith("  cpu sample  : 1 thread(s) flagged only by the 2.11s"), rendered
    assert "(age unknown)" in rendered[0], rendered

    procs = [{"pid": 4001, "rss_mib": 900, "kind": "other", "cmd": "python one.py"}]
    clean = gate_verdict(40 * GIB, 900, procs, {"hot": []}, [], 100.0)
    assert clean["failures"] == [] and clean["skipped"] == [], clean
    starved = gate_verdict(10 * GIB, 900, procs, {"hot": []}, [], 100.0)
    assert len(starved["failures"]) == 1 and "MemAvailable" in starved["failures"][0], starved
    # a ps failure is a refusal, not a quiet box (it used to pass the stage-core gate)
    blind = gate_verdict(40 * GIB, 900, procs, {"error": "ps exited 1: nope", "hot": []}, [], 100.0)
    assert len(blind["failures"]) == 1 and "stage-core snapshot failed" in blind["failures"][0], blind
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
          "a 3 s stage-core burst at 3 s of CPU is flagged while the gate's own 300 % ps child "
          "is excluded by pid and by <0.5 s of CPU; an old 88 %/900 s hog is flagged; an "
          "unparseable age adds to the refusals and is never exempt (fail closed); a "
          "duty-cycled thread advancing 1.2 s of CPU across the 2 s sample is found only by "
          "sampling; clean pre-boot passes, a starved one fails on memory, and a prod-up box "
          "refuses only with 'stop prod first' (RAM/CPU gates not evaluated); the non-engine "
          "RSS sum refuses unwaived, is WAIVED (operator, reading + journal recorded) per "
          "invocation, and memory, stage-core, disk and prod-up still refuse with the waiver set")
    live_stage_core_checks()





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
                "unknown_age_stage_core_threads": cores.get("unknown_age"),
                "unparsed_age_rows": cores.get("unparsed_age"),
                "ps_rows": cores.get("rows"),
                "sampled_stage_core_threads": cores.get("sampled"),
                "sample": cores.get("sample"),
                "ps_error": cores.get("error"),
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
        for line in cpu_text_lines(cores, loadavg, prod_up):
            print(line)
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

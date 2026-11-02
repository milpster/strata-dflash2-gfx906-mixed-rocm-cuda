#!/usr/bin/env python3
"""Opt-in private fixed-fill-cap sweep; always restore supervised tuned production."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import time
import urllib.request

import helper_ab
import pair_supervisor as pair
import recovery_window as runtime
import window_pair


class ExistingProcess:
    """PID/start-time handle for an already-running, identity-verified supervisor."""
    def __init__(self, pid, start):
        self.pid, self.start = pid, start

    def poll(self):
        return None if pair.alive(self.pid, self.start) else 0

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("owned supervisor", timeout)
            time.sleep(0.1)
        return 0


def graceful_stop(active, state, known, engine_wait_s=120, stop_timeout=45):
    """Deterministic close capture for a private window pair.

    The server owns graceful engine shutdown (pair_supervisor.stop_group deliberately
    does not TERM isolated engines early). slots3 lost arm-b's prompt-channel close
    line because stop_group's bounded SIGKILL tail could reach the engine before it
    printed teardown stats. Here: TERM only the server, let the engine finish its
    graceful exit (start-token-checked, bounded), then the usual bounded stop_group
    cleans the remainder. Seed/history start tokens are never dropped.
    """
    engine_pid = state.get("engine_pid")
    engine_token = pair.process_token(engine_pid) if engine_pid is not None else None
    engine_start = engine_token[1] if engine_token is not None else None
    try:
        os.kill(state["server_pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + engine_wait_s
    while engine_start is not None and time.monotonic() < deadline:
        known.update(pair.descendants(active.pid, known))  # token-preserving refresh
        if not pair.alive(engine_pid, engine_start):
            break
        time.sleep(1)
    pair.stop_group(active, stop_timeout, known)


def stop_production(pid, result, *, server_pid=None):
    """Stop the canonical swift15 production pair, supervised or direct.

    Supervised form: --expected-supervisor names a live pair_supervisor whose argv,
    state file and start tokens all match (strict identity). Direct form (user
    started ./launch-prod-...sh by hand): --expected-server names the serve/server.py
    process whose argv carries the canonical CONFIG and whose parent is the canonical
    launcher; teardown is the graceful server-first stop_existing path. Either way
    only the verified owned tree is signalled and only its observed sockets released.
    """
    if server_pid is not None:
        stopped = runtime.stop_existing(server_pid, result)  # sets outage_started, waits, releases
        stopped["form"] = "direct"
        result["old_production"] = stopped
        return
    argv = runtime.command(pid)
    required = [str(runtime.WORK / "tools/mixed_gpu/pair_supervisor.py"),
                str(runtime.LAUNCHER), str(runtime.STATE)]
    if not all(value in argv for value in required):
        raise RuntimeError("Production supervisor identity mismatch; refusing outage")
    state = json.loads(runtime.STATE.read_text())
    if state["supervisor_pid"] != pid:
        raise RuntimeError("Production state belongs to another supervisor")
    start = pair.process_token(pid)[1]
    known = pair.descendants(pid, {pid: start})
    owned = {path: token for path in runtime.SOCKETS if (token := pair.socket_token(path)) is not None}
    result["old_production"] = dict(supervisor_pid=pid, processes=known)
    result["outage_started"] = True
    pair.stop_group(ExistingProcess(pid, start), 45, known)
    pair.release_sockets(owned)
    result["old_production"]["form"] = "supervised"


def fill_report(log):
    filled = helper_ab.FILL_RE.search(log)
    stopped = re.search(r"fill stopped: ([^;]+); device free now (\d+) MiB of (\d+) MiB "
                        r"\((\d+) slots, ([\d.]+) GiB uploaded\)", log)
    if not filled or not stopped or int(filled[1]) != int(stopped[4]):
        raise ValueError("Missing or inconsistent actual fill accounting")
    if "second-channel engine connected" not in log:
        raise ValueError("Prompt channel did not attach")
    return dict(slots=int(filled[1]), gib_rounded=float(filled[2]), stop_reason=stopped[1],
                device_free_mib=int(stopped[2]), device_total_mib=int(stopped[3]))


def restore_production(result, family):
    restored = runtime.launch(True, runtime.EV / f"{family}-restore-console.log")
    state = runtime.wait_for(lambda: runtime.attached(supervisor_pid=restored.pid))
    runtime.check_fill(state)
    state["engine_pid"] = runtime.engine_of(state)
    body = json.dumps({"model": "swift-1.5-iq3_xxs", "messages": [
        {"role": "user", "content": "Reply with just the word ready."}],
        "max_tokens": 96, "temperature": 0}).encode()
    request = urllib.request.Request("http://127.0.0.1:8009/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=180) as response:
        raw = response.read()
        code = response.status
    (runtime.EV / f"{family}-restored-smoke.json").write_bytes(raw)
    reply = json.loads(raw)
    if code != 200 or not reply["choices"][0]["message"].get("content"):
        raise RuntimeError("Restored production did not generate content")
    result["restored"] = dict(state, completion_http=code)


def draft_report(engine_log, helper_log, enabled):
    engaged = "strata draft-helper: drafting on the helper:" in engine_log
    counts = re.findall(r", (\d+) draft context rows, (\d+) draft rounds", helper_log)
    if not enabled:
        if engaged or counts or "expert-helper: dflash " in helper_log:
            raise RuntimeError("OFF arm unexpectedly loaded or used the remote drafter")
        return dict(enabled=False, context_rows=0, rounds=0)
    failures = re.findall(r"strata draft-helper:[^\n]*(?:failed|refused|drafting stays|names)[^\n]*", engine_log)
    if not engaged or failures or len(counts) != 1 or not all(int(n) > 0 for n in counts[0]):
        raise RuntimeError(f"Missing clean positive remote-draft service proof: {failures}")
    return dict(enabled=True, context_rows=int(counts[0][0]), rounds=int(counts[0][1]))


def draft_comparison(rows):
    local = [p for row in rows if not row["draft_helper"] for p in row["probes"]]
    remote = [p for row in rows if row["draft_helper"] for p in row["probes"]]
    med = lambda probes, key: statistics.median(p[key] for p in probes)
    ratio = med(remote, "gen_rate") / med(local, "gen_rate")
    drift = rows[-1]["generated_median"] / rows[0]["generated_median"]
    return dict(local_decode_median=med(local, "gen_rate"), remote_decode_median=med(remote, "gen_rate"),
                remote_local_ratio=ratio, final_initial_control_ratio=drift,
                local_elapsed_median=med(local, "elapsed_s"), remote_elapsed_median=med(remote, "elapsed_s"),
                local_acceptance=sum(p["accepted"] for p in local) / sum(p["offered"] for p in local),
                remote_acceptance=sum(p["accepted"] for p in remote) / sum(p["offered"] for p in remote),
                verdict=("INCONCLUSIVE_CONTROL_DRIFT" if not 0.9 <= drift <= 1.1 else
                         "MEETS_LONG_DECODE_GATE" if ratio >= 1.05 else "NO_LONG_DECODE_GAIN"),
                limits="No automatic adoption; one XL probe/arm is secondary evidence. Local drafter VRAM remains allocated.")


def knob_comparison(rows):
    """OFF/ON/OFF config-knob verdict; the L7 slow-prefill fraction stays visible."""
    off = [p for row in rows if not row["knob_on"] for p in row["probes"]]
    on = [p for row in rows if row["knob_on"] for p in row["probes"]]
    med = lambda probes, key: statistics.median(p[key] for p in probes)
    slow = lambda probes: sum(p["read_ms"] > 120000 for p in probes) / len(probes)
    drift = med(rows[-1]["probes"], "gen_rate") / med(rows[0]["probes"], "gen_rate")
    ratio = med(on, "gen_rate") / med(off, "gen_rate")
    return dict(off_decode_median=med(off, "gen_rate"), on_decode_median=med(on, "gen_rate"),
                on_off_decode_ratio=ratio, control_drift=drift,
                off_read_ms_median=med(off, "read_ms"), on_read_ms_median=med(on, "read_ms"),
                off_slow_prefill_fraction=slow(off), on_slow_prefill_fraction=slow(on),
                off_elapsed_median=med(off, "elapsed_s"), on_elapsed_median=med(on, "elapsed_s"),
                verdict=("INCONCLUSIVE_CONTROL_DRIFT" if not 0.9 <= drift <= 1.1 else
                         "KNOB_CANDIDATE" if ratio >= 1.05 else "KNOB_NO_GAIN"),
                limits="Config-only OFF/ON/OFF engine-argv window; slow fraction uses "
                       "read_ms>120s (the L7 bimodality threshold); no adoption without "
                       "its own repeated evidence.")


def l2_comparison(rows):
    """OFF/ON/OFF helper-stream-split verdict on the same l2 binary; the gate is the
    lever's >=+5% long-arm decode median; the L7 slow-prefill fraction stays visible."""
    off = [p for row in rows if not row["l2_streams"] for p in row["probes"]]
    on = [p for row in rows if row["l2_streams"] for p in row["probes"]]
    med = lambda probes, key: statistics.median(p[key] for p in probes)
    slow = lambda probes: sum(p["read_ms"] > 120000 for p in probes) / len(probes)
    drift = med(rows[-1]["probes"], "gen_rate") / med(rows[0]["probes"], "gen_rate")
    ratio = med(on, "gen_rate") / med(off, "gen_rate")
    return dict(off_decode_median=med(off, "gen_rate"), on_decode_median=med(on, "gen_rate"),
                on_off_decode_ratio=ratio, control_drift=drift,
                off_read_ms_median=med(off, "read_ms"), on_read_ms_median=med(on, "read_ms"),
                off_slow_prefill_fraction=slow(off), on_slow_prefill_fraction=slow(on),
                off_elapsed_median=med(off, "elapsed_s"), on_elapsed_median=med(on, "elapsed_s"),
                verdict=("INCONCLUSIVE_CONTROL_DRIFT" if not 0.9 <= drift <= 1.1 else
                         "L2_CANDIDATE" if ratio >= 1.05 else "L2_NO_GAIN"),
                limits="Helper-env OFF/ON/OFF A/B on the rpcw-cuda-l2 helper binary "
                       "(engine byte-reused); the OFF arms are the env-unset "
                       "behaviour-identity control; no adoption without its own "
                       "repeated evidence.")


def l8_wire_comparison(rows):
    """OFF(local)/ON(remote fp32)/ON(remote bf16)/OFF(local) wire verdict; bf16 taps are
    lossy, so draft acceptance must not get worse alongside any decode gain."""
    med = lambda arm: statistics.median(p["gen_rate"] for p in arm)
    acc = lambda arm: (sum(p["accepted"] for p in arm) / sum(p["offered"] for p in arm)
                       if sum(p["offered"] for p in arm) else 0.0)
    local = [p for row in rows if not row["draft_helper"] for p in row["probes"]]
    f32 = [p for row in rows if row["draft_helper"] and not row.get("bf16_taps")
           for p in row["probes"]]
    bf16 = [p for row in rows if row.get("bf16_taps") for p in row["probes"]]
    drift = med(rows[-1]["probes"]) / med(rows[0]["probes"])
    ratio = med(bf16) / med(f32)
    return dict(local_decode_median=med(local), remote_f32_decode_median=med(f32),
                remote_bf16_decode_median=med(bf16), bf16_f32_decode_ratio=ratio,
                bf16_local_decode_ratio=med(bf16) / med(local), control_drift=drift,
                local_acceptance=acc(local), f32_acceptance=acc(f32), bf16_acceptance=acc(bf16),
                verdict=("INCONCLUSIVE_CONTROL_DRIFT" if not 0.9 <= drift <= 1.1 else
                         "L8_WIRE_GAIN" if ratio >= 1.05 and acc(bf16) >= acc(f32) - 0.02 else
                         "L8_NO_GAIN"),
                limits="Wire-only OFF(local)/ON(f32)/ON(bf16)/OFF(local) bracket on the l8 "
                       "binaries, ON arms remote-only so only the wire differs between b and "
                       "c; the >=+5% gate is c-vs-b decode with the acceptance guard (bf16 not "
                       "worse than f32 by more than 2 points); no adoption.")


def overlap_comparison(rows):
    """OFF/ON/OFF split-overlap verdict on the production binaries; the gate is the
    lever's >=+5% long-arm decode median (upstream measured +24-32% decode on 2x
    gfx1100; unmeasured on gfx906); the L7 slow-prefill fraction stays visible."""
    off = [p for row in rows if not row["split_overlap"] for p in row["probes"]]
    on = [p for row in rows if row["split_overlap"] for p in row["probes"]]
    med = lambda probes, key: statistics.median(p[key] for p in probes)
    slow = lambda probes: sum(p["read_ms"] > 120000 for p in probes) / len(probes)
    drift = med(rows[-1]["probes"], "gen_rate") / med(rows[0]["probes"], "gen_rate")
    ratio = med(on, "gen_rate") / med(off, "gen_rate")
    return dict(off_decode_median=med(off, "gen_rate"), on_decode_median=med(on, "gen_rate"),
                on_off_decode_ratio=ratio, control_drift=drift,
                off_read_ms_median=med(off, "read_ms"), on_read_ms_median=med(on, "read_ms"),
                off_slow_prefill_fraction=slow(off), on_slow_prefill_fraction=slow(on),
                off_elapsed_median=med(off, "elapsed_s"), on_elapsed_median=med(on, "elapsed_s"),
                verdict=("INCONCLUSIVE_CONTROL_DRIFT" if not 0.9 <= drift <= 1.1 else
                         "OVERLAP_CANDIDATE" if ratio >= 1.05 else "OVERLAP_NO_GAIN"),
                limits="Engine-env OFF/ON/OFF A/B on the production binaries (no rebuild, "
                       "no helper change); only the ON arm exports STRATA_SPLIT_OVERLAP=1 "
                       "and its engine log carries '(overlapped: ...)' as the engagement "
                       "proof; no adoption without its own repeated evidence.")


def helper_off_comparison(rows):
    """Helper-less vs canonical A/B: the user-asked measurement of what the RPC expert
    helper actually adds (infill = read_rate tok/s, TG = gen_rate tok/s). OFF arms are
    helper_ab's validated OFF derivation; engagement gates prove the split (OFF arms zero
    helper traffic, ON arm positive). Pure measurement - production is already canonical,
    so the verdicts describe the helper's value, never an adoption."""
    off = [p for row in rows if row["helper_off"] for p in row["probes"]]
    on = [p for row in rows if not row["helper_off"] for p in row["probes"]]
    off_rows = [row for row in rows if row["helper_off"]]
    med = lambda probes, key: statistics.median(p[key] for p in probes)
    drift = med(off_rows[-1]["probes"], "gen_rate") / med(off_rows[0]["probes"], "gen_rate")
    off_reqs = sum(p.get("helper_reqs", 0) for p in off)
    on_reqs = sum(p.get("helper_reqs", 0) for p in on)
    decode_ratio = med(on, "gen_rate") / med(off, "gen_rate")
    engagement = "HELPER_ENGAGEMENT_BROKEN" if off_reqs or not on_reqs else "OK"
    verdict = ("INCONCLUSIVE_CONTROL_DRIFT" if not 0.9 <= drift <= 1.1 else
               engagement if engagement != "OK" else
               "HELPER_GAIN_CONFIRMED" if decode_ratio >= 1.05 else
               "HELPER_GAIN_SMALL" if decode_ratio >= 1.0 else "HELPER_SLOWER")
    return dict(off_decode_median=med(off, "gen_rate"), on_decode_median=med(on, "gen_rate"),
                on_off_decode_ratio=decode_ratio,
                off_infill_median=med(off, "read_rate"), on_infill_median=med(on, "read_rate"),
                on_off_infill_ratio=med(on, "read_rate") / med(off, "read_rate"),
                off_read_ms_median=med(off, "read_ms"), on_read_ms_median=med(on, "read_ms"),
                off_elapsed_median=med(off, "elapsed_s"), on_elapsed_median=med(on, "elapsed_s"),
                control_drift=drift, off_helper_reqs_total=off_reqs, on_helper_reqs_total=on_reqs,
                verdict=verdict,
                limits="Measurement A/B, no adoption: OFF arms are helper_ab's validated OFF "
                       "derivation with the helper never started (supervisor helper_pid 0, "
                       "attach on the server's ready line); ON is the canonical supervised "
                       "pair; infill = read_rate, TG = gen_rate; request-SHA parity still "
                       "required (greedy tokens must not change).")


# ---- L7 (progressive degradation diagnosis): local resource samplers, best-effort ----

try:
    CLK_TCK = os.sysconf("SC_CLK_TCK")
except (OSError, ValueError):
    CLK_TCK = 100


def sample_process(pid, *, proc="/proc"):
    """One /proc snapshot of a process: faults, cpu ticks, resident MiB. None if it vanished."""
    try:
        base = Path(proc) / str(pid)
        stat = (base / "stat").read_text()
        tail = stat[stat.rfind(")") + 2:].split()           # stat field N (>=3) is tail[N - 3]
        fields = dict(minflt=int(tail[7]), majflt=int(tail[9]),
                      cpu_ticks=int(tail[11]) + int(tail[12]))
        fields["resident_mib"] = round(int((base / "statm").read_text().split()[1]) * 4096 / 2**20, 1)
        return fields
    except (OSError, ValueError, IndexError):
        return None


def _first_num(path):
    try:
        return float(Path(path).read_text().strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def sample_system(*, proc="/proc", sysfs="/sys"):
    """System snapshot: MemAvailable, memory PSI, per-card busy/temp/clock. Missing files stay None."""
    out = dict(mem_available_mib=None, psi_memory_avg10=None, gpus={})
    try:
        for line in (Path(proc) / "meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                out["mem_available_mib"] = int(line.split()[1]) // 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    try:
        first = (Path(proc) / "pressure" / "memory").read_text().splitlines()[0]
        out["psi_memory_avg10"] = float(first.split("avg10=")[1].split()[0])
    except (OSError, ValueError, IndexError):
        pass
    for card in ("card1", "card2", "card3"):
        base = Path(sysfs) / "class/drm" / card / "device"
        if not base.is_dir():
            continue
        info = {}
        busy = _first_num(base / "gpu_busy_percent")
        if busy is not None:
            info["busy_pct"] = round(busy, 1)
        for hwmon in sorted(base.glob("hwmon/hwmon*")):
            if "temp_c" not in info:
                micro = _first_num(hwmon / "temp1_input")
                if micro is not None:
                    info["temp_c"] = round(micro / 1000, 1)
            if "sclk_mhz" not in info:
                micro = _first_num(hwmon / "freq1_input")
                if micro is not None:
                    info["sclk_mhz"] = round(micro / 1e6, 1)
        out["gpus"][card] = info
    return out


def sample_pair(state):
    return dict(t=time.time(), engine=sample_process(state["engine_pid"]),
                helper=sample_process(state["helper_pid"]), system=sample_system())


def degrade_summary(probes, samples):
    """Per-probe deltas: what changed locally while the whole prefill slowed down."""
    rows = []
    for probe, pre, post in zip(probes, samples[0::2], samples[1::2]):
        row = dict(rep=probe["rep"], read_ms=probe["read_ms"], read_rate=probe["read_rate"],
                   gen_rate=probe["gen_rate"], elapsed_s=probe["elapsed_s"])
        eng_pre, eng_post = pre.get("engine") or {}, post.get("engine") or {}
        if eng_pre and eng_post:
            row["engine_majflt"] = eng_post["majflt"] - eng_pre["majflt"]
            row["engine_minflt"] = eng_post["minflt"] - eng_pre["minflt"]
            row["engine_cpu_s"] = round((eng_post["cpu_ticks"] - eng_pre["cpu_ticks"]) / CLK_TCK, 3)
            row["engine_resident_growth_mib"] = round(
                eng_post["resident_mib"] - eng_pre["resident_mib"], 1)
        sys_pre, sys_post = pre.get("system") or {}, post.get("system") or {}
        if sys_pre.get("mem_available_mib") is not None and sys_post.get("mem_available_mib") is not None:
            row["mem_available_drop_mib"] = sys_pre["mem_available_mib"] - sys_post["mem_available_mib"]
        gpu_post = {card: info for card, info in (sys_post.get("gpus") or {}).items() if info}
        if gpu_post:
            row["gpu_post"] = gpu_post
        rows.append(row)
    return rows


def run(args):
    result = dict(started=time.time(), pairs=[], success=False)
    status_path = runtime.EV / f"{args.family}-status.json"
    if status_path.exists():
        raise ValueError("Refusing to overwrite an earlier sweep")
    overrides = {}
    knob = None
    if args.draft_probe:
        schedule = [(label, 4200, 6400) for label in "abcd"]  # OFF/ON/ON/OFF
        if args.draft_l19:
            # l1w3: the L1 (n_drafts) + L9 (--dflash-remote-only) scratch binaries; the ON
            # arms draft remote-only so the VII's local drafter footprint is actually
            # recovered, and every arm carries the L7 per-chunk prefill spans.
            overrides = dict(engine=window_pair.WORK / "build-mixed-gpu/rpcw-hip-l19/strata",
                             helper=window_pair.WORK / "build-mixed-gpu/rpcw-cuda-l19/strata-expert-helper",
                             prefill_timing=True)
        if args.draft_l8:
            # L8: the bf16 tap wire binaries; both ON arms draft remote-only (the L1W10
            # shape) so ONLY the wire differs between b (fp32) and c (bf16).
            overrides = dict(engine=window_pair.WORK / "build-mixed-gpu/rpcw-hip-l8/strata",
                             helper=window_pair.WORK / "build-mixed-gpu/rpcw-cuda-l8/strata-expert-helper")
    elif args.knob_probe is not None:
        name, _, value = args.knob_probe.partition("=")
        knob = (name, value)
        schedule = [(label, 4200, 6400) for label in "abc"]  # OFF/ON/OFF
    elif args.l2_streams:
        # L2: every arm runs the SAME l2 helper binary (engine byte-reused); only arm
        # b's launcher injects STRATA_HELPER_STREAMS=1 helper-side. The OFF arms are
        # the behaviour-identity control: env gate unset = the pre-L2 single-mutex,
        # single-stream path, at the adopted canonical 4200/6400 identity.
        overrides = dict(helper=window_pair.WORK / "build-mixed-gpu/rpcw-cuda-l2/strata-expert-helper")
        schedule = [(label, 4200, 6400) for label in "abc"]  # OFF/ON/OFF
    elif args.split_overlap:
        # Lane 2 (the VII alternation): production binaries BOTH arms - the lever is
        # engine-env only (STRATA_SPLIT_OVERLAP=1 through the launcher's export seam),
        # so there is no rebuild and no helper change. Only arm b exports it; a/c are
        # byte-identical production controls at the canonical 4200/6400 identity, and
        # the ON arm's engine log carries '(overlapped: STRATA_SPLIT_OVERLAP=1)'.
        schedule = [(label, 4200, 6400) for label in "abc"]  # OFF/ON/OFF
    elif args.helper_off:
        # The user-asked helper measurement: a/c run helper_ab's validated OFF derivation
        # (no helper process at all - the shipped helper-less default path), b runs the
        # canonical supervised pair. Same production binaries; only the helper's presence
        # differs. infill = read_rate, TG = gen_rate; pure measurement, no adoption.
        schedule = [(label, 4200, 6400) for label in "abc"]  # OFF(helper-less)/ON/OFF
    elif args.l3_profile or args.degrade_probe:
        overrides = dict(engine=window_pair.WORK / "build-mixed-gpu/rpcw-hip-l3/strata",
                         helper=window_pair.WORK / "build-mixed-gpu/rpcw-cuda-l3/strata-expert-helper",
                         rpc_profile=True)
        schedule = [("a", 4200, 6400)]
    elif args.fill_probe:
        schedule = [("a", 4200, 6400), ("b", 4800, 6800), ("c", 4800, 7000), ("d", 4200, 6400)]
    else:
        schedule = [("a", 4000, 6400), ("b", 4200, 6400), ("c", 4400, 6400), ("d", 4000, 6400)]
    draft_remote_only = ("dflash-remote-only", "on") if (args.draft_l19 or args.draft_l8) else None
    # Prepare every file and syntax check BEFORE the first outage.
    private = [window_pair.prepare(f"{args.family}-{label}", slots=slots, fill_mib=fill,
                                  draft_helper=(label in "bc") if args.draft_probe else None,
                                  helper_streams=(label == "b") if args.l2_streams else False,
                                  draft_tap_bf16=(label == "c") if args.draft_l8 else False,
                                  split_overlap=(label == "b") if args.split_overlap else False,
                                  helper_off=(label != "b") if args.helper_off else False,
                                   knob=knob if (knob is not None and label == "b") else
                                   (draft_remote_only if (draft_remote_only is not None and label in "bc") else None),
                                   **overrides)
               for label, slots, fill in schedule]
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    active = None
    known = {}
    try:
        stop_production(args.expected_supervisor, result,
                        server_pid=args.expected_server)
        for config, (label, slots, fill) in zip(private, schedule):
            active = runtime.launch(True, runtime.EV / f"{config.family}-console.log", config.launcher)
            known = {active.pid: pair.process_token(active.pid)[1]}
            state = runtime.wait_for(lambda: runtime.attached(state_path=config.state,
                                      base_url=config.base_url, supervisor_pid=active.pid))
            state["engine_pid"] = runtime.engine_of(
                state, engine=json.loads(Path(config.config).read_text())["exe"])
            known = pair.descendants(active.pid, {active.pid: pair.process_token(active.pid)[1]})
            row = dict(label=label, requested_slots=slots, fill_mib=fill, state=state,
                        fill=None if (args.helper_off and label != "b") else
                        fill_report(Path(state["helper_log"]).read_text()), probes=[])
            if args.draft_probe:
                row["draft_helper"] = config.draft_helper
                engine_text = Path(json.loads(Path(config.config).read_text())["log"]).read_text()
                if config.draft_helper and "strata draft-helper: drafting on the helper:" not in engine_text:
                    raise RuntimeError("Remote drafter was not engaged at startup")
                if args.draft_l19:
                    # L9 absence proof: the ON arms never upload the local drafter; the OFF arms do.
                    if ("remote-only: local drafter not uploaded" in engine_text) != bool(config.draft_helper):
                        raise RuntimeError("The --dflash-remote-only upload-absence proof failed")
            if knob is not None:
                row["knob_on"] = config.knob is not None
                row["knob"] = config.knob
            elif config.knob is not None:
                row["knob_on"] = True
                row["knob"] = config.knob
            if args.l2_streams:
                row["l2_streams"] = bool(config.helper_streams)
            if args.draft_l8:
                row["bf16_taps"] = bool(config.draft_tap_bf16)
            if args.split_overlap:
                row["split_overlap"] = bool(config.split_overlap)
            if args.helper_off:
                row["helper_off"] = bool(config.helper_off)
            if row["fill"] is not None and row["fill"]["slots"] > slots:
                raise RuntimeError("Actual fill exceeded the requested slot cap")
            result["pairs"].append(row)
            probe_options = (dict(max_tokens=128, reasoning_budget_tokens=0, enable_thinking=False,
                                  require_remote_draft=config.draft_helper) if args.draft_probe else {})
            reps = args.reps if args.degrade_probe else 3
            for rep in range(1, reps + 1):
                if args.degrade_probe:
                    row.setdefault("samples", []).append(sample_pair(state))
                row["probes"].append(runtime.probe(label, rep, state, config_path=config.config,
                                                   base_url=config.base_url, family=args.family, **probe_options))
                if args.degrade_probe:
                    row["samples"].append(sample_pair(state))
                pair.write_json(status_path, result)
            if args.draft_probe:
                row["xl_probe"] = runtime.probe(label, 1, state, config_path=config.config,
                                                 base_url=config.base_url, family=args.family,
                                                 size="xl", **probe_options)
                pair.write_json(status_path, result)
            graceful_stop(active, state, known)
            active = None
            log = Path(json.loads(Path(config.config).read_text())["log"]).read_text()
            closes = [match for match in helper_ab.PROMPT_TIER_CLOSE_RE.finditer(log)
                      if match["path"] == config.socket2 and int(match["reqs"]) > 0]
            if args.helper_off and config.helper_off:
                if closes:
                    raise RuntimeError("Helper-less arm unexpectedly served prompt-tier traffic")
                row["prompt_channel"] = None
            else:
                if len(closes) != 1:
                    raise RuntimeError("Missing unique positive prompt-channel service proof")
                row["prompt_channel"] = dict(requests=int(closes[0]["reqs"]), rows=int(closes[0]["rows"]))
            if args.draft_probe:
                row["draft_service"] = draft_report(log, Path(state["helper_log"]).read_text(), config.draft_helper)
            if args.l3_profile or args.degrade_probe:
                helper_text = Path(state["helper_log"]).read_text()
                engine_lines = [line for line in log.splitlines() if "rpcprofile:" in line]
                helper_lines = [line for line in helper_text.splitlines() if "rpcprofile:" in line]
                if not engine_lines or not helper_lines:
                    raise RuntimeError("L3 instrumentation emitted no rpcprofile lines")
                row["rpcprofile"] = dict(engine=engine_lines, helper=helper_lines)
            row["generated_median"] = statistics.median(probe["gen_rate"] for probe in row["probes"])
            if args.degrade_probe:
                row["degrade"] = degrade_summary(row["probes"], row.pop("samples", []))
            print(json.dumps(row), flush=True)
        for rep in range(3):
            if len({row["probes"][rep]["request_sha256"] for row in result["pairs"]}) != 1:
                raise RuntimeError("Request bytes changed between slot settings")
        if args.draft_probe:
            if len({row["xl_probe"]["request_sha256"] for row in result["pairs"]}) != 1:
                raise RuntimeError("XL request bytes changed between draft settings")
            result["draft_comparison"] = draft_comparison(result["pairs"])
        if args.knob_probe is not None:
            result["knob_comparison"] = knob_comparison(result["pairs"])
        if args.l2_streams:
            result["l2_comparison"] = l2_comparison(result["pairs"])
        if args.draft_l8:
            result["l8_wire_comparison"] = l8_wire_comparison(result["pairs"])
        if args.split_overlap:
            result["overlap_comparison"] = overlap_comparison(result["pairs"])
        if args.helper_off:
            result["helper_off_comparison"] = helper_off_comparison(result["pairs"])
        result["success"] = True
        result["limits"] = ("Helper-less vs canonical measurement A/B (user ask): OFF arms are "
                        "helper_ab's validated OFF derivation with the helper never started "
                        "(supervisor helper_pid 0), ON is the canonical supervised pair; infill "
                        "= read_rate, TG = gen_rate; SHA parity required; pure measurement, "
                        "no adoption."
                        if args.helper_off else
                        "Engine-env OFF/ON/OFF STRATA_SPLIT_OVERLAP A/B on the production "
                        "binaries, no rebuild and no helper change; decode-median verdict "
                        "vs the >=+5% lever gate; the ON arm's engine log carries the "
                        "'(overlapped: ...)' line; no adoption."
                        if args.split_overlap else
                        "L8 wire A/B on the l8 binaries: OFF(local)/ON(remote fp32)/ON(remote "
                        "bf16)/OFF(local), ON arms remote-only so only the wire differs; "
                        "decode-median c-vs-b vs the >=+5% gate with the draft-acceptance "
                        "guard; no adoption." if args.draft_probe and args.draft_l8 else
                        "Bracketed remote-draft execution A/B; no VRAM displacement or automatic adoption."
                            if args.draft_probe else
                            "L6 config-knob OFF/ON/OFF engine-argv A/B at the canonical 4200/6400 "
                            "identity; decode-median verdict plus the L7 slow-prefill fraction; "
                            "no adoption." if args.knob_probe is not None else
                            "L2 helper-env OFF/ON/OFF A/B on the rpcw-cuda-l2 helper binary, "
                            "engine byte-reused; OFF arms are the env-unset behaviour-identity "
                            "control; decode-median verdict vs the >=+5% lever gate; no adoption."
                            if args.l2_streams else
                            "L7 diagnostic window: repeated long probes with per-probe local resource "
                            "sampling on the L3 candidate binaries; diagnosis only, no adoption."
                            if args.degrade_probe else
                            "L3 instrumented window on candidate binaries; component-timing "
                            "evidence only, no adoption; gates unchanged." if args.l3_profile else
                            "Exploratory slot sweep; no XL probes or new-default adoption. "
                            "Bracketed baseline retained, all timing samples included.")
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        try:
            if active is not None:
                pair.stop_group(active, 45, known)
        finally:
            try:
                if result.get("outage_started"):
                    old = result["old_production"]["processes"]
                    if any(pair.alive(pid, start) for pid, start in {**old, **known}.items()):
                        raise RuntimeError("Owned processes survived cleanup; refusing concurrent production start")
                    restore_production(result, args.family)
            except BaseException as error:
                result["restore_error"] = f"{type(error).__name__}: {error}"
                raise
            finally:
                result["finished"] = time.time()
                pair.write_json(status_path, result)
    print(json.dumps(result, indent=2), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", default="slots3")
    parser.add_argument("--expected-supervisor", type=int)
    parser.add_argument("--expected-server", type=int)
    parser.add_argument("--fill-probe", action="store_true",
                        help="probe the fill budget instead of the slot cap "
                             "(arms 4200/4800 slots at fill 6400/6800/7000 MiB)")
    parser.add_argument("--l3-profile", action="store_true",
                        help="single arm at the adopted canonical config on the l3 candidate "
                             "binaries with STRATA_RPC_PROFILE=1; collects per-op rpcprofile "
                              "lines from the engine and helper logs")
    parser.add_argument("--draft-probe", action="store_true",
                        help="OFF/ON/ON/OFF remote drafting under two sockets; "
                             "3 long + 1 XL probes/arm, 128 max tokens, thinking off, budget 0; no adoption")
    parser.add_argument("--draft-l19", action="store_true",
                        help="with --draft-probe: run the l19 scratch binaries (L1 n_drafts fix "
                             "+ L9); the ON arms draft remote-only so the VII's local drafter "
                             "footprint is recovered, and every arm logs the L7 per-chunk "
                             "prefill spans")
    parser.add_argument("--draft-l8", action="store_true",
                        help="with --draft-probe: run the l8 scratch binaries (bf16 draft tap "
                             "wire); arms OFF(local)/ON(remote fp32)/ON(remote bf16)/OFF(local), "
                             "ON arms remote-only so only the wire differs between b and c; the "
                             "verdict is c-vs-b decode vs the >=+5% gate with the acceptance guard")
    parser.add_argument("--degrade-probe", action="store_true",
                        help="L7 diagnosis: one arm at the canonical config on the l3 candidate "
                             "binaries with STRATA_RPC_PROFILE=1; repeated varied-nonce long probes "
                             "with per-probe engine/helper /proc, memory/PSI and GPU sampling")
    parser.add_argument("--knob-probe", metavar="NAME=VALUE",
                        help="L6: engine-argv config-knob A/B; arms OFF/ON/OFF at the canonical "
                             "4200/6400 identity, 3 matched long probes/arm; NAME from the approved "
                             "sweep set, VALUE a replacement value or on/off for flag knobs")
    parser.add_argument("--l2-streams", action="store_true",
                        help="L2: helper-env A/B; arms OFF/ON/OFF at the canonical 4200/6400 "
                             "identity, every arm on the rpcw-cuda-l2 helper binary (engine "
                             "byte-reused), only arm b injects STRATA_HELPER_STREAMS=1 "
                             "helper-side; 3 matched long probes/arm vs the >=+5% decode gate")
    parser.add_argument("--split-overlap", action="store_true",
                        help="lane 2: engine-env A/B of STRATA_SPLIT_OVERLAP=1 (the layer "
                             "split's overlapped hand-off); arms OFF/ON/OFF at the canonical "
                             "4200/6400 identity on the PRODUCTION binaries - no rebuild, no "
                             "helper change; 3 matched long probes/arm vs the >=+5% decode gate")
    parser.add_argument("--helper-off", action="store_true",
                        help="user-asked measurement A/B: arms OFF(helper-less)/ON(canonical)/"
                             "OFF - the OFF arms reuse helper_ab's validated OFF derivation "
                             "(no helper process at all, supervisor helper_pid 0) on the "
                             "production binaries; measures infill (read_rate) and TG "
                             "(gen_rate); pure measurement, no adoption")
    parser.add_argument("--reps", type=int, default=6,
                        help="probe count for --degrade-probe (3-12, default 6)")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if (args.expected_supervisor is None) == (args.expected_server is None):
        parser.error("give exactly one of --expected-supervisor (supervised pair) "
                     "or --expected-server (direct launcher pair)")
    if sum((args.l3_profile, args.fill_probe, args.draft_probe, args.degrade_probe,
            args.knob_probe is not None, args.l2_streams, args.split_overlap,
            args.helper_off)) > 1:
        parser.error("--l3-profile, --fill-probe, --draft-probe, --degrade-probe, "
                     "--knob-probe, --l2-streams, --split-overlap and --helper-off are exclusive")
    if args.knob_probe is not None:
        name, sep, value = args.knob_probe.partition("=")
        if not (sep and name and value):
            parser.error("--knob-probe needs NAME=VALUE (on/off for flag knobs)")
    if args.draft_l19 and not args.draft_probe:
        parser.error("--draft-l19 requires --draft-probe")
    if args.draft_l8 and not args.draft_probe:
        parser.error("--draft-l8 requires --draft-probe")
    if args.draft_l8 and args.draft_l19:
        parser.error("--draft-l8 and --draft-l19 are exclusive")
    if args.degrade_probe and not 3 <= args.reps <= 12:
        parser.error("--reps must be 3..12 with --degrade-probe")
    if not args.execute:
        if args.draft_probe:
            print("Plan: exact production identity stop; private18080 OFF/ON/ON/OFF remote "
                  "drafting at 4200 slots/fill6400, both sockets; 3 long and 1 XL probes/arm "
                  "at max_tokens128/thinking-off/budget0; reject remote-lane failures, verify draft counters, "
                  "request SHA parity and control drift; always restore supervised canonical. No adoption."
                  + (" L1+L9 l19 binaries; ON arms --dflash-remote-only with the upload-absence "
                     "proof; every arm logs the L7 per-chunk prefill spans." if args.draft_l19 else
                     " L8 l8 binaries; ON arms --dflash-remote-only, arm c rides the bf16 tap "
                     "wire (STRATA_DRAFT_TAP_BF16=1 engine-side); the verdict is c-vs-b decode "
                     "vs the >=+5% gate with the draft-acceptance guard." if args.draft_l8 else ""))
            return 0
        if args.knob_probe is not None:
            print("Plan: exact production identity stop; private18080 OFF/ON/OFF engine-argv knob "
                  f"--{args.knob_probe} at 4200 slots/fill6400, both sockets; 3 matched long "
                  "probes/arm; request SHA parity, zero-reuse, socket2 and control-drift gates; "
                  "decode median plus L7 slow-prefill fraction; always restore supervised "
                  "canonical. No adoption.")
            return 0
        if args.l2_streams:
            print("Plan: exact production identity stop; private18080 OFF/ON/OFF helper "
                  "stream split at 4200 slots/fill6400, both sockets; every arm runs the "
                  "rpcw-cuda-l2 helper (engine byte-reused), only arm b injects "
                  "STRATA_HELPER_STREAMS=1 helper-side so the OFF arms prove env-unset "
                  "behaviour identity; 3 matched long probes/arm; request SHA parity, "
                  "socket2 and control-drift gates; decode median plus L7 slow-prefill "
                  "fraction vs the >=+5% lever gate; always restore supervised canonical. "
                  "No adoption.")
            return 0
        if args.split_overlap:
            print("Plan: exact production identity stop; private18080 OFF/ON/OFF split "
                  "overlap at 4200 slots/fill6400, both sockets; every arm runs the "
                  "PRODUCTION binaries untouched, only arm b's launcher exports "
                  "STRATA_SPLIT_OVERLAP=1 (engine-side; the arm's engine log must carry "
                  "'(overlapped: ...)' as the engagement proof); 3 matched long probes/arm; "
                  "request SHA parity, socket2 and control-drift gates; decode median plus "
                  "L7 slow-prefill fraction vs the >=+5% lever gate (upstream measured "
                  "+24-32% on 2x gfx1100, unmeasured on gfx906); always restore supervised "
                  "canonical. No adoption.")
            return 0
        if args.helper_off:
            print("Plan: exact production identity stop; private18080 helper measurement at "
                  "the canonical 4200/6400 identity; arms a/c run helper_ab's validated OFF "
                  "derivation (NO helper process: launcher helper block stripped, record 0, "
                  "supervisor attaches on the server's ready line, zero helper traffic "
                  "enforced), arm b runs the canonical supervised pair untouched; 3 matched "
                  "long probes/arm; request-SHA parity across arms (greedy must not change); "
                  "infill (read_rate) and TG (gen_rate) medians; always restore supervised "
                  "canonical. Pure measurement, no adoption.")
            return 0
        if args.degrade_probe:
            print("Plan: exact production identity stop; private18080 single arm 4200 slots/fill6400 "
                  "on the l3 candidate binaries with STRATA_RPC_PROFILE=1; N varied-nonce long probes "
                  "with per-probe engine/helper /proc, memory/PSI and GPU clock/temp sampling; all "
                  "existing serve/close gates; always restore supervised canonical. Diagnosis only, "
                  "no adoption.")
            return 0
        if args.l3_profile:
            arms = ("single arm 4200 slots / fill 6400 on the rpcw-hip-l3 + rpcw-cuda-l3 "
                    "candidate binaries with STRATA_RPC_PROFILE=1 (per-op rpcprofile timing)")
        else:
            arms = ("fill6400/6800/7000/6400 MiB at slots 4200/4800/4800/4200" if args.fill_probe
                    else "pairs4000/4200/4400/4000, fill6400")
        print("Plan: verify/stop production pair (supervised supervisor or verified direct "
              f"server form); private18080 {arms}, 3 matched long "
              "probes each, graceful per-arm teardown for deterministic close capture; restore "
              "supervised canonical pair and generation. No adoption.")
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Opt-in, bracketed four-lever calibration on pinned production binaries.

Reuses identity-checked pair lifecycle helpers. Never adopts settings. A completed
window is not a numerical acceptance: timings and exact output TEXT are separate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import statistics
import threading
import time
import urllib.error
import urllib.request

import helper_ab
import pair_supervisor as pair
import recovery_window as runtime
import slots_window as lifecycle
import window_pair

MATRIX = {
    "trim": [{"stage_trim": True}],
    "dflash": [{"dflash": [4, 3, 0.7]}, {"dflash": [7, 6, 0.3]}],
    "pipe": [{"prefill_pipe": 1}, {"prefill_pipe": 6144}],
    "split": [{"layer_split": 30}, {"layer_split": 34}],
}
PARAGRAPH = ("Mixed precision inference on consumer GPUs requires careful memory planning "
             "because expert weights exceed the card capacity, so residency must be ranked "
             "by routing frequency and streamed only on demand. ")
MIN_OUTPUT = 384
PROTECTED = runtime.WORK / "build-mixed-gpu/nmse-tooling1/parent-build1-protected-before.sha256"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def request_body(family, rep):
    """Fresh multi-chunk PP plus sustained TG; bytes are arm-independent."""
    # 280 copies left only 993 tokens after the 8192 chunk: below pipe=1's
    # 1024-token stream-all guard. Keep a longer tail so that lever can engage.
    text = (f"Performance window {family}, repetition {rep}; independent cold request.\n"
            + PARAGRAPH * 320
            + "\nWrite 120 numbered engineering checklist entries about implementing and "
              "validating the system described above. Each entry must be a full sentence "
              "with at least twelve words. Start with 1. Do not summarize or stop early.")
    return json.dumps(dict(model="swift-1.5-iq3_xxs", messages=[dict(role="user", content=text)],
                           max_tokens=512, temperature=0, seed=1729,
                           reasoning_budget_tokens=0,
                           chat_template_kwargs=dict(enable_thinking=False))).encode()


def validate_reply(reply, metric):
    choice = reply["choices"][0]
    message = choice.get("message", {})
    count = reply["usage"]["completion_tokens"]
    if (type(count) is not int or not MIN_OUTPUT <= count <= metric["generated"] or
            choice["finish_reason"] not in ("stop", "length") or not message.get("content") or
            metric["cancelled"] or metric["reused"] != 0 or metric["read"] != metric["prompt"] or
            metric["prompt"] < 9000 or not (metric.get("helper_reqs") or 0) > 0 or
            (metric.get("helper_timeouts") or 0) or (metric.get("helper_fallbacks") or 0) or
            metric["offered"] <= 0 or min(metric["gen_rate"], metric["read_rate"]) <= 0):
        raise RuntimeError(f"Invalid sustained probe: ct={count}, {metric}")
    # Compare actual returned message bytes, never the request hash or envelope's id/date.
    canonical = json.dumps(message, sort_keys=True, ensure_ascii=False).encode()
    return dict(completion_tokens=count, finish=choice["finish_reason"],
                message_sha256=digest(canonical),
                content_sha256=digest(message["content"].encode()),
                content_chars=len(message["content"]))


def probe(config, state, family, rep):
    prefix = runtime.EV / f"{family}-{config.label}-r{rep}"
    body = request_body(family, rep)
    Path(f"{prefix}-request.json").write_bytes(body)
    log_path = Path(json.loads(Path(config.config).read_text())["log"])
    mark = log_path.stat().st_size
    request = urllib.request.Request(config.base_url + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            raw, code = response.read(), response.status
        Path(f"{prefix}-reply.json").write_bytes(raw)
    except urllib.error.HTTPError as error:
        Path(f"{prefix}-reply.json").write_bytes(error.read())
        raise
    finally:
        with log_path.open("rb") as source:
            source.seek(mark)
            segment = source.read().decode(errors="replace")
        Path(f"{prefix}-engine.log").write_text(segment)
    metrics = [m for line in segment.splitlines() if (m := helper_ab.parse_serve_line(line))]
    if code != 200 or len(metrics) != 1:
        raise RuntimeError(f"Expected one successful generation, http={code}, summaries={len(metrics)}")
    metric = metrics[0]
    output = validate_reply(json.loads(raw), metric)
    if not pair.alive(state["helper_pid"], state["helper_start"]):
        raise RuntimeError("Helper identity changed during probe")
    if re.search(r"drafting is off|candidate tokens outside|sampled request uses target-only", segment):
        raise RuntimeError("Drafter fell back during probe; raw artifacts retained")
    row = dict(rep=rep, elapsed_s=time.monotonic() - start, request_sha256=digest(body),
               **output, **metric)
    row["plans"] = [line for line in segment.splitlines() if
                    line.startswith("strata prefill:") and "tokens in" in line]
    print(json.dumps(dict(event="probe", arm=config.label, **row)), flush=True)
    return row


def startup_report(config, state):
    cfg = json.loads(Path(config.config).read_text())
    log = Path(cfg["log"]).read_text()
    args = cfg["args"]
    value = lambda flag: args[args.index(flag) + 1]
    split = int(cfg["layer_split"])
    later = re.search(r"CUDA1 runs layers (\d+)-(\d+), expert cache (\d+) slots", log)
    first = re.search(r"strata generate: expert cache (\d+) slots", log)
    owned = re.search(r"(\d+) of (\d+) ranked pairs eligible in layers 0\.\.47 "
                      r"\((\d+) held by a cache, 0 outside the window, 0 outside the geometry, "
                      r"0 repeated, 0 Hadamard-folded\)", log)
    k = re.search(r"trained block (\d+), pass K=(\d+)", log)
    fill = lifecycle.fill_report(Path(state["helper_log"]).read_text())
    mirrored = re.search(r"expert-helper: (\d+) rows on .* \(mirrored (\d+)/4200 slots", log)
    if (not later or tuple(map(int, later.group(1, 2))) != (split, 47) or
            f"CUDA0 runs layers 0-{split - 1}" not in log or not first or not owned or
            int(owned[1]) + int(owned[3]) != int(owned[2]) or int(owned[2]) != 24576 or
            int(first[1]) + int(later[3]) != int(owned[3]) or
            not k or int(k[2]) != min(int(value("--spec")) - 1, int(value("--dflash-block"))) or
            not mirrored or int(mirrored[1]) != int(mirrored[2]) or
            int(mirrored[1]) != fill["slots"] or not 0 < fill["slots"] <= 4200):
        raise RuntimeError("Startup geometry/drafter/disjoint-helper proof failed")
    trimmed = re.findall(r"CUDA(\d+) loads the dense weights of layers (\d+)-(\d+) only", log)
    expected_trim = [("0", "0", str(split - 1)), ("1", str(split), "47")]
    if sorted(trimmed) != (expected_trim if config.tuning == {"stage_trim": True} else []):
        raise RuntimeError("Stage-trim engagement/absence proof failed")
    taps = [line for line in log.splitlines() if "cross-GPU tap collection" in line]
    if len(taps) != 1:
        raise RuntimeError("Missing unique cross-GPU DFlash tap binding")
    return dict(split=split, first_cache_slots=int(first[1]), later_cache_slots=int(later[3]),
                helper_fill=fill, eligible_pairs=int(owned[1]), held_pairs=int(owned[3]),
                effective_k=int(k[2]), floor=float(value("--spec-min-p")), trimmed=trimmed,
                tap_lines=taps)


def compare(rows):
    """Preserve noisy/mismatching observations, but never label them accepted gains."""
    controls = rows[0]["probes"] + rows[-1]["probes"]
    med = lambda probes, key: statistics.median(p[key] for p in probes)
    drift = {key: med(rows[-1]["probes"], key) / med(rows[0]["probes"], key)
             for key in ("gen_rate", "read_rate")}
    a_hash = [p["message_sha256"] for p in rows[0]["probes"]]
    stable = a_hash == [p["message_sha256"] for p in rows[-1]["probes"]]
    results = []
    for row in rows[1:-1]:
        probes = row["probes"]
        parity = [p["message_sha256"] for p in probes] == a_hash
        ratios = {key: med(probes, key) / med(controls, key) for key in drift}
        timing = {key: ("INCONCLUSIVE_CONTROL_DRIFT" if not 0.9 <= drift[key] <= 1.1 else
                        "SPEED_CANDIDATE" if ratios[key] >= 1.05 else "NO_GAIN") for key in drift}
        verdict = ("INCONCLUSIVE_BASELINE_OUTPUT" if not stable else
                   "OUTPUT_MISMATCH" if not parity else
                   "CANDIDATE_NEEDS_CONFIRMATION" if "SPEED_CANDIDATE" in timing.values() else
                   "INCONCLUSIVE_CONTROL_DRIFT" if "INCONCLUSIVE_CONTROL_DRIFT" in timing.values() else
                   "NO_GAIN")
        results.append(dict(label=row["label"], tuning=row["tuning"], ratios=ratios,
                            medians={key: med(probes, key) for key in drift},
                            timing=timing, output_text_equal=parity, verdict=verdict))
    for rep in range(len(rows[0]["probes"])):
        if len({row["probes"][rep]["request_sha256"] for row in rows}) != 1:
            raise RuntimeError("Matched request bytes changed across arms")
    return dict(control_medians={key: med(controls, key) for key in drift}, drift=drift,
                baseline_output_text_stable=stable, candidates=results,
                limits="Exact returned message text only, not token-ID or numerical equivalence. "
                       "Exploratory calibration; pooled numerical gate remains FAIL; no adoption.")


class Heartbeat:
    def __init__(self, family):
        self.family, self.phase = family, "preflight"
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.done.wait(5):
            print(json.dumps(dict(event="heartbeat", family=self.family, phase=self.phase,
                                  time=time.time())), flush=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.done.set()
        self.thread.join()


def pins():
    paths = [runtime.CONFIG, runtime.LAUNCHER, Path(__file__),
             Path(window_pair.__file__), Path(runtime.__file__), Path(lifecycle.__file__),
             Path(pair.__file__), Path(helper_ab.__file__)]
    for line in PROTECTED.read_text().splitlines():
        expected, relative = line.split(maxsplit=1)
        path = runtime.WORK / relative
        if digest(path.read_bytes()) != expected:
            raise RuntimeError(f"Protected binary changed: {relative}")
        paths.append(path)
    return {str(path): digest(path.read_bytes()) for path in paths}


def run(args):
    if not re.fullmatch(r"[a-z][a-z0-9]{0,10}", args.family):
        raise ValueError("Use a fresh short alphanumeric family")
    status = runtime.EV / f"{args.family}-perf-status.json"
    if status.exists():
        raise ValueError("Refusing to overwrite an earlier window")
    configs = json.loads(runtime.CONFIG.read_text())
    for key in ("STRATA_STAGE_TRIM", "STRATA_PREFILL_PIPE", "STRATA_PREFILL_EQUAL",
                "STRATA_SPLIT_OVERLAP", "STRATA_DRAFT_HELPER", "STRATA_IQ_MT_MIN"):
        if os.environ.get(key, "0") not in ("", "0") or configs.get("env", {}).get(key, "0") not in ("", "0"):
            raise ValueError(f"Unexpected baseline lever enabled: {key}")
    fingerprint = pins()
    schedule = [None, *MATRIX[args.lever], None]
    if args.candidate is not None:
        schedule = [None, MATRIX[args.lever][args.candidate - 1], None]
    if not args.execute:
        print(json.dumps(dict(family=args.family, schedule=schedule, reps=3,
                              max_tokens=512, minimum_output=MIN_OUTPUT,
                              iq_mt_min=args.iq_mt_min, adapt_off=args.adapt_off,
                              restore=str(runtime.LAUNCHER) + " --recover", pins=fingerprint), indent=2))
        return 0
    private = []
    for i, tuning in enumerate(schedule):
        config = window_pair.prepare(f"{args.family}-{chr(97 + i)}", slots=4200, tuning=tuning,
                                     iq_mt_min=args.iq_mt_min, adapt_off=args.adapt_off)
        config.label = chr(97 + i)
        private.append(config)
    result = dict(started=time.time(), lever=args.lever, iq_mt_min=args.iq_mt_min,
                  adapt_off=args.adapt_off, pairs=[], success=False, pins=fingerprint)
    pair.write_json(status, result)  # durable intent before any production signal
    active, known = None, {}
    old_handlers = {sig: signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
                    for sig in (signal.SIGTERM, signal.SIGINT)}
    with Heartbeat(args.family) as beat:
        try:
            beat.phase = "stop canonical"
            lifecycle.stop_production(args.expected_supervisor, result)
            for config in private:
                beat.phase = f"arm {config.label} startup"
                active = runtime.launch(True, runtime.EV / f"{config.family}-console.log", config.launcher)
                known = {active.pid: pair.process_token(active.pid)[1]}
                state = runtime.wait_for(lambda: runtime.attached(state_path=config.state,
                                         base_url=config.base_url, supervisor_pid=active.pid))
                state["engine_pid"] = runtime.engine_of(state)
                state["engine_argv"] = runtime.command(state["engine_pid"])
                known = pair.descendants(active.pid, known)
                row = dict(label=config.label, tuning=config.tuning, state=state,
                           startup=startup_report(config, state), probes=[])
                result["pairs"].append(row)
                for rep in range(1, 4):
                    beat.phase = f"arm {config.label} probe {rep}/3"
                    row["probes"].append(probe(config, state, args.family, rep))
                    pair.write_json(status, result)
                beat.phase = f"arm {config.label} graceful stop"
                lifecycle.graceful_stop(active, state, known)
                active = None
                if any(pair.alive(pid, token) for pid, token in known.items()):
                    raise RuntimeError("Private processes survived arm teardown")
                log = Path(json.loads(Path(config.config).read_text())["log"]).read_text()
                closes = [m for m in helper_ab.PROMPT_TIER_CLOSE_RE.finditer(log)
                          if m["path"] == config.socket2 and int(m["reqs"]) > 0]
                if len(closes) != 1:
                    raise RuntimeError("Missing unique positive prompt-channel close proof")
                row["prompt_channel"] = dict(requests=int(closes[0]["reqs"]), rows=int(closes[0]["rows"]))
                if config.tuning and "prefill_pipe" in config.tuning and not any(p["plans"] for p in row["probes"]):
                    raise RuntimeError("Prefill-pipe changed no chunk plan; no engagement proof")
                print(json.dumps(dict(event="arm_done", arm=config.label, startup=row["startup"])), flush=True)
                pair.write_json(status, result)
            result["comparison"] = compare(result["pairs"])
            result["success"] = True
        except BaseException as error:
            result["error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            try:
                beat.phase = "cleanup"
                if active is not None:
                    pair.stop_group(active, 45, known)
            finally:
                try:
                    if result.get("outage_started"):
                        owned = {**result["old_production"]["processes"], **known}
                        if any(pair.alive(pid, token) for pid, token in owned.items()):
                            raise RuntimeError("Owned processes survived; refusing concurrent production")
                        beat.phase = "restore canonical --recover"
                        lifecycle.restore_production(result, args.family)
                    if pins() != fingerprint:
                        raise RuntimeError("Pinned files changed during measurement")
                except BaseException as error:
                    result["restore_error"] = f"{type(error).__name__}: {error}"
                    raise
                finally:
                    result["finished"] = time.time()
                    pair.write_json(status, result)
                    for sig, handler in old_handlers.items():
                        signal.signal(sig, handler)
    print(json.dumps(result, indent=2), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", required=True)
    parser.add_argument("--lever", choices=MATRIX, required=True)
    parser.add_argument("--candidate", type=int, help="one-based candidate for a fresh confirmation")
    parser.add_argument("--expected-supervisor", type=int, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--iq-mt-min", action="store_true",
                        help="baseline env STRATA_IQ_MT_MIN=1 on EVERY arm (issue #152: "
                             "drafting-independent CPU IQ expert rounding; controls and "
                             "candidates alike, so the window still measures one lever)")
    parser.add_argument("--adapt-off", action="store_true",
                        help="baseline argv --adapt-swaps 0 on EVERY arm (freeze both adaptive "
                             "tiers at the deterministic initial fill, so placement stops "
                             "following the lever's timing; controls and candidates alike)")
    args = parser.parse_args()
    if args.candidate is not None and not 1 <= args.candidate <= len(MATRIX[args.lever]):
        parser.error("candidate outside the bounded matrix")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

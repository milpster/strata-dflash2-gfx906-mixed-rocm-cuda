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
REPLAY_RE = r"[a-z][a-z0-9]{0,10}"
# Exact retained-fixture contract (see validate_fixture): the known request key set and model.
FIXTURE_KEYS = frozenset({"model", "messages", "max_tokens", "temperature", "seed",
                          "reasoning_budget_tokens", "chat_template_kwargs"})
FIXTURE_MODEL = "swift-1.5-iq3_xxs"
# Narrow audit allowlist for the live engine environment: only these keys are ever read
# and echoed, so the engagement proof never dumps secrets (API keys, tokens, paths).
ENGINE_ENV_KEYS = ("STRATA_HIP_PROMPT_F16", "STRATA_IQ_MT_MIN")
# The engine line the FP16 prompt override prints (ppc2-*-engine.log:34/53); under
# --prompt-f32 it must be ABSENT on every HIP device (engine log:34/53, gemm.hpp:121-123).
FP16_PROMPT_LINE = re.compile(r"HIP device \d+ .*STRATA_HIP_PROMPT_F16=1.*16-bit GEMMs run FP16")


def engine_env(pid, keys=ENGINE_ENV_KEYS):
    """Read ONLY the narrow audited keys from the live engine's environment snapshot.

    /proc/<pid>/environ is the exec-time kernel snapshot the server applied from the
    private config (serve/server.py). The fixed allowlist means no secret is ever
    returned, and an unreadable environ is a hard failure - the engagement proof is a
    direct read, never inferred from the line's absence."""
    try:
        raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
    except OSError as error:
        raise RuntimeError(f"Engine environ unreadable for the engagement proof: {error}")
    live = {}
    for entry in raw.split(b"\0"):
        key, sep, value = entry.partition(b"=")
        if sep and key.decode(errors="replace") in keys:
            live[key.decode(errors="replace")] = value.decode(errors="replace")
    return live


def validate_fixture(raw):
    """Strict retained-fixture validation, narrow on purpose.

    Accepts ONLY the known request key set, the pinned model, a single user message with
    non-empty string content, and exact numeric fixture values. Sampling/tool/stop controls,
    extra keys, malformed messages/model, and bool-as-number (True/False as 0/1) are refused.
    The raw bytes are never reserialised - the caller digests them verbatim. This is reused
    fixture validation, not a gate change; it does not require the prompt family to equal any
    source name (nested replay legitimately keeps the source's prompt bytes).
    """
    try:
        req = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError(f"Replay source request is not valid JSON: {error}")
    if not isinstance(req, dict) or set(req) != FIXTURE_KEYS:
        raise ValueError(f"Replay source request key set must be exactly {sorted(FIXTURE_KEYS)}")
    if req["model"] != FIXTURE_MODEL:
        raise ValueError(f"Replay source request model must be {FIXTURE_MODEL}")
    messages = req["messages"]
    if (not isinstance(messages, list) or len(messages) != 1
            or not isinstance(messages[0], dict) or messages[0].get("role") != "user"
            or not isinstance(messages[0].get("content"), str) or not messages[0]["content"]
            or set(messages[0]) != {"role", "content"}):
        raise ValueError("Replay source request must be one user message with non-empty string content")
    numeric = lambda key: type(req[key]) in (int, float) and type(req[key]) is not bool
    for key, expected in (("max_tokens", 512), ("temperature", 0), ("seed", 1729),
                          ("reasoning_budget_tokens", 0)):
        if not numeric(key) or req[key] != expected:
            raise ValueError(f"Replay source request {key} must be the number {expected} (not a bool)")
    kwargs = req["chat_template_kwargs"]
    if (not isinstance(kwargs, dict) or set(kwargs) != {"enable_thinking"}
            or kwargs.get("enable_thinking") is not False):
        raise ValueError("Replay source chat_template_kwargs must be exactly {enable_thinking: False}")
    return req


def load_replay(source, evidence=runtime.EV):
    """Freeze the exact retained request bytes of a completed window, per repetition.

    Read-only and pure: validates the source shape (3 or 4 arms, safe sequential unique
    labels, exactly reps 1,2,3 per arm), pins the raw STATUS bytes, then re-validates each
    arm's per-rep request file (raw SHA against the recorded request_sha256, strict fixture
    semantics, byte-identical across all arms per rep) and that the three reps are distinct.
    Returns the raw bytes to reuse VERBATIM (never regenerated or reserialised) plus
    provenance including the status SHA. Fails closed on any missing, corrupt, malformed,
    hash-mismatched, cross-arm-differing, non-fixture, or non-distinct source, before any
    production signal.
    """
    if not re.fullmatch(REPLAY_RE, source):
        raise ValueError("Replay source needs a short family name")
    status = Path(evidence) / f"{source}-perf-status.json"
    if not status.is_file():
        raise ValueError(f"Replay source status is missing: {status.name}")
    status_bytes = status.read_bytes()
    status_sha = digest(status_bytes)
    try:
        data = json.loads(status_bytes)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError(f"Replay source status is malformed: {error}")
    if not data.get("success"):
        raise ValueError("Replay source window did not complete")
    pairs = data.get("pairs")
    if not isinstance(pairs, list) or len(pairs) not in (3, 4):
        raise ValueError("Replay source must have 3 (bracket) or 4 (two-candidate) arms")
    labels = []
    for row in pairs:
        if not isinstance(row, dict) or "label" not in row or "probes" not in row:
            raise ValueError("Replay source arm is not a label/probes record")
        label = row["label"]
        if not isinstance(label, str) or not re.fullmatch(r"[a-z]", label):
            raise ValueError(f"Replay source arm label is unsafe: {label!r}")
        if not isinstance(row["probes"], list):
            raise ValueError(f"Replay source {label} probes is not a list")
        reps = [p.get("rep") for p in row["probes"] if isinstance(p, dict)]
        if sorted(reps) != [1, 2, 3] or len(set(reps)) != 3:
            raise ValueError(f"Replay source {label} reps must be exactly 1,2,3 unique")
        labels.append(label)
    if len(set(labels)) != len(labels) or labels != [chr(97 + i) for i in range(len(labels))]:
        raise ValueError("Replay source labels must be unique and sequential a,b,c[,d]")
    frozen, source_files = {}, {}
    for rep in (1, 2, 3):
        body, digests = None, set()
        for row in pairs:
            probe = {p["rep"]: p for p in row["probes"]}[rep]
            req = Path(evidence) / f"{source}-{row['label']}-r{rep}-request.json"
            if not req.is_file():
                raise ValueError(f"Replay source request is missing: {req.name}")
            raw = req.read_bytes()
            sha = digest(raw)
            if probe.get("request_sha256") != sha:
                raise ValueError(f"Replay source request hash mismatch: {req.name}")
            validate_fixture(raw)
            if body is None:
                body = raw
            digests.add(sha)
            source_files[str(req)] = sha
        if len(digests) != 1:
            raise ValueError(f"Replay source request bytes differ across arms for rep {rep}")
        frozen[rep] = body
    if len({digest(frozen[rep]) for rep in (1, 2, 3)}) != 3:
        raise ValueError("Replay source repetitions are not distinct")
    provenance = dict(source=source, status=str(status), status_sha256=status_sha, arms=labels,
                      request_sha256={str(rep): digest(frozen[rep]) for rep in (1, 2, 3)},
                      source_files=source_files)
    return frozen, provenance


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


def probe(config, state, family, rep, body=None):
    prefix = runtime.EV / f"{family}-{config.label}-r{rep}"
    # Frozen replay reuses the retained source bytes VERBATIM (never regenerated or
    # reserialised); the no-option path stays request_body(family, rep).
    body = request_body(family, rep) if body is None else body
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
    prompt_f32_engagement = None
    if getattr(config, "prompt_f32", False):
        # Direct engagement proof (NOT inferred from the line's absence): the private config
        # sets the key to 0, the live engine's environment snapshot shows it at 0, and no
        # HIP device reports the FP16 prompt GEMMs. Startup is refused if the FP16 prompt
        # line appears under the flag.
        if cfg.get("env", {}).get("STRATA_HIP_PROMPT_F16") != "0":
            raise RuntimeError("Prompt-F32 private config env is not STRATA_HIP_PROMPT_F16=0")
        live = engine_env(state["engine_pid"])
        if live.get("STRATA_HIP_PROMPT_F16") != "0":
            raise RuntimeError(f"Engine environ shows STRATA_HIP_PROMPT_F16={live.get('STRATA_HIP_PROMPT_F16')}, not 0")
        if FP16_PROMPT_LINE.search(log):
            raise RuntimeError("A HIP device reports prompt FP16 GEMMs under --prompt-f32")
        prompt_f32_engagement = dict(private_env="0", engine_environ=live,
                                     engine_pid=state["engine_pid"], fp16_prompt_line_absent=True)
    return dict(split=split, first_cache_slots=int(first[1]), later_cache_slots=int(later[3]),
                helper_fill=fill, eligible_pairs=int(owned[1]), held_pairs=int(owned[3]),
                effective_k=int(k[2]), floor=float(value("--spec-min-p")), trimmed=trimmed,
                tap_lines=taps, prompt_f32_engagement=prompt_f32_engagement)


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
    replay = None
    frozen = None
    if args.replay_family:
        if not re.fullmatch(REPLAY_RE, args.replay_family):
            raise ValueError("Replay source needs a short family name")
        if args.replay_family == args.family:
            raise ValueError("Replay source must differ from the fresh evidence family")
        # Select the frozen per-rep source bytes ONCE, before any production signal, and
        # keep their provenance for the status/dry-run recheck.
        frozen, replay = load_replay(args.replay_family, evidence=runtime.EV)
    schedule = [None, *MATRIX[args.lever], None]
    if args.candidate is not None:
        schedule = [None, MATRIX[args.lever][args.candidate - 1], None]
    if not args.execute:
        print(json.dumps(dict(family=args.family, schedule=schedule, reps=3,
                              max_tokens=512, minimum_output=MIN_OUTPUT,
                              iq_mt_min=args.iq_mt_min, adapt_off=args.adapt_off,
                              prompt_f32=args.prompt_f32, replay_family=args.replay_family,
                              replay=replay,
                              restore=str(runtime.LAUNCHER) + " --recover", pins=fingerprint), indent=2))
        return 0
    private = []
    for i, tuning in enumerate(schedule):
        config = window_pair.prepare(f"{args.family}-{chr(97 + i)}", slots=4200, tuning=tuning,
                                     iq_mt_min=args.iq_mt_min, adapt_off=args.adapt_off,
                                     prompt_f32=args.prompt_f32)
        config.label = chr(97 + i)
        private.append(config)
    result = dict(started=time.time(), lever=args.lever, iq_mt_min=args.iq_mt_min,
                  adapt_off=args.adapt_off, prompt_f32=args.prompt_f32,
                  replay=replay, pairs=[], success=False, pins=fingerprint)
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
                    row["probes"].append(probe(config, state, args.family, rep,
                                               None if frozen is None else frozen[rep]))
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
            if replay is not None:
                # Recheck the pinned source artifacts and that every probe used the frozen
                # bytes before comparison (fail closed if the source mutated mid-window).
                _, prov_again = load_replay(args.replay_family, evidence=runtime.EV)
                if (prov_again["source_files"] != replay["source_files"]
                        or prov_again["status_sha256"] != replay["status_sha256"]):
                    raise RuntimeError("Replay source artifacts changed during measurement")
                for row in result["pairs"]:
                    for probe_row in row["probes"]:
                        expected = replay["request_sha256"].get(str(probe_row["rep"]))
                        if probe_row["request_sha256"] != expected:
                            raise RuntimeError(f"Probe bytes diverged from the frozen source (arm {row['label']})")
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
    parser.add_argument("--prompt-f32", action="store_true",
                        help="baseline env STRATA_HIP_PROMPT_F16=0 on EVERY arm (controls and "
                             "candidates alike): disables the prompt-FP16 override ONLY, "
                             "selecting the engine's non-FP16 default prompt GEMMs "
                             "(gemm.hpp prompt_f16). Not a whole-engine FP32 claim; startup "
                             "proves engagement from the private config and the live engine "
                             "environ, and refuses a HIP device reporting FP16 prompt GEMMs")
    parser.add_argument("--replay-family",
                        help="source family whose completed window's EXACT request bytes are "
                             "frozen and reused verbatim by every NEW arm; the new --family "
                             "stays a fresh artifact/lifecycle identity. Validated before any "
                             "production signal (raw SHA, fixture semantics, byte-identical "
                             "across arms per rep, distinct reps); source must differ from "
                             "--family")
    args = parser.parse_args()
    if args.candidate is not None and not 1 <= args.candidate <= len(MATRIX[args.lever]):
        parser.error("candidate outside the bounded matrix")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

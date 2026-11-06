#!/usr/bin/env python3
"""Read-only audit of a COMPLETED performance window.

No GPU, network, process, signal, or production action: it reads only the persisted
status/config/manifest/engine-log/request/reply artifacts. It re-verifies the window SHAPE
(success, 3/4 bracket arms, safe sequential labels, exactly reps 1,2,3), the baseline
ENGAGEMENT (config env/argv must match the recorded flags - not merely record them), the
manifest-vs-row tuning, the recorded engine argv against the config, request/message hashes,
serve-line metrics, validate_reply, the persisted startup proof, the stored comparison
arithmetic, and the pinned binaries. It FAILS CLOSED on any malformed, missing, mismatched,
unengaged, or corrupt input.

Coverage, stated honestly: the arithmetic reuses the window's OWN pure helpers
(validate_reply / compare / startup_report / helper_ab.parse_serve_line), so it is a
shared-code recompute (tamper detection), not an independent reimplementation. The live
engine-environ proof for --prompt-f32 is the engagement snapshot RECORDED at runtime in the
status; this audit reads that record - it does NOT re-query a finished process. The startup
and config re-derivation is persisted-only evidence, not a live-environment proof.

Pin policy: protected binaries and the canonical production config/launcher MUST match the
historical pins. Between-window harness diffs are allowed ONLY for an explicit allowlist
(default: the two files that carry this support phase - performance_window.py, window_pair.py);
any other harness change fails closed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import helper_ab
import performance_window as perf
import recovery_window as runtime
import window_pair

SUPPORT_HARNESS = frozenset({str(Path(perf.__file__)), str(Path(window_pair.__file__))})
APPROVED_TUNINGS = [t for values in perf.MATRIX.values() for t in values]


def _sha(data):
    return perf.digest(data)


def _flags(st):
    return dict(iq_mt_min=bool(st.get("iq_mt_min")), adapt_off=bool(st.get("adapt_off")),
                prompt_f32=bool(st.get("prompt_f32")))


def _argv_value(seq, flag):
    return seq[seq.index(flag) + 1] if flag in seq and seq.index(flag) + 1 < len(seq) else None


def _validate_shape(st, family):
    if st.get("success") is not True:
        raise RuntimeError(f"Window did not complete (success is not True): {family}")
    pairs = st.get("pairs")
    if not isinstance(pairs, list) or len(pairs) not in (3, 4):
        raise RuntimeError(f"Window must have 3 (bracket) or 4 (two-candidate) arms: {family}")
    labels = []
    for row in pairs:
        if not isinstance(row, dict) or not all(k in row for k in ("label", "probes", "startup", "state", "tuning")):
            raise RuntimeError(f"Window arm is not a full label/probes/startup/state/tuning record: {family}")
        label = row["label"]
        if not isinstance(label, str) or not label.isalpha() or len(label) != 1:
            raise RuntimeError(f"Window arm label is unsafe: {label!r}")
        reps = [p.get("rep") for p in row["probes"] if isinstance(p, dict)]
        if sorted(reps) != [1, 2, 3] or len(set(reps)) != 3:
            raise RuntimeError(f"Window arm {label} probes must be exactly reps 1,2,3: got {reps}")
        labels.append(label)
    if len(set(labels)) != len(labels) or labels != [chr(97 + i) for i in range(len(labels))]:
        raise RuntimeError(f"Window labels must be unique and sequential a,b,c[,d]: {labels}")
    if pairs[0]["tuning"] is not None or pairs[-1]["tuning"] is not None:
        raise RuntimeError(f"Window must bracket the candidates with control arms (tuning None at both ends): {family}")
    for row in pairs:
        if row["tuning"] is not None and not any(row["tuning"] == t for t in APPROVED_TUNINGS):
            raise RuntimeError(f"Window carries an unapproved tuning: {row['tuning']}")
    return pairs, labels


def _validate_baseline(cfg, flags, label):
    env, args = cfg.get("env", {}), cfg.get("args", [])
    if flags["iq_mt_min"]:
        if env.get("STRATA_IQ_MT_MIN") != "1":
            raise RuntimeError(f"iq_mt_min flag set but arm {label} config env is not STRATA_IQ_MT_MIN=1")
    elif "STRATA_IQ_MT_MIN" in env:
        raise RuntimeError(f"Arm {label} carries STRATA_IQ_MT_MIN without the flag")
    if flags["adapt_off"]:
        if "--adapt-swaps" not in args or _argv_value(args, "--adapt-swaps") != "0":
            raise RuntimeError(f"adapt_off flag set but arm {label} argv lacks --adapt-swaps 0")
    elif "--adapt-swaps" in args:
        raise RuntimeError(f"Arm {label} carries --adapt-swaps without the flag")
    if flags["prompt_f32"]:
        if env.get("STRATA_HIP_PROMPT_F16") != "0":
            raise RuntimeError(f"prompt_f32 flag set but arm {label} config env is not STRATA_HIP_PROMPT_F16=0")
    # flag off: F16 inherits production (byte-identical); no constraint on the off path.


def _validate_tuning(cfg, tuning, label):
    args, env = cfg.get("args", []), cfg.get("env", {})
    if tuning is None:
        if "--prefill-pipe" in args or env.get("STRATA_STAGE_TRIM") or str(cfg.get("layer_split")) != "32":
            raise RuntimeError(f"Control arm {label} is not the plain production baseline")
        return
    name = next(iter(tuning))
    if name == "prefill_pipe":
        if args.count("--prefill-pipe") != 1 or _argv_value(args, "--prefill-pipe") != str(tuning["prefill_pipe"]):
            raise RuntimeError(f"Arm {label} prefill-pipe argv does not match tuning {tuning}")
    elif name == "stage_trim":
        if env.get("STRATA_STAGE_TRIM") != "1":
            raise RuntimeError(f"Arm {label} stage-trim env does not match tuning {tuning}")
    elif name == "dflash":
        for flag, value in zip(("--spec", "--dflash-block", "--spec-min-p"), tuning["dflash"]):
            if _argv_value(args, flag) != str(value):
                raise RuntimeError(f"Arm {label} {flag} does not match tuning {tuning}")
    elif name == "layer_split":
        if str(cfg.get("layer_split")) != str(tuning["layer_split"]):
            raise RuntimeError(f"Arm {label} layer_split does not match tuning {tuning}")
    else:
        raise RuntimeError(f"Arm {label} tuning name {name} is not a lever applied by the window")


def _validate_engine_argv(state, cfg, label):
    argv = state.get("engine_argv")
    if not argv:
        return None  # not recorded on every runner; validated only where available
    cargs = cfg.get("args", [])
    mismatches = {flag: ( _argv_value(argv, flag), _argv_value(cargs, flag))
                  for flag in ("--adapt-swaps", "--prefill-pipe", "--prefill")
                  if _argv_value(argv, flag) != _argv_value(cargs, flag)}
    if mismatches:
        raise RuntimeError(f"Recorded engine argv diverges from the config baseline (arm {label}): {mismatches}")
    return dict(engine_argv_matches_config=True, flags=sorted("--adapt-swaps --prefill-pipe --prefill".split()))


def _pins_audit(stored_pins, allowed_harness=SUPPORT_HARNESS):
    current = perf.pins()  # raises if a protected binary diverged from its stored pin
    protected = {str(runtime.WORK / line.split(maxsplit=1)[1]) for line in perf.PROTECTED.read_text().splitlines()}
    canonical = {str(runtime.CONFIG), str(runtime.LAUNCHER)}
    changes = []
    for path, digest_now in current.items():
        kind = "protected" if path in protected else "canonical" if path in canonical else "harness"
        was = stored_pins.get(path)
        if was == digest_now:
            continue
        if kind != "harness":
            raise RuntimeError(f"{kind} pin changed and must not: {path}")
        if path not in allowed_harness:
            raise RuntimeError(f"harness pin changed outside the support allowlist: {path}")
        changes.append(dict(path=path, historical=was, current=digest_now))
    missing = [p for p in protected | canonical if p not in stored_pins]
    if missing:
        raise RuntimeError(f"Protected/canonical pins absent from the stored status: {missing}")
    return dict(changes=changes,
                harness_changed=sorted(c["path"] for c in changes),
                protected_and_canonical_intact=True)


def _probe_audit(ev, family, label, probe):
    rep = probe["rep"]
    req = Path(ev) / f"{family}-{label}-r{rep}-request.json"
    reply = Path(ev) / f"{family}-{label}-r{rep}-reply.json"
    seg = Path(ev) / f"{family}-{label}-r{rep}-engine.log"
    for path in (req, reply, seg):
        if not path.is_file():
            raise RuntimeError(f"Missing persisted artifact: {path.name}")
    body = req.read_bytes()
    request_sha = _sha(body)
    if request_sha != probe.get("request_sha256"):
        raise RuntimeError(f"Request SHA changed on disk: {req.name}")
    perf.validate_fixture(body)
    segment = seg.read_text(errors="replace")
    metrics = [m for line in segment.splitlines() if (m := helper_ab.parse_serve_line(line))]
    if len(metrics) != 1:
        raise RuntimeError(f"Expected one serve summary in {seg.name}, got {len(metrics)}")
    metric = metrics[0]
    out = perf.validate_reply(json.loads(reply.read_bytes()), metric)
    plans = [line for line in segment.splitlines()
             if line.startswith("strata prefill:") and "tokens in" in line]
    mismatches = {k: (v, probe.get(k)) for k, v in metric.items() if probe.get(k) != v}
    for key, value in out.items():
        if probe.get(key) != value:
            mismatches[key] = (value, probe.get(key))
    if plans != probe.get("plans"):
        mismatches["plans"] = (plans, probe.get("plans"))
    if mismatches:
        raise RuntimeError(f"Re-derived probe differs from the status ({family}-{label}-r{rep}): {mismatches}")
    return dict(request_sha256=request_sha, **out)


def _startup_audit(manifest_path, state, stored):
    manifest = json.loads(Path(manifest_path).read_text())
    cfg = SimpleNamespace(**{k: v for k, v in manifest.items() if k in ("config", "tuning", "family")})
    cfg.prompt_f32 = False  # process-free: never re-read the live engine environ of a finished window
    recomputed = perf.startup_report(cfg, state)
    for side in (recomputed, stored):
        side.pop("prompt_f32_engagement", None)
    diffs = {k: (v, stored.get(k)) for k, v in recomputed.items() if stored.get(k) != v}
    if diffs:
        raise RuntimeError(f"Persisted startup proof differs from the status ({manifest['family']}): {diffs}")
    return dict(baseline=_baseline(json.loads(Path(manifest["config"]).read_text())))


def _baseline(cfg):
    env, args = cfg.get("env", {}), cfg.get("args", [])
    return dict(iq_mt_min=env.get("STRATA_IQ_MT_MIN"), hip_prompt_f16=env.get("STRATA_HIP_PROMPT_F16"),
                argv={flag: _argv_value(args, flag) for flag in ("--prefill-pipe", "--adapt-swaps", "--prefill")})


def _prompt_f32_audit(cfg, stored_startup, log, flags, label):
    engagement = stored_startup.get("prompt_f32_engagement")
    if not flags["prompt_f32"]:
        if engagement is not None:
            raise RuntimeError(f"Arm {label} recorded a prompt-F32 engagement the status flag does not claim")
        return None
    if engagement is None:
        raise RuntimeError(f"prompt_f32 flag set but arm {label} has no stored engagement proof")
    live = engagement.get("engine_environ") or {}
    if (cfg.get("env", {}).get("STRATA_HIP_PROMPT_F16") != "0"
            or engagement.get("private_env") != "0"
            or live.get("STRATA_HIP_PROMPT_F16") != "0"
            or engagement.get("fp16_prompt_line_absent") is not True
            or perf.FP16_PROMPT_LINE.search(log)):
        raise RuntimeError(f"Arm {label} prompt-F32 engagement proof is incomplete or contradicted")
    return dict(engagement, live_env_proof="recorded engine-environ snapshot (live at runtime, not re-queried)")


def audit(family, ev=runtime.EV, allowed_harness=SUPPORT_HARNESS):
    status = Path(ev) / f"{family}-perf-status.json"
    if not status.is_file():
        raise RuntimeError(f"Window status is missing: {status.name}")
    try:
        st = json.loads(status.read_text())
    except ValueError as error:
        raise RuntimeError(f"Window status is malformed: {error}")
    pairs, labels = _validate_shape(st, family)
    flags = _flags(st)
    report = dict(family=family, status=str(status), status_sha256=_sha(status.read_bytes()),
                  lever=st.get("lever"), **flags, replay=st.get("replay"),
                  coverage="reuses the window's own pure helpers (validate_reply/compare/"
                           "startup_report/parse_serve_line): a shared-code recompute for tamper "
                           "detection, not an independent reimplementation; live engine-environ "
                           "proof is the recorded engagement snapshot, startup/config re-derivation "
                           "is persisted-only",
                  arms=[], pins=_pins_audit(st.get("pins", {}), allowed_harness))
    for row in pairs:
        label = row["label"]
        manifest = Path(ev) / f"{family}-{label}-manifest.json"
        if not manifest.is_file():
            raise RuntimeError(f"Arm manifest is missing: {manifest.name}")
        manifest_data = json.loads(manifest.read_text())
        if manifest_data.get("tuning") != row["tuning"]:
            raise RuntimeError(f"Arm {label} manifest tuning differs from the status row")
        cfg = json.loads(Path(manifest_data["config"]).read_text())
        log = Path(cfg["log"]).read_text(errors="replace")
        _validate_baseline(cfg, flags, label)
        _validate_tuning(cfg, row["tuning"], label)
        report["arms"].append(dict(
            label=label, tuning=row["tuning"],
            baseline=_baseline(cfg),
            prompt_f32=_prompt_f32_audit(cfg, row["startup"], log, flags, label),
            engine_argv=_validate_engine_argv(row["state"], cfg, label),
            startup=_startup_audit(manifest, row["state"], dict(row["startup"])),
            probes=[_probe_audit(ev, family, label, p) for p in row["probes"]]))
    recomputed = perf.compare([dict(label=r["label"], tuning=r["tuning"], probes=r["probes"]) for r in pairs])
    if json.dumps(recomputed, sort_keys=True) != json.dumps(st.get("comparison", {}), sort_keys=True):
        raise RuntimeError(f"Stored comparison arithmetic differs from a recompute for {family}")
    report["comparison"] = recomputed
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("families", nargs="+", help="completed window family names to audit (e.g. dfc2 dfc3 ppc2)")
    parser.add_argument("--out", help="new evidence path to write the report to; never overwrites")
    parser.add_argument("--allow-harness", action="append", default=[],
                        help="extra harness file allowed to differ between window and audit (repeatable)")
    args = parser.parse_args(argv)
    allowed = set(SUPPORT_HARNESS) | {str(Path(p).resolve()) for p in args.allow_harness}
    report = dict(audits=[audit(f, allowed_harness=allowed) for f in args.families], all_pass=True)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        out = Path(args.out)
        if out.exists():
            raise SystemExit(f"Refusing to overwrite {out}; choose a fresh evidence path")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

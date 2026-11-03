"""Read-only independent audit of retained windows; CPU-only, no GPU/network/process."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/mixed_gpu"))
import review_performance as review
import recovery_window as runtime
import performance_window as perf
import helper_ab

HARNESS = {str(ROOT / "tools/mixed_gpu/performance_window.py"),
           str(ROOT / "tools/mixed_gpu/window_pair.py")}

SERVE = ("strata serve: prompt 10000 tokens = 0 reused + 10000 read in 44400.0 ms (225.2 tok/s), "
         "512 generated in 13600.0 ms (37.6 tok/s), drafts accepted 300 of 600, 64 checkpoints "
         "helper=reqs=6644 rows=3322 bytes=341 timeouts=0 fallbacks=0")
STARTUP = """strata generate: expert cache 6069 slots, 9.12 GiB
strata generate: layer split: CUDA1 runs layers 32-47, expert cache 4780 slots
strata generate: layer split: CUDA0 runs layers 0-31
strata generate: expert-helper: 13727 of 24576 ranked pairs eligible in layers 0..47 (10849 held by a cache, 0 outside the window, 0 outside the geometry, 0 repeated, 0 Hadamard-folded)
dflash: trained block 8, pass K=4
expert-helper: 4093 rows on /private.sock (mirrored 4093/4200 slots, 6.25 GiB)
dflash: cross-GPU tap collection serial on CUDA1
"""


def make_window(ev, family, arms=("a", "b", "c"), tuning=None, flags=None, lever="pipe"):
    """Fully synthetic, self-contained completed window in ev (retained originals untouched)."""
    flags = flags or {}
    if tuning is None:
        tuning = ([None, {"prefill_pipe": 6144}, None] if len(arms) == 3
                  else [None, {"prefill_pipe": 1}, {"prefill_pipe": 6144}, None])
    pairs = []
    for i, label in enumerate(arms):
        tune = tuning[i]
        cfg = dict(args=["--spec", "5", "--dflash-block", "5", "--spec-min-p", "0.5", "--prefill", "8192"],
                   layer_split="32", env={})
        if flags.get("iq_mt_min"):
            cfg["env"]["STRATA_IQ_MT_MIN"] = "1"
        if flags.get("prompt_f32"):
            cfg["env"]["STRATA_HIP_PROMPT_F16"] = "0"
        if flags.get("adapt_off"):
            cfg["args"] += ["--adapt-swaps", "0"]
        if tune:
            name = next(iter(tune))
            if name == "prefill_pipe":
                cfg["args"] += ["--prefill-pipe", str(tune["prefill_pipe"])]
            elif name == "stage_trim":
                cfg["env"]["STRATA_STAGE_TRIM"] = "1"
            elif name == "dflash":
                for flag, value in zip(("--spec", "--dflash-block", "--spec-min-p"), tune["dflash"]):
                    cfg["args"][cfg["args"].index(flag) + 1] = str(value)
            elif name == "layer_split":
                cfg["layer_split"] = str(tune["layer_split"])
        engine = ev / f"{family}-{label}-engine.log"
        engine.write_text(STARTUP + ("CUDA0 loads the dense weights of layers 0-31 only\n"
                                     "CUDA1 loads the dense weights of layers 32-47 only\n"
                                     if tune and "stage_trim" in tune else ""))
        cfg["log"] = str(engine)
        cfg_path = ev / f"{family}-{label}-config.json"
        cfg_path.write_text(json.dumps(cfg))
        (ev / f"{family}-{label}-manifest.json").write_text(
            json.dumps(dict(config=str(cfg_path), tuning=tune, family=f"{family}-{label}")))
        helper = ev / f"{family}-{label}-helper.log"
        helper.write_text("expert-helper: filled 4093 slots (6.25 GiB)")
        state = dict(helper_log=str(helper), engine_argv=list(cfg["args"]), engine_pid=3)
        ns = SimpleNamespace(config=cfg_path, tuning=tune, family=f"{family}-{label}")
        startup = perf.startup_report(ns, state)
        if flags.get("prompt_f32"):
            startup["prompt_f32_engagement"] = dict(
                private_env="0", engine_environ={"STRATA_HIP_PROMPT_F16": "0"},
                engine_pid=3, fp16_prompt_line_absent=True)
        probes = []
        for rep in (1, 2, 3):
            req = perf.request_body(family, rep)
            (ev / f"{family}-{label}-r{rep}-request.json").write_bytes(req)
            reply = dict(usage=dict(completion_tokens=512), choices=[dict(
                finish_reason="length", message=dict(role="assistant", content=f"checklist {rep}"))])
            (ev / f"{family}-{label}-r{rep}-reply.json").write_text(json.dumps(reply))
            (ev / f"{family}-{label}-r{rep}-engine.log").write_text(SERVE + "\n")
            metric = helper_ab.parse_serve_line(SERVE)
            out = perf.validate_reply(reply, metric)
            probes.append(dict(rep=rep, request_sha256=perf.digest(req), **out, **metric, plans=[]))
        pairs.append(dict(label=label, tuning=tune, state=state, startup=startup,
                          probes=probes, prompt_channel=dict(requests=6644, rows=3322)))
    st = dict(started=0.0, success=True, lever=lever, iq_mt_min=bool(flags.get("iq_mt_min")),
              adapt_off=bool(flags.get("adapt_off")), prompt_f32=bool(flags.get("prompt_f32")),
              replay=None, pairs=pairs, pins=perf.pins(), comparison=perf.compare(pairs))
    (ev / f"{family}-perf-status.json").write_text(json.dumps(st))
    return st


@pytest.fixture
def fill_ready(monkeypatch):
    monkeypatch.setattr(perf.lifecycle, "fill_report", lambda _path: dict(slots=4093))


def test_review_clean_synthetic_window(tmp_path, fill_ready):
    make_window(tmp_path, "w1", flags=dict(iq_mt_min=True, adapt_off=True, prompt_f32=True))
    report = review.audit("w1", ev=tmp_path)
    assert report["iq_mt_min"] is True and report["adapt_off"] is True and report["prompt_f32"] is True
    assert all(arm["prompt_f32"]["live_env_proof"] for arm in report["arms"])
    assert all(arm["engine_argv"]["engine_argv_matches_config"] for arm in report["arms"])
    assert report["pins"]["protected_and_canonical_intact"] is True


@pytest.mark.parametrize("mutate,match", [
    ("success", "did not complete"),
    ("iq_env", "STRATA_IQ_MT_MIN=1"),
    ("adapt_argv", "--adapt-swaps 0"),
    ("f32_engagement", "no stored engagement"),
    ("f32_env", "STRATA_HIP_PROMPT_F16=0"),
    ("harness_pin", "outside the support allowlist"),
    ("bracket", "control arms"),
    ("reps", "probes must be exactly"),
    ("labels", "sequential"),
])
def test_review_rejects_previously_accepted_corruptions(tmp_path, fill_ready, mutate, match):
    make_window(tmp_path, "w1", flags=dict(iq_mt_min=True, adapt_off=True, prompt_f32=True))
    st = json.loads((tmp_path / "w1-perf-status.json").read_text())
    cfg_path = Path(json.loads((tmp_path / "w1-a-manifest.json").read_text())["config"])
    cfg = json.loads(cfg_path.read_text())
    if mutate == "success":
        st["success"] = False
    elif mutate == "iq_env":
        cfg["env"].pop("STRATA_IQ_MT_MIN")
        cfg_path.write_text(json.dumps(cfg))
    elif mutate == "adapt_argv":
        cfg["args"] = [a for a in cfg["args"] if a != "--adapt-swaps" and a != "0"]
        cfg_path.write_text(json.dumps(cfg))
    elif mutate == "f32_engagement":
        st["pairs"][0]["startup"].pop("prompt_f32_engagement")
    elif mutate == "f32_env":
        cfg["env"]["STRATA_HIP_PROMPT_F16"] = "1"
        cfg_path.write_text(json.dumps(cfg))
    elif mutate == "harness_pin":
        st["pins"][str(ROOT / "tools/mixed_gpu/recovery_window.py")] = "0" * 64
    elif mutate == "bracket":
        st["pairs"][0]["tuning"] = {"prefill_pipe": 1}
    elif mutate == "reps":
        st["pairs"][0]["probes"][2]["rep"] = 1
    elif mutate == "labels":
        st["pairs"][1]["label"] = "z"
    (tmp_path / "w1-perf-status.json").write_text(json.dumps(st))
    with pytest.raises(RuntimeError, match=match):
        review.audit("w1", ev=tmp_path)


@pytest.mark.skipif(not (runtime.EV / "ppc2-perf-status.json").is_file(),
                    reason="retained window artifacts unavailable")
@pytest.mark.parametrize("family", ["dfc2", "dfc3", "ppc2"])
def test_review_recomputes_retained_windows(family):
    report = review.audit(family)
    assert report["pins"]["protected_and_canonical_intact"] is True
    assert set(report["pins"]["harness_changed"]).issubset(HARNESS)
    assert all(a["probes"] for a in report["arms"])
    assert report["comparison"]["candidates"]


def test_review_missing_window_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match="status is missing"):
        review.audit("nope", ev=tmp_path)


def test_review_absent_protected_pins_fail_closed(tmp_path, fill_ready):
    (tmp_path / "x-perf-status.json").write_text(json.dumps(
        dict(success=True, pairs=[dict(label="a", tuning=None, probes=[], startup={}, state={})],
             pins={})))
    with pytest.raises(RuntimeError, match="bracket"):
        review.audit("x", ev=tmp_path)


@pytest.mark.skipif(not (runtime.EV / "ppc2-perf-status.json").is_file(),
                    reason="retained window artifacts unavailable")
def test_review_refuses_overwrite(tmp_path):
    (tmp_path / "out.json").write_text("prior")
    with pytest.raises(SystemExit, match="Refusing to overwrite"):
        review.main(["ppc2", "--out", str(tmp_path / "out.json")])
    assert (tmp_path / "out.json").read_text() == "prior"

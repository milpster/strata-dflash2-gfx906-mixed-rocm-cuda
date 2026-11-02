"""CPU-only checks for the opt-in four-lever evidence path."""
import json
import io
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/mixed_gpu"))
import performance_window as perf
import window_pair


def config_fixture():
    return dict(args=["--spec", "5", "--dflash-block", "5", "--spec-min-p", "0.5"],
                layer_split="32", env={"KEEP": "1"}, capacity=262144)


@pytest.mark.parametrize("tuning", [t for values in perf.MATRIX.values() for t in values])
def test_tuning_is_narrow(tuning):
    cfg = config_fixture()
    window_pair.apply_tuning(cfg, tuning)
    assert cfg["capacity"] == 262144
    assert cfg["env"]["KEEP"] == "1"
    if "stage_trim" in tuning:
        assert cfg["env"]["STRATA_STAGE_TRIM"] == "1"
    elif "layer_split" in tuning:
        assert cfg["layer_split"] == str(tuning["layer_split"])
    elif "prefill_pipe" in tuning:
        assert cfg["args"][-2:] == ["--prefill-pipe", str(tuning["prefill_pipe"])]
    else:
        assert cfg["args"][1::2] == list(map(str, tuning["dflash"]))


@pytest.mark.parametrize("tuning", [None, {}, {"stage_trim": 1}, {"other": 1},
    {"layer_split": 32}, {"layer_split": "30"}, {"prefill_pipe": True},
    {"stage_trim": True, "layer_split": 30}, {"dflash": [7, 7, .5]},
    {"dflash": [5, 4, float("nan")]}, {"dflash": [5, 4, 0]}, {"dflash": [True, 1, .5]}])
def test_bad_tuning_refused(tuning):
    with pytest.raises(ValueError):
        window_pair.apply_tuning(config_fixture(), tuning)


def test_tuning_missing_or_ambiguous_flag():
    cfg = config_fixture()
    cfg["args"] += ["--spec", "5"]
    with pytest.raises(ValueError, match="exactly one"):
        window_pair.apply_tuning(cfg, {"dflash": [7, 6, .3]})


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local fixture unavailable")
def test_private_trim_and_control(tmp_path):
    sources = [window_pair.PROD_CONFIG, window_pair.PROD_LAUNCHER]
    before = {p: p.read_bytes() for p in sources}
    control = window_pair.prepare("pc", evidence=tmp_path, slots=4200)
    candidate = window_pair.prepare("pt", evidence=tmp_path, slots=4200,
                                     tuning={"stage_trim": True})
    prod = json.loads(before[window_pair.PROD_CONFIG])
    a, b = [json.loads(Path(c.config).read_text()) for c in (control, candidate)]
    assert a["env"] == prod["env"]
    assert b["env"] == dict(prod["env"], STRATA_STAGE_TRIM="1")
    assert a["args"] == b["args"] == prod["args"]
    assert all(p.read_bytes() == old for p, old in before.items())


def test_tuning_conflict_before_files(tmp_path):
    with pytest.raises(ValueError, match="cannot compose"):
        window_pair.prepare("bad", evidence=tmp_path, tuning={"stage_trim": True}, helper_off=True)
    with pytest.raises(ValueError):
        window_pair.prepare("bad", evidence=tmp_path, tuning={"layer_split": 100})
    assert not list(tmp_path.iterdir())


def test_sustained_request_is_arm_independent():
    a = perf.request_body("family", 1)
    assert a == perf.request_body("family", 1)
    assert a != perf.request_body("family", 2)
    body = json.loads(a)
    assert body["max_tokens"] == 512 and body["temperature"] == 0
    assert body["reasoning_budget_tokens"] == 0
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "120 numbered" in body["messages"][0]["content"]
    assert "one focused paragraph" not in body["messages"][0]["content"]


def metric_fixture():
    return dict(generated=512, cancelled=False, reused=0, read=10000, prompt=10000,
                helper_reqs=100, helper_timeouts=0, helper_fallbacks=0,
                offered=600, accepted=300, gen_rate=30, read_rate=200)


def reply_fixture():
    return dict(usage=dict(completion_tokens=512), choices=[dict(
        finish_reason="length", message=dict(role="assistant", content="Useful checklist."))])


def test_output_hash_is_actual_message():
    a = perf.validate_reply(reply_fixture(), metric_fixture())
    reply = reply_fixture()
    reply["id"] = "irrelevant"
    assert a == perf.validate_reply(reply, metric_fixture())
    reply["choices"][0]["message"]["content"] = "Different checklist."
    b = perf.validate_reply(reply, metric_fixture())
    assert a["message_sha256"] != b["message_sha256"]


@pytest.mark.parametrize("key,value", [("reused", 1), ("helper_reqs", None),
    ("helper_timeouts", 1), ("offered", 0), ("cancelled", True), ("prompt", 10001),
    ("read", 8000), ("read_rate", 0)])
def test_bad_probe_rejected(key, value):
    metric = metric_fixture()
    metric[key] = value
    with pytest.raises(RuntimeError, match="Invalid sustained"):
        perf.validate_reply(reply_fixture(), metric)


def test_short_or_reasoning_only_rejected():
    reply = reply_fixture()
    reply["usage"]["completion_tokens"] = 18
    with pytest.raises(RuntimeError):
        perf.validate_reply(reply, metric_fixture())
    reply = reply_fixture()
    reply["choices"][0]["message"] = dict(content="", reasoning="thinking only")
    with pytest.raises(RuntimeError):
        perf.validate_reply(reply, metric_fixture())


def rows_fixture(candidate_rate=33, last_rate=30):
    return [dict(label=label, tuning=tune, probes=[dict(rep=rep, gen_rate=rate, read_rate=200,
                request_sha256=f"request{rep}", message_sha256=f"output{rep}") for rep in range(3)])
            for label, tune, rate in (("a", None, 30), ("b", {"stage_trim": True}, candidate_rate),
                                      ("c", None, last_rate))]


@pytest.mark.parametrize("rate,last,verdict", [(33, 30, "CANDIDATE_NEEDS_CONFIRMATION"),
    (30, 30, "NO_GAIN"), (33, 40, "INCONCLUSIVE_CONTROL_DRIFT")])
def test_comparison_keeps_drift(rate, last, verdict):
    result = perf.compare(rows_fixture(rate, last))
    assert result["candidates"][0]["verdict"] == verdict


def test_comparison_keeps_output_mismatch_and_input_parity_separate():
    rows = rows_fixture()
    rows[1]["probes"][0]["message_sha256"] = "different output"
    result = perf.compare(rows)
    assert result["candidates"][0]["verdict"] == "OUTPUT_MISMATCH"
    assert result["candidates"][0]["timing"]["gen_rate"] == "SPEED_CANDIDATE"
    rows[-1]["probes"][0]["message_sha256"] = "unstable control"
    assert perf.compare(rows)["candidates"][0]["verdict"] == "INCONCLUSIVE_BASELINE_OUTPUT"
    rows[1]["probes"][0]["request_sha256"] = "different input"
    with pytest.raises(RuntimeError, match="request bytes"):
        perf.compare(rows)


STARTUP = """strata generate: expert cache 6069 slots, 9.12 GiB
strata generate: layer split: CUDA1 runs layers 32-47, expert cache 4780 slots
strata generate: layer split: CUDA0 runs layers 0-31
strata generate: expert-helper: 13727 of 24576 ranked pairs eligible in layers 0..47 (10849 held by a cache, 0 outside the window, 0 outside the geometry, 0 repeated, 0 Hadamard-folded)
dflash: trained block 8, pass K=4
expert-helper: 4093 rows on /private.sock (mirrored 4093/4200 slots, 6.25 GiB)
dflash: cross-GPU tap collection serial on CUDA1
"""


@pytest.mark.parametrize("trim", [False, True])
def test_startup_proof(tmp_path, monkeypatch, trim):
    cfg = config_fixture()
    cfg["log"] = str(tmp_path / "engine.log")
    Path(cfg["log"]).write_text(STARTUP + ("CUDA0 loads the dense weights of layers 0-31 only\n"
        "CUDA1 loads the dense weights of layers 32-47 only\n" if trim else ""))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    helper = tmp_path / "helper.log"
    helper.write_text("fill placeholder")
    monkeypatch.setattr(perf.lifecycle, "fill_report", lambda _: dict(slots=4093))
    config = SimpleNamespace(config=path, tuning={"stage_trim": True} if trim else None)
    report = perf.startup_report(config, {"helper_log": str(helper)})
    assert report["eligible_pairs"] + report["held_pairs"] == 24576
    assert report["effective_k"] == 4
    if trim:
        Path(cfg["log"]).write_text(STARTUP)
        with pytest.raises(RuntimeError, match="Stage-trim"):
            perf.startup_report(config, {"helper_log": str(helper)})


def run_world(monkeypatch, tmp_path, *, fail_probe=False, fail_restore=False):
    """No processes/HTTP/signals; isolate new runner's finally and ownership logic."""
    monkeypatch.setattr(perf.runtime, "EV", tmp_path)
    source = tmp_path / "production.json"
    source.write_text(json.dumps(dict(env={})))
    monkeypatch.setattr(perf.runtime, "CONFIG", source)
    monkeypatch.setattr(perf, "pins", lambda: {"pinned": "same"})
    events = []

    def prepare(family, **kwargs):
        path = tmp_path / f"{family}.json"
        path.write_text(json.dumps(dict(log=str(tmp_path / "engine.log"))))
        return SimpleNamespace(family=family, config=path, launcher="launcher", state="state",
                               base_url="url", socket2="socket2", tuning=kwargs.get("tuning"))

    class Proc:
        pid = 123

    def stop(_pid, result):
        events.append("stop")
        result.update(outage_started=True, old_production=dict(processes={1: "old"}))

    def restore(result, family):
        events.append("restore")
        if fail_restore:
            raise RuntimeError("restore failed")
        result["restored"] = dict(completion_http=200)

    def probe(*_):
        if fail_probe:
            raise RuntimeError("probe failed")
        return dict(gen_rate=30, read_rate=200)

    monkeypatch.setattr(perf.window_pair, "prepare", prepare)
    monkeypatch.setattr(perf.lifecycle, "stop_production", stop)
    monkeypatch.setattr(perf.lifecycle, "restore_production", restore)
    monkeypatch.setattr(perf.runtime, "launch", lambda *_: Proc())
    monkeypatch.setattr(perf.pair, "process_token", lambda _: ("S", "token"))
    monkeypatch.setattr(perf.pair, "descendants", lambda _, known: dict(known))
    monkeypatch.setattr(perf.pair, "alive", lambda *_: False)
    monkeypatch.setattr(perf.pair, "stop_group", lambda *_: events.append("cleanup"))
    monkeypatch.setattr(perf.runtime, "wait_for", lambda fn: fn())
    monkeypatch.setattr(perf.runtime, "attached", lambda **_: dict(helper_pid=2))
    monkeypatch.setattr(perf.runtime, "engine_of", lambda _: 3)
    monkeypatch.setattr(perf.runtime, "command", lambda _: ["pinned engine"])
    monkeypatch.setattr(perf, "startup_report", lambda *_: {})
    monkeypatch.setattr(perf, "probe", probe)
    return events, SimpleNamespace(family="unit", lever="trim", candidate=None,
                                   expected_supervisor=1, execute=True)


@pytest.mark.parametrize("restore_fails", [False, True])
def test_failure_always_cleans_and_restores(monkeypatch, tmp_path, restore_fails):
    events, args = run_world(monkeypatch, tmp_path, fail_probe=True, fail_restore=restore_fails)
    with pytest.raises(RuntimeError, match="restore failed" if restore_fails else "probe failed"):
        perf.run(args)
    assert events == ["stop", "cleanup", "restore"]
    result = json.loads((tmp_path / "unit-perf-status.json").read_text())
    assert result["success"] is False and "probe failed" in result["error"]
    assert ("restore_error" in result) is restore_fails


def test_failed_ownership_refuses_concurrent_restore(monkeypatch, tmp_path):
    events, args = run_world(monkeypatch, tmp_path, fail_probe=True)
    monkeypatch.setattr(perf.pair, "alive", lambda *_: True)
    with pytest.raises(RuntimeError, match="survived"):
        perf.run(args)
    assert events == ["stop", "cleanup"]


def test_preflight_refusal_never_stops(monkeypatch, tmp_path):
    events, args = run_world(monkeypatch, tmp_path)
    (tmp_path / "unit-perf-status.json").write_text("existing evidence")
    with pytest.raises(ValueError, match="overwrite"):
        perf.run(args)
    assert events == []


def test_success_runs_all_arms_and_restores(monkeypatch, tmp_path):
    events, args = run_world(monkeypatch, tmp_path)
    (tmp_path / "engine.log").write_text(
        "the prompt tier's channel (socket2) closed after 3 prompt requests / 9 prompt rows\n")
    monkeypatch.setattr(perf.lifecycle, "graceful_stop", lambda *_: events.append("arm_stop"))
    monkeypatch.setattr(perf, "probe", lambda cfg, state, family, rep: dict(
        rep=rep, gen_rate=30, read_rate=200, request_sha256=f"req{rep}",
        message_sha256=f"out{rep}", plans=[]))
    assert perf.run(args) == 0
    result = json.loads((tmp_path / "unit-perf-status.json").read_text())
    assert events == ["stop", "arm_stop", "arm_stop", "arm_stop", "restore"]
    assert result["success"] is True
    assert len(result["pairs"]) == 3
    assert all(len(arm["probes"]) == 3 for arm in result["pairs"])
    assert result["comparison"]["candidates"][0]["verdict"] == "NO_GAIN"
    assert result["restored"]["completion_http"] == 200


def test_http_failure_retains_request_reply_and_log(monkeypatch, tmp_path):
    monkeypatch.setattr(perf.runtime, "EV", tmp_path)
    log = tmp_path / "engine.log"
    log.write_text("startup\n")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(dict(log=str(log))))
    config = SimpleNamespace(label="a", config=cfg, base_url="http://localhost:12345")

    def fail(*_, **__):
        with log.open("a") as out:
            out.write("engine request failed\n")
        raise perf.urllib.error.HTTPError("url", 503, "unavailable", {}, io.BytesIO(b"error evidence"))

    monkeypatch.setattr(perf.urllib.request, "urlopen", fail)
    with pytest.raises(perf.urllib.error.HTTPError):
        perf.probe(config, {}, "unit", 1)
    assert (tmp_path / "unit-a-r1-request.json").is_file()
    assert (tmp_path / "unit-a-r1-reply.json").read_bytes() == b"error evidence"
    assert (tmp_path / "unit-a-r1-engine.log").read_text() == "engine request failed\n"

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
    assert body["messages"][0]["content"].count(perf.PARAGRAPH) == 320
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


def run_world(monkeypatch, tmp_path, *, fail_probe=False, fail_restore=False,
              iq_mt_min=False, adapt_off=False, prompt_f32=False):
    """No processes/HTTP/signals; isolate new runner's finally and ownership logic."""
    monkeypatch.setattr(perf.runtime, "EV", tmp_path)
    source = tmp_path / "production.json"
    source.write_text(json.dumps(dict(env={})))
    monkeypatch.setattr(perf.runtime, "CONFIG", source)
    monkeypatch.setattr(perf, "pins", lambda: {"pinned": "same"})
    events, prepared = [], []

    def prepare(family, **kwargs):
        path = tmp_path / f"{family}.json"
        path.write_text(json.dumps(dict(log=str(tmp_path / "engine.log"))))
        prepared.append(dict(family=family, iq_mt_min=kwargs.get("iq_mt_min"),
                             adapt_off=kwargs.get("adapt_off"),
                             prompt_f32=kwargs.get("prompt_f32")))
        return SimpleNamespace(family=family, config=path, launcher="launcher", state="state",
                               base_url="url", socket2="socket2", tuning=kwargs.get("tuning"),
                               prompt_f32=kwargs.get("prompt_f32", False))

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
    monkeypatch.setattr(perf.runtime, "engine_of", lambda state, engine=None: 3)
    monkeypatch.setattr(perf.runtime, "command", lambda _: ["pinned engine"])
    monkeypatch.setattr(perf, "startup_report", lambda *_: {})
    monkeypatch.setattr(perf, "probe", probe)
    return events, SimpleNamespace(family="unit", lever="trim", candidate=None,
                                   expected_supervisor=1, execute=True,
                                   iq_mt_min=iq_mt_min, adapt_off=adapt_off,
                                   prompt_f32=prompt_f32, replay_family=None), prepared


@pytest.mark.parametrize("restore_fails", [False, True])
def test_failure_always_cleans_and_restores(monkeypatch, tmp_path, restore_fails):
    events, args, _ = run_world(monkeypatch, tmp_path, fail_probe=True, fail_restore=restore_fails)
    with pytest.raises(RuntimeError, match="restore failed" if restore_fails else "probe failed"):
        perf.run(args)
    assert events == ["stop", "cleanup", "restore"]
    result = json.loads((tmp_path / "unit-perf-status.json").read_text())
    assert result["success"] is False and "probe failed" in result["error"]
    assert ("restore_error" in result) is restore_fails


def test_failed_ownership_refuses_concurrent_restore(monkeypatch, tmp_path):
    events, args, _ = run_world(monkeypatch, tmp_path, fail_probe=True)
    monkeypatch.setattr(perf.pair, "alive", lambda *_: True)
    with pytest.raises(RuntimeError, match="survived"):
        perf.run(args)
    assert events == ["stop", "cleanup"]


def test_preflight_refusal_never_stops(monkeypatch, tmp_path):
    events, args, _ = run_world(monkeypatch, tmp_path)
    (tmp_path / "unit-perf-status.json").write_text("existing evidence")
    with pytest.raises(ValueError, match="overwrite"):
        perf.run(args)
    assert events == []


def test_success_runs_all_arms_and_restores(monkeypatch, tmp_path):
    events, args, _ = run_world(monkeypatch, tmp_path)
    (tmp_path / "engine.log").write_text(
        "the prompt tier's channel (socket2) closed after 3 prompt requests / 9 prompt rows\n")
    monkeypatch.setattr(perf.lifecycle, "graceful_stop", lambda *_: events.append("arm_stop"))
    monkeypatch.setattr(perf, "probe", lambda cfg, state, family, rep, body=None: dict(
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


def test_ambient_iq_mt_min_refused_before_any_signal(monkeypatch, tmp_path):
    events, args, _ = run_world(monkeypatch, tmp_path)
    monkeypatch.setenv("STRATA_IQ_MT_MIN", "1")
    with pytest.raises(ValueError, match="STRATA_IQ_MT_MIN"):
        perf.run(args)
    assert events == []  # the baseline guard fires before any production signal


def test_iq_mt_min_flag_reaches_every_arm_and_the_record(monkeypatch, tmp_path):
    events, args, prepared = run_world(monkeypatch, tmp_path, iq_mt_min=True)
    (tmp_path / "engine.log").write_text(
        "the prompt tier's channel (socket2) closed after 3 prompt requests / 9 prompt rows\n")
    monkeypatch.setattr(perf.lifecycle, "graceful_stop", lambda *_: events.append("arm_stop"))
    monkeypatch.setattr(perf, "probe", lambda cfg, state, family, rep, body=None: dict(
        rep=rep, gen_rate=30, read_rate=200, request_sha256=f"req{rep}",
        message_sha256=f"out{rep}", plans=[]))
    assert perf.run(args) == 0
    assert [row["iq_mt_min"] for row in prepared] == [True, True, True]  # controls and candidates alike
    assert [row["adapt_off"] for row in prepared] == [False, False, False]
    result = json.loads((tmp_path / "unit-perf-status.json").read_text())
    assert result["iq_mt_min"] is True
    assert result["adapt_off"] is False


def test_adapt_off_flag_reaches_every_arm_and_the_record(monkeypatch, tmp_path):
    events, args, prepared = run_world(monkeypatch, tmp_path, adapt_off=True)
    (tmp_path / "engine.log").write_text(
        "the prompt tier's channel (socket2) closed after 3 prompt requests / 9 prompt rows\n")
    monkeypatch.setattr(perf.lifecycle, "graceful_stop", lambda *_: events.append("arm_stop"))
    monkeypatch.setattr(perf, "probe", lambda cfg, state, family, rep, body=None: dict(
        rep=rep, gen_rate=30, read_rate=200, request_sha256=f"req{rep}",
        message_sha256=f"out{rep}", plans=[]))
    assert perf.run(args) == 0
    assert [row["adapt_off"] for row in prepared] == [True, True, True]  # controls and candidates alike
    assert [row["iq_mt_min"] for row in prepared] == [False, False, False]
    result = json.loads((tmp_path / "unit-perf-status.json").read_text())
    assert result["adapt_off"] is True


def test_prompt_f32_flag_reaches_every_arm_and_the_record(monkeypatch, tmp_path):
    events, args, prepared = run_world(monkeypatch, tmp_path, prompt_f32=True)
    (tmp_path / "engine.log").write_text(
        "the prompt tier's channel (socket2) closed after 3 prompt requests / 9 prompt rows\n")
    monkeypatch.setattr(perf.lifecycle, "graceful_stop", lambda *_: events.append("arm_stop"))
    monkeypatch.setattr(perf, "probe", lambda cfg, state, family, rep, body=None: dict(
        rep=rep, gen_rate=30, read_rate=200, request_sha256=f"req{rep}",
        message_sha256=f"out{rep}", plans=[]))
    assert perf.run(args) == 0
    assert [row["prompt_f32"] for row in prepared] == [True, True, True]  # controls and candidates alike
    result = json.loads((tmp_path / "unit-perf-status.json").read_text())
    assert result["prompt_f32"] is True


def make_source(ev, source="src", arms=("a", "b", "c")):
    """Synthetic completed window: valid status + per-arm, per-rep request files."""
    bodies = {rep: perf.request_body(source, rep) for rep in (1, 2, 3)}
    pairs = [dict(label=label,
                  probes=[dict(rep=rep, request_sha256=perf.digest(bodies[rep]))
                          for rep in (1, 2, 3)]) for label in arms]
    for label in arms:
        for rep in (1, 2, 3):
            (ev / f"{source}-{label}-r{rep}-request.json").write_bytes(bodies[rep])
    (ev / f"{source}-perf-status.json").write_text(json.dumps(dict(success=True, pairs=pairs)))
    return bodies


def test_load_replay_freezes_source_bytes(tmp_path):
    bodies = make_source(tmp_path)
    frozen, provenance = perf.load_replay("src", evidence=tmp_path)
    assert frozen == bodies                                    # raw bytes reused verbatim
    assert provenance["request_sha256"] == {str(r): perf.digest(bodies[r]) for r in (1, 2, 3)}
    assert len(provenance["source_files"]) == 9 and provenance["arms"] == ["a", "b", "c"]
    assert provenance["status_sha256"] == perf.digest((tmp_path / "src-perf-status.json").read_bytes())


def test_load_replay_pins_status_sha(tmp_path):
    make_source(tmp_path)
    before = perf.digest((tmp_path / "src-perf-status.json").read_bytes())
    _, provenance = perf.load_replay("src", evidence=tmp_path)
    assert provenance["status_sha256"] == before


def test_load_replay_rejects_two_arms(tmp_path):
    make_source(tmp_path, arms=("a", "b"))
    with pytest.raises(ValueError, match="bracket"):
        perf.load_replay("src", evidence=tmp_path)


def test_load_replay_rejects_non_sequential_labels(tmp_path):
    make_source(tmp_path, arms=("a", "c", "b"))
    with pytest.raises(ValueError, match="sequential"):
        perf.load_replay("src", evidence=tmp_path)


def test_load_replay_rejects_bad_reps(tmp_path):
    bodies = make_source(tmp_path)
    st = json.loads((tmp_path / "src-perf-status.json").read_text())
    st["pairs"][0]["probes"] = [dict(rep=1, request_sha256=perf.digest(bodies[1])),
                                dict(rep=1, request_sha256=perf.digest(bodies[1])),
                                dict(rep=3, request_sha256=perf.digest(bodies[3]))]
    (tmp_path / "src-perf-status.json").write_text(json.dumps(st))
    with pytest.raises(ValueError, match="reps must be exactly"):
        perf.load_replay("src", evidence=tmp_path)


def test_load_replay_rejects_unsafe_label(tmp_path):
    make_source(tmp_path)
    st = json.loads((tmp_path / "src-perf-status.json").read_text())
    st["pairs"][0]["label"] = "a b"
    (tmp_path / "src-perf-status.json").write_text(json.dumps(st))
    with pytest.raises(ValueError, match="unsafe"):
        perf.load_replay("src", evidence=tmp_path)


def _fixture(**over):
    body = json.loads(perf.request_body("src", 1))
    for key, value in over.items():
        if value is Ellipsis:
            body.pop(key, None)
        else:
            body[key] = value
    return json.dumps(body).encode()


def test_validate_fixture_accepts_verbatim_request_body():
    body = perf.request_body("src", 1)
    assert perf.validate_fixture(body)["seed"] == 1729


@pytest.mark.parametrize("key,value,match", [
    ("stop", ["stop"], "key set"),
    ("tools", [{}], "key set"),
    ("model", "other-model", "model"),
    ("max_tokens", True, "max_tokens"),
    ("temperature", False, "temperature"),
    ("seed", True, "seed"),
    ("chat_template_kwargs", {"enable_thinking": False, "top_k": 5}, "chat_template_kwargs"),
])
def test_validate_fixture_rejects_controls_and_types(tmp_path, key, value, match):
    with pytest.raises(ValueError, match=match):
        perf.validate_fixture(_fixture(**{key: value}))


def test_validate_fixture_rejects_multi_and_nonstring_messages():
    body = json.loads(perf.request_body("src", 1))
    body["messages"].append(dict(role="user", content="extra"))
    with pytest.raises(ValueError, match="one user message"):
        perf.validate_fixture(json.dumps(body).encode())
    body = json.loads(perf.request_body("src", 1))
    body["messages"][0]["content"] = 123
    with pytest.raises(ValueError, match="one user message"):
        perf.validate_fixture(json.dumps(body).encode())
    body = json.loads(perf.request_body("src", 1))
    body["messages"][0]["name"] = "extra"
    with pytest.raises(ValueError, match="one user message"):
        perf.validate_fixture(json.dumps(body).encode())


def test_load_replay_missing_source(tmp_path):
    make_source(tmp_path)
    (tmp_path / "src-perf-status.json").unlink()
    with pytest.raises(ValueError, match="status is missing"):
        perf.load_replay("src", evidence=tmp_path)


def test_load_replay_incomplete_source(tmp_path):
    make_source(tmp_path)
    (tmp_path / "src-perf-status.json").write_text(json.dumps(dict(success=False, pairs=[dict(label="a", probes=[])])))
    with pytest.raises(ValueError, match="did not complete"):
        perf.load_replay("src", evidence=tmp_path)


def test_load_replay_missing_request_file(tmp_path):
    make_source(tmp_path)
    (tmp_path / "src-a-r2-request.json").unlink()
    with pytest.raises(ValueError, match="request is missing"):
        perf.load_replay("src", evidence=tmp_path)


def test_load_replay_corrupt_request_json(tmp_path):
    make_source(tmp_path)
    (tmp_path / "src-a-r1-request.json").write_bytes(b"{not json")
    with pytest.raises(ValueError, match="hash mismatch|not valid JSON"):
        perf.load_replay("src", evidence=tmp_path)


def test_load_replay_hash_mismatch(tmp_path):
    bodies = make_source(tmp_path)
    tampered = json.loads(bodies[1]); tampered["messages"][0]["content"] += " x"
    (tmp_path / "src-a-r1-request.json").write_bytes(json.dumps(tampered).encode())
    with pytest.raises(ValueError, match="hash mismatch"):
        perf.load_replay("src", evidence=tmp_path)


def test_load_replay_non_fixture_request(tmp_path):
    make_source(tmp_path)
    body = json.loads((tmp_path / "src-a-r1-request.json").read_bytes())
    body["temperature"] = 1
    sha = perf.digest(json.dumps(body).encode())
    (tmp_path / "src-a-r1-request.json").write_bytes(json.dumps(body).encode())
    st = json.loads((tmp_path / "src-perf-status.json").read_text())
    for pair in st["pairs"]:
        for probe in pair["probes"]:
            if probe["rep"] == 1:
                probe["request_sha256"] = sha
    (tmp_path / "src-perf-status.json").write_text(json.dumps(st))
    with pytest.raises(ValueError, match="number 0"):
        perf.load_replay("src", evidence=tmp_path)


def test_load_replay_cross_arm_mismatch(tmp_path):
    bodies = make_source(tmp_path)
    other = json.loads(bodies[1]); other["messages"][0]["content"] += " arm b drift"
    sha = perf.digest(json.dumps(other).encode())
    (tmp_path / "src-b-r1-request.json").write_bytes(json.dumps(other).encode())
    st = json.loads((tmp_path / "src-perf-status.json").read_text())
    for pair in st["pairs"]:
        if pair["label"] == "b":
            for probe in pair["probes"]:
                if probe["rep"] == 1:
                    probe["request_sha256"] = sha
    (tmp_path / "src-perf-status.json").write_text(json.dumps(st))
    with pytest.raises(ValueError, match="differ across arms"):
        perf.load_replay("src", evidence=tmp_path)


def test_load_replay_reps_not_distinct(tmp_path):
    bodies = make_source(tmp_path)
    # Make every rep byte-identical and re-record the hashes, then expect refusal.
    st = json.loads((tmp_path / "src-perf-status.json").read_text())
    for pair in st["pairs"]:
        for rep in (1, 2, 3):
            (tmp_path / f"src-{pair['label']}-r{rep}-request.json").write_bytes(bodies[1])
        for probe in pair["probes"]:
            probe["request_sha256"] = perf.digest(bodies[1])
    (tmp_path / "src-perf-status.json").write_text(json.dumps(st))
    with pytest.raises(ValueError, match="not distinct"):
        perf.load_replay("src", evidence=tmp_path)


def test_replay_source_equals_family_rejected(monkeypatch, tmp_path):
    make_source(tmp_path)
    events, args, _ = run_world(monkeypatch, tmp_path)
    args.replay_family = "unit"
    with pytest.raises(ValueError, match="must differ"):
        perf.run(args)
    assert events == []            # rejected before any production signal
    assert not (tmp_path / "unit-perf-status.json").exists()


def test_replay_source_missing_rejected_before_signal(monkeypatch, tmp_path):
    events, args, _ = run_world(monkeypatch, tmp_path)
    args.replay_family = "src"     # no artifacts staged
    with pytest.raises(ValueError, match="status is missing"):
        perf.run(args)
    assert events == []


def test_replay_frozen_bytes_reach_every_probe(monkeypatch, tmp_path):
    bodies = make_source(tmp_path)
    events, args, _ = run_world(monkeypatch, tmp_path)
    args.family, args.replay_family = "fresh", "src"
    (tmp_path / "engine.log").write_text(
        "the prompt tier's channel (socket2) closed after 3 prompt requests / 9 prompt rows\n")
    monkeypatch.setattr(perf.lifecycle, "graceful_stop", lambda *_: events.append("arm_stop"))
    seen = []

    def probe(cfg, state, family, rep, body=None):
        seen.append(body)
        return dict(rep=rep, gen_rate=30, read_rate=200, request_sha256=perf.digest(body),
                    message_sha256=f"out{rep}", plans=[])
    monkeypatch.setattr(perf, "probe", probe)
    assert perf.run(args) == 0
    assert seen == [bodies[1], bodies[2], bodies[3]] * 3      # frozen source bytes, verbatim
    result = json.loads((tmp_path / "fresh-perf-status.json").read_text())
    assert result["replay"]["source"] == "src"
    assert result["replay"]["request_sha256"] == {str(r): perf.digest(bodies[r]) for r in (1, 2, 3)}
    assert all(probe["request_sha256"] == perf.digest(bodies[probe["rep"]])
               for arm in result["pairs"] for probe in arm["probes"])


def test_replay_source_mutation_during_window_is_detected(monkeypatch, tmp_path):
    bodies = make_source(tmp_path)
    events, args, _ = run_world(monkeypatch, tmp_path)
    args.family, args.replay_family = "fresh", "src"
    (tmp_path / "engine.log").write_text(
        "the prompt tier's channel (socket2) closed after 3 prompt requests / 9 prompt rows\n")
    monkeypatch.setattr(perf.lifecycle, "graceful_stop", lambda *_: events.append("arm_stop"))

    def probe(cfg, state, family, rep, body=None):
        # Mutate every arm's rep-1 source file identically (a valid fixture) and keep the
        # source status self-consistent, so load_replay passes its own checks and the
        # end-of-window source recheck catches the on-disk change.
        st = json.loads((tmp_path / "src-perf-status.json").read_text())
        req = json.loads((tmp_path / "src-a-r1-request.json").read_bytes())
        req["messages"][0]["content"] += " mutation"
        raw = json.dumps(req).encode()
        sha = perf.digest(raw)
        for pair in st["pairs"]:
            (tmp_path / f"src-{pair['label']}-r1-request.json").write_bytes(raw)
            for item in pair["probes"]:
                if item["rep"] == 1:
                    item["request_sha256"] = sha
        (tmp_path / "src-perf-status.json").write_text(json.dumps(st))
        return dict(rep=rep, gen_rate=30, read_rate=200, request_sha256=perf.digest(body),
                    message_sha256=f"out{rep}", plans=[])
    monkeypatch.setattr(perf, "probe", probe)
    with pytest.raises(RuntimeError, match="changed during measurement"):
        perf.run(args)
    assert events == ["stop", "arm_stop", "arm_stop", "arm_stop", "restore"]


def test_replay_status_only_mutation_detected_and_restore_runs(monkeypatch, tmp_path):
    make_source(tmp_path)
    events, args, _ = run_world(monkeypatch, tmp_path)
    args.family, args.replay_family = "fresh", "src"
    (tmp_path / "engine.log").write_text(
        "the prompt tier's channel (socket2) closed after 3 prompt requests / 9 prompt rows\n")
    monkeypatch.setattr(perf.lifecycle, "graceful_stop", lambda *_: events.append("arm_stop"))
    state = {"mutated": False}

    def probe(cfg, state_obj, family, rep, body=None):
        if not state["mutated"]:   # edit ONLY the status file, leaving the request bytes intact
            st = json.loads((tmp_path / "src-perf-status.json").read_text())
            st["comparison"] = {"tampered": True}
            (tmp_path / "src-perf-status.json").write_text(json.dumps(st))
            state["mutated"] = True
        return dict(rep=rep, gen_rate=30, read_rate=200, request_sha256=perf.digest(body),
                    message_sha256=f"out{rep}", plans=[])
    monkeypatch.setattr(perf, "probe", probe)
    with pytest.raises(RuntimeError, match="changed during measurement"):
        perf.run(args)
    assert events == ["stop", "arm_stop", "arm_stop", "arm_stop", "restore"]


def test_startup_prompt_f32_engagement_proof(tmp_path, monkeypatch):
    cfg = config_fixture()
    cfg["env"] = {"STRATA_HIP_PROMPT_F16": "0"}
    cfg["log"] = str(tmp_path / "engine.log")
    Path(cfg["log"]).write_text(STARTUP)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    helper = tmp_path / "helper.log"
    helper.write_text("fill placeholder")
    monkeypatch.setattr(perf.lifecycle, "fill_report", lambda _: dict(slots=4093))
    monkeypatch.setattr(perf, "engine_env", lambda pid: {"STRATA_HIP_PROMPT_F16": "0"})
    config = SimpleNamespace(config=path, tuning=None, prompt_f32=True)
    report = perf.startup_report(config, {"helper_log": str(helper), "engine_pid": 3})
    assert report["prompt_f32_engagement"]["private_env"] == "0"
    assert report["prompt_f32_engagement"]["engine_environ"]["STRATA_HIP_PROMPT_F16"] == "0"
    assert report["prompt_f32_engagement"]["fp16_prompt_line_absent"] is True


@pytest.mark.parametrize("env_value,live_value,fp16_line", [
    ("1", "0", False), ("0", "1", False), ("0", "0", True), (None, "0", False)])
def test_startup_prompt_f32_rejects_missing_engagement(tmp_path, monkeypatch, env_value, live_value, fp16_line):
    cfg = config_fixture()
    cfg["env"] = ({"STRATA_HIP_PROMPT_F16": env_value} if env_value is not None else {})
    cfg["log"] = str(tmp_path / "engine.log")
    Path(cfg["log"]).write_text(STARTUP + ("strata prefill: HIP device 0 gfx906 - STRATA_HIP_PROMPT_F16=1: "
                                           "the prompt's 16-bit GEMMs run FP16 in and out\n" if fp16_line else ""))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    helper = tmp_path / "helper.log"
    helper.write_text("fill placeholder")
    monkeypatch.setattr(perf.lifecycle, "fill_report", lambda _: dict(slots=4093))
    monkeypatch.setattr(perf, "engine_env", lambda pid: {"STRATA_HIP_PROMPT_F16": live_value})
    config = SimpleNamespace(config=path, tuning=None, prompt_f32=True)
    with pytest.raises(RuntimeError):
        perf.startup_report(config, {"helper_log": str(helper), "engine_pid": 3})


def test_startup_without_prompt_f32_is_byte_identical_proof(tmp_path, monkeypatch):
    cfg = config_fixture()
    cfg["env"] = {"STRATA_HIP_PROMPT_F16": "1"}
    cfg["log"] = str(tmp_path / "engine.log")
    Path(cfg["log"]).write_text(STARTUP)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    helper = tmp_path / "helper.log"
    helper.write_text("fill placeholder")
    monkeypatch.setattr(perf.lifecycle, "fill_report", lambda _: dict(slots=4093))

    def boom(_):
        raise AssertionError("the engine environ must not be read when the flag is off")
    monkeypatch.setattr(perf, "engine_env", boom)
    config = SimpleNamespace(config=path, tuning=None, prompt_f32=False)
    report = perf.startup_report(config, {"helper_log": str(helper)})
    assert report["prompt_f32_engagement"] is None


def test_fixture_enable_thinking_requires_false_identity():
    body = json.loads(perf.request_body("unit", 1))
    body["chat_template_kwargs"]["enable_thinking"] = 0
    with pytest.raises(ValueError, match="chat_template_kwargs"):
        perf.validate_fixture(json.dumps(body).encode())


def test_engine_env_reader_only_returns_narrow_keys():
    import os
    live = perf.engine_env(os.getpid())
    assert set(live).issubset(set(perf.ENGINE_ENV_KEYS))   # never dumps secrets


def test_validate_reply_stock_arm_helper_semantics():
    # A stock arm's serve lines carry no helper tail at all - that is the valid state.
    helper_less = dict(metric_fixture(), helper_reqs=None)
    assert perf.validate_reply(reply_fixture(), helper_less, stock=True)["completion_tokens"] == 512
    # A stock arm showing helper traffic is NOT the helper-less pre-helper product.
    with pytest.raises(RuntimeError, match="Invalid sustained"):
        perf.validate_reply(reply_fixture(), metric_fixture(), stock=True)
    # A mixed arm without helper traffic still fails, exactly as before.
    with pytest.raises(RuntimeError, match="Invalid sustained"):
        perf.validate_reply(reply_fixture(), helper_less)


STOCK_STARTUP = """strata generate: expert cache 6069 slots, 9.12 GiB
strata generate: layer split: CUDA1 runs layers 32-47, expert cache 4780 slots
strata generate: layer split: CUDA0 runs layers 0-31
dflash: trained block 8, pass K=4
strata prefill: HIP device 0 gfx906:sramecc+:xnack- - STRATA_HIP_PROMPT_F16=1: the prompt's 16-bit GEMMs run FP16 in and out
strata prefill: HIP device 1 gfx906:sramecc+:xnack- - STRATA_HIP_PROMPT_F16=1: the prompt's 16-bit GEMMs run FP16 in and out
"""


def stock_config_fixture(tmp_path, log_text=STOCK_STARTUP):
    cfg = dict(args=["--spec", "5", "--dflash-block", "5", "--spec-min-p", "0.5",
                     "--max-context", "262144", "--gpu", "0,1", "--prefill", "8192"],
               layer_split="32", exe=str(window_pair.STOCK_EXE))
    cfg["log"] = str(tmp_path / "engine.log")
    Path(cfg["log"]).write_text(log_text)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    return path, cfg


def test_stock_startup_report_proofs(tmp_path):
    path, _ = stock_config_fixture(tmp_path)
    console = tmp_path / "console.log"
    console.write_text("ready: http://127.0.0.1:18080/v1  (OpenAI: /v1/chat/completions, "
                       "Anthropic: /v1/messages, context 262144 tokens, images on)\n")
    report = perf.stock_startup_report(SimpleNamespace(config=path),
                                       {"engine_log": str(console)})
    assert report["kind"] == perf.STOCK_KIND and report["split"] == 32
    assert report["later_cache_slots"] == 4780 and report["effective_k"] == 4
    assert report["fp16_prompt_line"] is True and report["ready_context"] == 262144


@pytest.mark.parametrize("mutate,match", [
    (lambda s: s + "expert-helper: 4093 rows on /x.sock (mirrored 4093/4200 slots)\n",
     "helper-less"),
    (lambda s: s.replace("CUDA1 runs layers 32-47", "CUDA1 runs layers 30-47")
                .replace("CUDA0 runs layers 0-31", "CUDA0 runs layers 0-29"),
     "layer-split"),
])
def test_stock_startup_report_rejects_helper_and_geometry(tmp_path, mutate, match):
    path, cfg = stock_config_fixture(tmp_path)
    Path(cfg["log"]).write_text(mutate(STOCK_STARTUP))
    with pytest.raises(RuntimeError, match=match):
        perf.stock_startup_report(SimpleNamespace(config=path),
                                  {"engine_log": str(tmp_path / "missing.log")})


def test_stock_startup_report_rejects_wrong_ready_context(tmp_path):
    path, _ = stock_config_fixture(tmp_path)
    console = tmp_path / "console.log"
    console.write_text("ready: http://127.0.0.1:18080/v1  (context 131072 tokens)\n")
    with pytest.raises(RuntimeError, match="ready-line"):
        perf.stock_startup_report(SimpleNamespace(config=path),
                                  {"engine_log": str(console)})


def test_stock_startup_report_requires_fp16_parity_line(tmp_path):
    stripped = "".join(line + "\n" for line in STOCK_STARTUP.splitlines()
                       if "STRATA_HIP_PROMPT_F16" not in line)
    path, _ = stock_config_fixture(tmp_path, log_text=stripped)
    console = tmp_path / "console.log"
    console.write_text("ready: http://127.0.0.1:18080/v1  (context 262144 tokens)\n")
    with pytest.raises(RuntimeError, match="prompt GEMM line"):
        perf.stock_startup_report(SimpleNamespace(config=path),
                                  {"engine_log": str(console)})


STOCK_LAUNCHER_FIXTURE = (
    '#!/usr/bin/env bash\nset -euo pipefail\n'
    'ROOT=/mnt/user/Strata\nCLONE=/mnt/user/dflash2/Strata-integ\n'
    'PY="$ROOT/.venv/bin/python"\n'
    'CONFIG="$ROOT/prod-config-swift15-dflash-w4-q4_0.json"\n'
    'LOGDIR="$ROOT/logs"\n\n'
    'echo "checking port 8009"\n'
    'LOG="$LOGDIR/prod-swift15-dflash-w4-q4_0-$(date +%Y%m%d-%H%M%S)-$$.log"\n'
    'cd "$CLONE"\n'
    'exec env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \\\n'
    '  -u HSA_USERPTR_FOR_PAGED_MEM \\\n'
    '  LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib \\\n'
    '  setsid nohup "$PY" serve/server.py \\\n'
    '  --engine strata --config "$CONFIG" --port 8009 \\\n'
    '  >> "$LOG" 2>&1 < /dev/null\n')


def stock_sources(tmp_path):
    import os
    stock_dir = tmp_path / "stock"
    stock_dir.mkdir()
    exe = stock_dir / "strata"
    exe.write_bytes(b"#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    server = stock_dir / "server.py"
    server.write_text("# stock server\n")
    config = stock_dir / "stock-config.json"
    config.write_text(json.dumps(dict(
        exe=str(exe), layer_split="32",
        args=["--spec", "5", "--dflash-block", "5", "--spec-min-p", "0.5",
              "--max-context", "262144", "--gpu", "0,1", "--prefill", "8192"],
        env={"STRATA_HIP_PROMPT_F16": "1"})))
    launcher = stock_dir / "stock-launch.sh"
    launcher.write_text(STOCK_LAUNCHER_FIXTURE)
    return exe, server, config, launcher


def test_prepare_stock_derives_private_supervised_arm(tmp_path, monkeypatch):
    exe, server, config, launcher = stock_sources(tmp_path)
    monkeypatch.setattr(window_pair, "STOCK_EXE", exe)
    monkeypatch.setattr(window_pair, "STOCK_SERVER", server)
    monkeypatch.setattr(window_pair, "STOCK_CONFIG", config)
    monkeypatch.setattr(window_pair, "STOCK_LAUNCHER", launcher)
    arm = window_pair.prepare_stock("sab", port=18080, evidence=tmp_path)
    assert arm.kind == "stock" and arm.socket is None and arm.socket2 is None
    private = json.loads(Path(arm.config).read_text())
    assert private["exe"] == str(exe) and private["port"] == 18080
    assert private["args"] == json.loads(config.read_text())["args"]
    assert "STRATA_DUMP_FIRST_LOGITS" not in private.get("env", {})
    text = Path(arm.launcher).read_text()
    assert "setsid" not in text and str(arm.config) in text
    assert arm.state in text and 'record 0 "$server_pid"' in text
    assert 'wait "$server_pid"' in text and "18080" in text and "8009" not in text
    # the optional first-logits baseline rides the private config env
    arm2 = window_pair.prepare_stock("sac", port=18081, evidence=tmp_path, first_logits="/tmp/fl")
    assert json.loads(Path(arm2.config).read_text())["env"]["STRATA_DUMP_FIRST_LOGITS"] == "/tmp/fl"
    # a family is single-use
    with pytest.raises(ValueError, match="overwrite"):
        window_pair.prepare_stock("sab", evidence=tmp_path)


def test_prepare_stock_rejects_drifted_stock_product(tmp_path, monkeypatch):
    exe, server, config, launcher = stock_sources(tmp_path)
    monkeypatch.setattr(window_pair, "STOCK_EXE", exe)
    monkeypatch.setattr(window_pair, "STOCK_SERVER", server)
    monkeypatch.setattr(window_pair, "STOCK_CONFIG", config)
    monkeypatch.setattr(window_pair, "STOCK_LAUNCHER", launcher)
    drifted = json.loads(config.read_text())
    drifted["args"] += ["--expert-helper-slots", "4200"]
    config.write_text(json.dumps(drifted))
    with pytest.raises(ValueError, match="pre-helper product"):
        window_pair.prepare_stock("sab", evidence=tmp_path)
    drifted["args"] = drifted["args"][:-2]
    drifted["env"]["STRATA_PREFILL_HELPER"] = "1"
    config.write_text(json.dumps(drifted))
    with pytest.raises(ValueError, match="pre-helper product"):
        window_pair.prepare_stock("sbd", evidence=tmp_path)


def test_stock_controls_run_uses_stock_arms(monkeypatch, tmp_path):
    events, args, _ = run_world(monkeypatch, tmp_path)
    args.lever = None
    args.stock_controls = True
    engine_expected = []
    real_engine_of = perf.runtime.engine_of
    monkeypatch.setattr(perf.runtime, "engine_of",
                        lambda state, engine=None: engine_expected.append(engine) or 3)
    (tmp_path / "engine.log").write_text(
        "the prompt tier's channel (socket2) closed after 3 prompt requests / 9 prompt rows\n")
    monkeypatch.setattr(perf.lifecycle, "graceful_stop", lambda *_: events.append("arm_stop"))
    prepared_stock = []

    def prepare_stock(family, **kwargs):
        prepared_stock.append((family, kwargs.get("first_logits")))
        path = tmp_path / f"{family}.json"
        path.write_text(json.dumps(dict(log=str(tmp_path / "engine.log"))))
        return SimpleNamespace(family=family, config=path, launcher="launcher", state="state",
                               base_url="url", socket2=None, tuning=None)

    monkeypatch.setattr(perf.window_pair, "prepare_stock", prepare_stock)
    monkeypatch.setattr(perf, "stock_pins", lambda: {"stock": "same"})
    monkeypatch.setattr(perf, "stock_startup_report", lambda cfg, state: {"kind": "stock"})
    monkeypatch.setattr(perf, "probe", lambda cfg, state, family, rep, body=None: dict(
        rep=rep, gen_rate=30, read_rate=200, request_sha256=f"req{rep}",
        message_sha256=f"out{rep}", plans=[]))
    assert perf.run(args) == 0
    assert [family for family, _ in prepared_stock] == ["unit-a", "unit-c"]  # stock brackets
    assert engine_expected == [str(window_pair.STOCK_EXE), None, str(window_pair.STOCK_EXE)]
    result = json.loads((tmp_path / "unit-perf-status.json").read_text())
    assert result["stock_controls"] is True
    assert [row["kind"] for row in result["pairs"]] == ["stock", "mixed", "stock"]
    assert "product-level" in result["comparison"]["limits"]
    assert events == ["stop", "arm_stop", "arm_stop", "arm_stop", "restore"]


def test_stock_controls_refuses_lever_and_baselines(monkeypatch):
    base = ["performance_window", "--family", "x", "--expected-supervisor", "1",
            "--stock-controls"]
    for extra in (["--lever", "trim"], ["--candidate", "1"], ["--iq-mt-min"],
                  ["--adapt-off"], ["--prompt-f32"]):
        monkeypatch.setattr(sys, "argv", base + extra)
        with pytest.raises(SystemExit):
            perf.main()
    monkeypatch.setattr(sys, "argv", ["performance_window", "--family", "x",
                                      "--expected-supervisor", "1"])
    with pytest.raises(SystemExit):   # --lever is required without --stock-controls
        perf.main()


def test_helper_off_candidate_requires_stock_controls(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["performance_window", "--family", "x",
                                      "--expected-supervisor", "1",
                                      "--helper-off-candidate"])
    with pytest.raises(SystemExit):
        perf.main()


def test_validate_reply_helper_off_arm_helper_less_semantics():
    metric = metric_fixture()
    metric["helper_reqs"] = None
    with pytest.raises(RuntimeError, match="Invalid sustained"):
        perf.validate_reply(reply_fixture(), metric)                  # helper-full expected
    perf.validate_reply(reply_fixture(), metric, helper_off=True)     # helper-less accepted


def test_probe_helper_off_state_needs_no_helper_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(perf.runtime, "EV", tmp_path)
    log = tmp_path / "engine.log"
    log.write_text("serve summary line\n")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(dict(log=str(log))))
    raw = json.dumps(reply_fixture()).encode()

    class Resp:
        status = 200

        def read(self):
            return raw

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(perf.urllib.request, "urlopen", lambda *_, **__: (
        log.open("a").write("serve summary line\n"), Resp())[-1])
    helper_less = metric_fixture()
    helper_less["helper_reqs"] = None
    monkeypatch.setattr(perf.helper_ab, "parse_serve_line", lambda _: helper_less)
    config = SimpleNamespace(label="b", kind="helper_off", config=cfg, base_url="http://x")
    assert perf.probe(config, dict(helper_pid=0), "unit", 1)["rep"] == 1
    monkeypatch.setattr(perf.helper_ab, "parse_serve_line", lambda _: metric_fixture())
    mixed = SimpleNamespace(label="b", kind="mixed", config=cfg, base_url="http://x")
    with pytest.raises(RuntimeError, match="lacks helper identity"):
        perf.probe(mixed, dict(helper_pid=0), "unit", 1)


def helper_off_config_fixture(tmp_path, log_text=STOCK_STARTUP):
    cfg = dict(args=["--spec", "5", "--dflash-block", "5", "--spec-min-p", "0.5",
                     "--max-context", "262144", "--gpu", "0,1", "--prefill", "8192",
                     "--expert-helper-slots", "4200"],
               layer_split="32", exe="/mixed/strata",
               env={"STRATA_PREFILL_HELPER": "1", "STRATA_HIP_PROMPT_F16": "1"})
    cfg["log"] = str(tmp_path / "engine.log")
    Path(cfg["log"]).write_text(log_text)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    prod = tmp_path / "prod.json"
    prod.write_text(json.dumps(dict(exe="/mixed/strata")))
    return path, prod


def test_helper_off_startup_report_proofs(tmp_path, monkeypatch):
    path, prod = helper_off_config_fixture(tmp_path)
    monkeypatch.setattr(perf.runtime, "CONFIG", prod)
    console = tmp_path / "console.log"
    console.write_text("ready: http://127.0.0.1:18080/v1  (context 262144 tokens, images on)\n")
    report = perf.helper_off_startup_report(SimpleNamespace(config=path),
                                            {"engine_log": str(console)})
    assert report["kind"] == "helper_off" and report["split"] == 32
    assert report["first_cache_slots"] == 6069 and report["later_cache_slots"] == 4780
    assert report["effective_k"] == 4 and report["fp16_prompt_line"] is True
    assert report["ready_context"] == 262144
    # the engine-started banner echoing the inert --expert-helper-slots argv pair is
    # NOT a client line (iso2 evidence: the only match in a clean OFF arm)
    banner = ("[strata] 2026-11-03 12:12:33 engine started: /mixed/strata --serve --gpu 0,1 "
              "--expert-helper-slots 4200 --layer-split 32\n")
    Path(json.loads(path.read_text())["log"]).write_text(STOCK_STARTUP + banner)
    perf.helper_off_startup_report(SimpleNamespace(config=path),
                                   {"engine_log": str(console)})


@pytest.mark.parametrize("mutate_cfg,mutate_log,match", [
    (lambda c: c.update(exe=str(window_pair.STOCK_EXE)), None, "OFF derivation"),
    (lambda c: c["args"].append("--expert-helper-socket"), None, "OFF derivation"),
    (lambda c: c.update(expert_helper_socket="/x.sock"), None, "OFF derivation"),
    (lambda c: c.update(expert_helper_socket2="/x2.sock"), None, "OFF derivation"),
    (lambda c: c["env"].pop("STRATA_PREFILL_HELPER"), None, "OFF derivation"),
    (None, lambda s: s + "expert-helper: 4093 rows on /x.sock (mirrored 4093/4200 slots)\n",
     "expert-helper client"),
    (None, lambda s: s + "strata generate: expert-helper: 13727 of 24576 ranked pairs "
                       "eligible in layers 0..47\n", "expert-helper client"),
    (None, lambda s: s.replace("CUDA1 runs layers 32-47", "CUDA1 runs layers 30-47")
                      .replace("CUDA0 runs layers 0-31", "CUDA0 runs layers 0-29"), "layer-split"),
    (None, lambda s: "".join(line + "\n" for line in s.splitlines()
                             if "STRATA_HIP_PROMPT_F16" not in line), "prompt GEMM line"),
])
def test_helper_off_startup_report_rejects_seams_and_drift(tmp_path, monkeypatch,
                                                           mutate_cfg, mutate_log, match):
    path, prod = helper_off_config_fixture(tmp_path)
    if mutate_cfg:
        cfg = json.loads(path.read_text())
        mutate_cfg(cfg)
        path.write_text(json.dumps(cfg))
    if mutate_log:
        Path(json.loads(path.read_text())["log"]).write_text(mutate_log(STOCK_STARTUP))
    monkeypatch.setattr(perf.runtime, "CONFIG", prod)
    with pytest.raises(RuntimeError, match=match):
        perf.helper_off_startup_report(SimpleNamespace(config=path),
                                       {"engine_log": str(tmp_path / "missing.log")})


def test_helper_off_startup_report_rejects_wrong_ready_context(tmp_path, monkeypatch):
    path, prod = helper_off_config_fixture(tmp_path)
    monkeypatch.setattr(perf.runtime, "CONFIG", prod)
    console = tmp_path / "console.log"
    console.write_text("ready: http://127.0.0.1:18080/v1  (context 131072 tokens)\n")
    with pytest.raises(RuntimeError, match="ready-line"):
        perf.helper_off_startup_report(SimpleNamespace(config=path),
                                       {"engine_log": str(console)})


def test_helper_off_candidate_run_uses_off_arm(monkeypatch, tmp_path):
    events, args, _ = run_world(monkeypatch, tmp_path)
    args.lever = None
    args.stock_controls = True
    args.helper_off_candidate = True
    engine_expected = []
    monkeypatch.setattr(perf.runtime, "engine_of",
                        lambda state, engine=None: engine_expected.append(engine) or 3)
    monkeypatch.setattr(perf.lifecycle, "graceful_stop", lambda *_: events.append("arm_stop"))
    prepared_stock, startup_seen = [], []

    def prepare_stock(family, **kwargs):
        prepared_stock.append(family)
        path = tmp_path / f"{family}.json"
        path.write_text(json.dumps(dict(log=str(tmp_path / "engine.log"))))
        return SimpleNamespace(family=family, config=path, launcher="launcher", state="state",
                               base_url="url", socket2=None, tuning=None)

    def prepare(family, **kwargs):
        path = tmp_path / f"{family}.json"
        path.write_text(json.dumps(dict(log=str(tmp_path / "engine.log"))))
        return SimpleNamespace(family=family, config=path, launcher="launcher", state="state",
                               base_url="url", socket2="socket2", tuning=None,
                               helper_off=kwargs.get("helper_off", False))

    monkeypatch.setattr(perf.window_pair, "prepare_stock", prepare_stock)
    monkeypatch.setattr(perf.window_pair, "prepare", prepare)
    monkeypatch.setattr(perf, "stock_pins", lambda: {"stock": "same"})
    monkeypatch.setattr(perf, "stock_startup_report", lambda cfg, state: {"kind": "stock"})
    monkeypatch.setattr(perf, "helper_off_startup_report",
                        lambda cfg, state: startup_seen.append(cfg.label) or {"kind": "helper_off"})
    monkeypatch.setattr(perf, "probe", lambda cfg, state, family, rep, body=None: dict(
        rep=rep, gen_rate=30, read_rate=200, request_sha256=f"req{rep}",
        message_sha256=f"out{rep}", plans=[]))
    assert perf.run(args) == 0
    assert prepared_stock == ["unit-a", "unit-c"]              # stock brackets, helper-off middle
    assert startup_seen == ["b"]                               # the OFF arm ran its own proofs
    assert engine_expected == [str(window_pair.STOCK_EXE), None, str(window_pair.STOCK_EXE)]
    result = json.loads((tmp_path / "unit-perf-status.json").read_text())
    assert result["helper_off_candidate"] is True
    assert [row["kind"] for row in result["pairs"]] == ["stock", "helper_off", "stock"]
    assert "Helper-off candidate" in result["comparison"]["limits"]
    assert events == ["stop", "arm_stop", "arm_stop", "arm_stop", "restore"]


def test_stock_mode_kl_keys_follow_candidate_kind(monkeypatch, tmp_path):
    import struct
    # the plain-stock variant's middle arm is a HELPER-FULL mixed arm: it still demands
    # the prompt-channel close proof (the helper-off variant must not)
    (tmp_path / "engine.log").write_text(
        "the prompt tier's channel (socket2) closed after 3 prompt requests / 9 prompt rows\n")
    for family, helper_off in (("iso1", False), ("iso2", True)):
        events, args, _ = run_world(monkeypatch, tmp_path)
        args.lever = None
        args.stock_controls = True
        args.helper_off_candidate = helper_off
        args.first_logits = True
        args.family = family
        monkeypatch.setattr(perf.lifecycle, "graceful_stop", lambda *_: events.append("arm_stop"))

        def prepare_stock(family, **kwargs):
            path = tmp_path / f"{family}.json"
            path.write_text(json.dumps(dict(log=str(tmp_path / "engine.log"))))
            return SimpleNamespace(family=family, config=path, launcher="launcher", state="state",
                                   base_url="url", socket2=None, tuning=None)

        def prepare(family, **kwargs):
            path = tmp_path / f"{family}.json"
            path.write_text(json.dumps(dict(log=str(tmp_path / "engine.log"))))
            return SimpleNamespace(family=family, config=path, launcher="launcher", state="state",
                                   base_url="url", socket2="socket2", tuning=None,
                                   helper_off=kwargs.get("helper_off", False))

        def probe(cfg, state, family, rep, body=None):
            for n in range(3):
                Path(f"{tmp_path}/{family}-{cfg.label}-first-logits.{n}").write_bytes(
                    struct.pack("4f", 1.0, 2.0, 3.0, 4.0))
            return dict(rep=rep, gen_rate=30, read_rate=200, request_sha256=f"req{rep}",
                        message_sha256=f"out{rep}", plans=[])

        monkeypatch.setattr(perf.window_pair, "prepare_stock", prepare_stock)
        monkeypatch.setattr(perf.window_pair, "prepare", prepare)
        monkeypatch.setattr(perf, "probe", probe)
        monkeypatch.setattr(perf, "stock_pins", lambda: {"stock": "same"})
        monkeypatch.setattr(perf, "stock_startup_report", lambda cfg, state: {"kind": "stock"})
        monkeypatch.setattr(perf, "helper_off_startup_report", lambda cfg, state: {"kind": "helper_off"})
        assert perf.run(args) == 0
        kl = json.loads((tmp_path / f"{family}-perf-status.json").read_text())["first_logits_kl"]
        assert set(kl) == ({"stock_a_vs_stock_c", "stock_a_vs_helper_off_b",
                            "stock_c_vs_helper_off_b", "note"} if helper_off else
                           {"stock_a_vs_stock_c", "stock_a_vs_mixed_b",
                            "stock_c_vs_mixed_b", "note"})


def test_first_logits_flag_reaches_every_arm_and_kl(monkeypatch, tmp_path):
    import struct
    events, args, _ = run_world(monkeypatch, tmp_path)
    args.first_logits = True
    (tmp_path / "engine.log").write_text(
        "the prompt tier's channel (socket2) closed after 3 prompt requests / 9 prompt rows\n")
    monkeypatch.setattr(perf.lifecycle, "graceful_stop", lambda *_: events.append("arm_stop"))
    seen = []

    def prepare(family, **kwargs):
        seen.append((family, kwargs.get("first_logits")))
        path = tmp_path / f"{family}.json"
        path.write_text(json.dumps(dict(log=str(tmp_path / "engine.log"))))
        return SimpleNamespace(family=family, config=path, launcher="launcher", state="state",
                               base_url="url", socket2="socket2", tuning=kwargs.get("tuning"))

    def probe(cfg, state, family, rep, body=None):
        for n in range(3):   # one dump row per request, as generate.cpp appends them
            Path(f"{tmp_path}/unit-{cfg.label}-first-logits.{n}").write_bytes(
                struct.pack("4f", 1.0, 2.0, 3.0, 4.0))
        return dict(rep=rep, gen_rate=30, read_rate=200, request_sha256=f"req{rep}",
                    message_sha256=f"out{rep}", plans=[])

    monkeypatch.setattr(perf.window_pair, "prepare", prepare)
    monkeypatch.setattr(perf, "probe", probe)
    assert perf.run(args) == 0
    assert [family for family, _ in seen] == ["unit-a", "unit-b", "unit-c"]
    assert all(fl is not None for _, fl in seen)
    assert len({str(fl) for _, fl in seen}) == 3            # arm-private dump prefixes
    result = json.loads((tmp_path / "unit-perf-status.json").read_text())
    assert result["instrumented"] is True and result["stock_controls"] is False
    assert all(len(row["first_logits"]["rows"]) == 3 for row in result["pairs"])
    kl = result["first_logits_kl"]
    assert kl["control_a_vs_control_c"]["vocab"] == 4
    assert kl["control_a_vs_b"]["mean_kl_forward"] == pytest.approx(0.0, abs=1e-12)
    assert "never a gate" in kl["note"]


def test_softmax_kl_matches_naive_computation():
    import math
    p, q = [1.0, 2.0, 0.5, -0.25], [0.8, 2.2, 0.3, 0.0]

    def naive(a, b):
        ea = [math.exp(v - max(a)) for v in a]
        eb = [math.exp(v - max(b)) for v in b]
        sa, sb = sum(ea), sum(eb)
        return sum((x / sa) * math.log((x / sa) / (y / sb)) for x, y in zip(ea, eb))

    assert perf.softmax_kl(p, q) == pytest.approx(naive(p, q))
    assert perf.softmax_kl(p, p) == pytest.approx(0.0, abs=1e-15)
    with pytest.raises(ValueError):
        perf.softmax_kl([1.0], [1.0, 2.0])
    with pytest.raises(ValueError):
        perf.softmax_kl([], [])


def test_kl_report_rejects_vocabulary_mismatch(tmp_path):
    import struct
    a = tmp_path / "a.0"
    a.write_bytes(struct.pack("4f", 1, 2, 3, 4))
    b = tmp_path / "b.0"
    b.write_bytes(struct.pack("5f", 1, 2, 3, 4, 5))
    with pytest.raises(RuntimeError, match="vocabulary"):
        perf.kl_report(dict(vocab=4, rows=[dict(file=str(a), floats=4)]),
                       dict(vocab=5, rows=[dict(file=str(b), floats=5)]))


def test_first_logits_report_guards(tmp_path):
    import struct
    prefix = tmp_path / "fl"
    for n in range(3):
        Path(f"{prefix}.{n}").write_bytes(struct.pack("3f", 1.0, 2.0, 3.0))
    report = perf.first_logits_report(prefix, 3)
    assert report["vocab"] == 3 and len(report["rows"]) == 3
    assert all(row["sha256"] for row in report["rows"])
    Path(f"{prefix}.3").write_bytes(struct.pack("3f", 1.0, 2.0, 3.0))
    with pytest.raises(RuntimeError, match="more rows"):
        perf.first_logits_report(prefix, 3)   # a warmup would shift every index
    Path(f"{prefix}.0").unlink()
    with pytest.raises(RuntimeError, match="missing"):
        perf.first_logits_report(prefix, 3)

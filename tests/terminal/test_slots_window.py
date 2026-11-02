import json
from pathlib import Path
import signal
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/mixed_gpu"))
import slots_window


LOG = """expert-helper: filled 4116 slots (6.25 GiB), scratch 672000 B, buffers 179767896 B
expert-helper: fill stopped: fill-mib; device free now 1090 MiB of 7842 MiB (4116 slots, 6.25 GiB uploaded)
expert-helper: second-channel engine connected (peer uid 1000)
"""


def test_actual_fill_not_requested_slots():
    result = slots_window.fill_report(LOG)
    assert result == dict(slots=4116, gib_rounded=6.25, stop_reason="fill-mib",
                          device_free_mib=1090, device_total_mib=7842)


@pytest.mark.parametrize("text", ["", LOG.replace("(4116 slots,", "(4400 slots,"),
                                   LOG.replace("second-channel engine connected", "not attached")])
def test_bad_fill_proof_refused(text):
    with pytest.raises(ValueError):
        slots_window.fill_report(text)


def test_foreign_supervisor_refused_before_outage(monkeypatch):
    monkeypatch.setattr(slots_window.runtime, "command", lambda _pid: ["python", "unrelated.py"])
    result = {}
    with pytest.raises(RuntimeError, match="identity mismatch"):
        slots_window.stop_production(1234, result)
    assert "outage_started" not in result


class FakeProc:
    def __init__(self, pid):
        self.pid = pid


@pytest.mark.parametrize("candidate", [False, True])
def test_engine_identity_requires_exact_selected_binary(monkeypatch, candidate):
    rw = slots_window.runtime
    prod = rw.WORK / "build-mixed-gpu/rpcw-hip/strata"
    l3 = rw.WORK / "build-mixed-gpu/rpcw-hip-l3/strata"
    selected, other = (l3, prod) if candidate else (prod, l3)
    monkeypatch.setattr(rw.pair, "process_token", lambda pid: ("S", "start"))
    monkeypatch.setattr(rw.pair, "descendants", lambda root, known: {2: "a", 3: "b"})
    monkeypatch.setattr(rw.pair, "alive", lambda pid, start: True)
    monkeypatch.setattr(rw, "command", lambda pid: [str(selected if pid == 2 else other)])
    kwargs = {"engine": selected} if candidate else {}
    assert rw.engine_of({"launcher_pid": 1}, **kwargs) == 2
    monkeypatch.setattr(rw.pair, "descendants", lambda root, known: {3: "b"})
    with pytest.raises(RuntimeError, match="exactly one engine"):
        rw.engine_of({"launcher_pid": 1}, **kwargs)


def test_launcher_matches_absolute_argv(monkeypatch):
    rw = slots_window.runtime
    monkeypatch.setattr(rw, "command", lambda pid: ["bash", str(rw.LAUNCHER)])
    assert rw.launcher_matches(4242)


def test_launcher_matches_user_relative_argv(monkeypatch):
    rw = slots_window.runtime
    monkeypatch.setattr(rw, "command", lambda pid: ["bash", "./" + rw.LAUNCHER.name])
    monkeypatch.setattr(rw.os, "readlink",
                        lambda path: str(rw.LAUNCHER.parent) if path == "/proc/4242/cwd" else "")
    assert rw.launcher_matches(4242)


def test_launcher_rejects_same_name_wrong_directory(monkeypatch):
    rw = slots_window.runtime
    monkeypatch.setattr(rw, "command", lambda pid: ["bash", "./" + rw.LAUNCHER.name])
    monkeypatch.setattr(rw.os, "readlink", lambda path: "/tmp")
    assert not rw.launcher_matches(4242)


def test_launcher_rejects_when_cwd_unreadable(monkeypatch):
    rw = slots_window.runtime
    monkeypatch.setattr(rw, "command", lambda pid: ["bash", "./" + rw.LAUNCHER.name])

    def boom(path):
        raise OSError("process gone")

    monkeypatch.setattr(rw.os, "readlink", boom)
    assert not rw.launcher_matches(4242)


def test_launcher_matches_after_cd_via_exec_time_pwd(monkeypatch):
    # The canonical launcher `cd`s into the dflash2 integration tree after start:
    # live cwd no longer contains the script, the exec-time PWD still does.
    rw = slots_window.runtime
    monkeypatch.setattr(rw, "command", lambda pid: ["bash", "./" + rw.LAUNCHER.name])
    monkeypatch.setattr(rw, "launcher_dirs",
                        lambda pid: [Path("/mnt/user/dflash2/Strata-integ"), rw.LAUNCHER.parent])
    assert rw.launcher_matches(4242)


def test_launcher_rejects_when_no_dir_resolves(monkeypatch):
    rw = slots_window.runtime
    monkeypatch.setattr(rw, "command", lambda pid: ["bash", "./" + rw.LAUNCHER.name])
    monkeypatch.setattr(rw, "launcher_dirs", lambda pid: [Path("/mnt/user/dflash2/Strata-integ")])
    assert not rw.launcher_matches(4242)


def test_stop_production_direct_form_uses_stop_existing(monkeypatch):
    seen = {}

    def fake_stop_existing(server_pid, result):
        seen["server_pid"] = server_pid
        result["outage_started"] = True
        return {"server_pid": server_pid, "launcher_pid": 3148688,
                "processes": {2238464: "tok", 2238705: "tok", 2238470: "tok"}}

    monkeypatch.setattr(slots_window.runtime, "stop_existing", fake_stop_existing)
    result = {}
    slots_window.stop_production(None, result, server_pid=2238705)
    assert seen["server_pid"] == 2238705
    assert result["outage_started"] is True
    # The stop_existing process map must land in old_production so the
    # finally-restore gate can refuse a concurrent production start while
    # any owned process from the direct tree is still alive.
    assert result["old_production"]["form"] == "direct"
    assert result["old_production"]["processes"][2238464] == "tok"


def make_teardown_world(monkeypatch, engine_exits_at_s=None, engine_hangs=False):
    """Record signals/stop_group; fake clock; optional engine death mid-wait."""
    world = dict(kills=[], stops=[], scans=[], clock=0.0, live={555: "e-start"})
    monkeypatch.setattr(slots_window.os, "kill",
                        lambda pid, sig: world["kills"].append((pid, sig)))
    monkeypatch.setattr(slots_window.pair, "process_token",
                        lambda pid: ("S", world["live"][pid]) if pid in world["live"] else None)
    monkeypatch.setattr(slots_window.pair, "alive",
                        lambda pid, tok: world["live"].get(pid) == tok)
    monkeypatch.setattr(slots_window.pair, "descendants",
                        lambda root, known: (world["scans"].append(dict(known)), dict(known))[1])
    monkeypatch.setattr(slots_window.pair, "stop_group",
                        lambda child, timeout, known: world["stops"].append(
                            (child.pid, timeout, dict(known))))
    monkeypatch.setattr(slots_window.time, "monotonic", lambda: world["clock"])

    def sleep(seconds):
        world["clock"] += seconds
        if engine_exits_at_s is not None and world["clock"] >= engine_exits_at_s:
            world["live"].pop(555, None)

    monkeypatch.setattr(slots_window.time, "sleep", sleep)
    return world


def test_graceful_stop_terms_server_first_and_waits_for_engine(monkeypatch):
    world = make_teardown_world(monkeypatch, engine_exits_at_s=2.0)
    state = dict(server_pid=777, engine_pid=555)
    known = {100: "root"}
    slots_window.graceful_stop(FakeProc(100), state, known,
                               engine_wait_s=30, stop_timeout=7)
    # Only the server gets a direct signal — engines are never TERMed alongside it.
    assert world["kills"] == [(777, signal.SIGTERM)]
    # stop_group runs only after the engine's graceful exit (2 fake seconds in).
    assert world["stops"] == [(100, 7, dict(known))]
    assert world["clock"] == 2.0
    # Every refresh preserved the root seed: ownership is never dropped mid-wait.
    assert world["scans"] and all(scan.get(100) == "root" for scan in world["scans"])


def test_graceful_stop_without_engine_skips_the_wait(monkeypatch):
    world = make_teardown_world(monkeypatch)
    state = dict(server_pid=777)  # engine never identified (partial startup)
    slots_window.graceful_stop(FakeProc(100), state, {}, engine_wait_s=30)
    assert world["kills"] == [(777, signal.SIGTERM)]
    assert world["stops"] == [(100, 45, {})]
    assert world["scans"] == [] and world["clock"] == 0.0  # no wait loop at all


def test_graceful_stop_engine_hang_is_bounded(monkeypatch):
    world = make_teardown_world(monkeypatch, engine_hangs=True)  # engine stays alive
    state = dict(server_pid=777, engine_pid=555)
    slots_window.graceful_stop(FakeProc(100), state, {100: "root"},
                               engine_wait_s=5, stop_timeout=3)
    assert world["clock"] == 5.0              # deadline reached, loop exited
    assert world["stops"] == [(100, 3, {100: "root"})]  # bounded stop_group still ran
    assert world["kills"] == [(777, signal.SIGTERM)]    # engine itself never signalled


ENGAGED = "strata draft-helper: drafting on the helper: 5 planes x 2560 hidden"
DRAFT_COUNTS = "expert-helper: 999 windows served, 192 prompt row requests, 104800 draft context rows, 66 draft rounds; releasing the card"


def test_draft_service_proof():
    assert slots_window.draft_report(ENGAGED, DRAFT_COUNTS, True) == dict(
        enabled=True, context_rows=104800, rounds=66)
    assert slots_window.draft_report("", "expert-helper: 99 windows served", False) == dict(
        enabled=False, context_rows=0, rounds=0)


@pytest.mark.parametrize("engine,helper,enabled", [
    ("", DRAFT_COUNTS, True), (ENGAGED, "", True),
    (ENGAGED, DRAFT_COUNTS.replace("66 draft rounds", "0 draft rounds"), True),
    (ENGAGED, DRAFT_COUNTS + "\n" + DRAFT_COUNTS, True),
    (ENGAGED + "\nstrata draft-helper: the draft round at pos 9 failed (timeout)", DRAFT_COUNTS, True),
    (ENGAGED, DRAFT_COUNTS, False), ("", "expert-helper: dflash artifact", False)])
def test_bad_draft_service_refused(engine, helper, enabled):
    with pytest.raises(RuntimeError):
        slots_window.draft_report(engine, helper, enabled)


@pytest.mark.parametrize("remote,final,verdict", [
    (34.0, 30.0, "MEETS_LONG_DECODE_GATE"),
    (30.0, 30.0, "NO_LONG_DECODE_GAIN"),
    (34.0, 40.0, "INCONCLUSIVE_CONTROL_DRIFT")])
def test_draft_verdict_keeps_control_drift(remote, final, verdict):
    rows = [dict(label=label, draft_helper=enabled, generated_median=rate,
                 probes=[dict(gen_rate=rate, elapsed_s=60.0, accepted=50, offered=75)] * 3)
            for label, enabled, rate in [("a", False, 30.0), ("b", True, remote),
                                          ("c", True, remote), ("d", False, final)]]
    result = slots_window.draft_comparison(rows)
    assert result["verdict"] == verdict
    assert result["remote_decode_median"] == remote
    assert result["remote_acceptance"] == pytest.approx(2 / 3)


def probe_world(monkeypatch, tmp_path, failure=""):
    rw = slots_window.runtime
    monkeypatch.setattr(rw, "EV", tmp_path)
    engine = tmp_path / "engine.log"
    engine.write_text("")
    config = tmp_path / "config.json"
    config.write_text(json.dumps(dict(log=str(engine))))
    for size in ("long", "xl"):
        (tmp_path / f"ab2-req-on-{size}-r1.json").write_text(json.dumps(
            dict(model="old", messages=[dict(role="user", content=f"nonce\n{size} prompt")],
                 max_tokens=32, temperature=0)))
    metric = dict(generated=128, reused=0, helper_reqs=100, helper_timeouts=0,
                  helper_fallbacks=0, offered=90, accepted=60, gen_rate=30.0)
    monkeypatch.setattr(rw.helper_ab, "parse_serve_line", lambda line: metric if line == "METRIC" else None)
    monkeypatch.setattr(rw.pair, "alive", lambda *_: True)
    requests = []

    class Reply:
        status = 200

        def __enter__(self):
            with engine.open("a") as log:
                log.write("METRIC\n" + failure)
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps(dict(usage=dict(completion_tokens=128), choices=[dict(
                finish_reason="length")])).encode()

    def urlopen(request, timeout):
        requests.append(request.data)
        return Reply()

    monkeypatch.setattr(rw.urllib.request, "urlopen", urlopen)
    return rw, config, requests


@pytest.mark.parametrize("size", ["long", "xl"])
def test_draft_probe_matched_bytes(monkeypatch, tmp_path, size):
    rw, config, requests = probe_world(monkeypatch, tmp_path)
    options = dict(config_path=config, family="unit", size=size, max_tokens=128,
                   reasoning_budget_tokens=0, enable_thinking=False, require_remote_draft=True)
    state = dict(helper_pid=10, helper_start="start")
    a = rw.probe("a", 1, state, **options)
    b = rw.probe("b", 1, state, **options)
    assert requests[0] == requests[1]
    assert a["request_sha256"] == b["request_sha256"]
    body = json.loads(requests[0])
    assert body["max_tokens"] == 128 and body["reasoning_budget_tokens"] == 0
    assert body["chat_template_kwargs"] == dict(enable_thinking=False)
    assert body["messages"][0]["content"].endswith(f"{size} prompt")
    if size == "xl":
        assert "size=xl" in body["messages"][0]["content"].split("\n", 1)[0]  # no parked long-prefix reuse
    tag = "a" if size == "long" else "a-xl"
    assert (tmp_path / f"unit-{tag}-r1-reply.json").exists()


def test_draft_probe_refuses_silent_fallback(monkeypatch, tmp_path):
    rw, config, _ = probe_world(monkeypatch, tmp_path,
                               "strata draft-helper: context at pos 0 failed (timeout)\n")
    with pytest.raises(RuntimeError, match="did not remain active"):
        rw.probe("b", 1, dict(helper_pid=10, helper_start="start"),
                 config_path=config, family="unit", require_remote_draft=True)
    assert (tmp_path / "unit-b-r1-reply.json").exists()
    assert (tmp_path / "unit-b-r1-engine.log").exists()


def test_default_probe_request_unchanged(monkeypatch, tmp_path):
    rw, config, requests = probe_world(monkeypatch, tmp_path)
    rw.probe("a", 1, dict(helper_pid=10, helper_start="start"), config_path=config, family="unit")
    assert json.loads(requests[0]) == dict(
        model="swift-1.5-iq3_xxs", max_tokens=32, temperature=0,
        messages=[dict(role="user", content="Pair recovery check rep=1, matched before and after.\nlong prompt")])


def test_probe_still_refuses_multiple_engine_generations(monkeypatch, tmp_path):
    rw, config, _ = probe_world(monkeypatch, tmp_path, "METRIC\n")
    with pytest.raises(RuntimeError, match="Expected one serve line, got 2"):
        rw.probe("a", 1, dict(helper_pid=10, helper_start="start"), config_path=config,
                 family="unit", reasoning_budget_tokens=0, enable_thinking=False)
    assert (tmp_path / "unit-a-r1-engine.log").read_text() == "METRIC\nMETRIC\n"


# ---- L7 degrade-probe samplers ----


def test_sample_process_parses_comm_with_spaces(tmp_path):
    tail = ["R", "1", "2", "3", "4", "5", "6", "100", "0", "5", "0", "200", "300", "0"]
    d = tmp_path / "proc" / "42"
    d.mkdir(parents=True)
    (d / "stat").write_text("42 (strata serve) " + " ".join(tail) + "\n")
    (d / "statm").write_text("1 1024 512 4 0 0 0\n")
    assert slots_window.sample_process(42, proc=str(tmp_path / "proc")) == dict(
        minflt=100, majflt=5, cpu_ticks=500, resident_mib=4.0)


def test_sample_process_returns_none_when_gone(tmp_path):
    assert slots_window.sample_process(999, proc=str(tmp_path)) is None


def test_sample_system_missing_files_stay_none(tmp_path):
    assert slots_window.sample_system(proc=str(tmp_path), sysfs=str(tmp_path)) == dict(
        mem_available_mib=None, psi_memory_avg10=None, gpus={})


def test_degrade_summary_deltas():
    def snap(maj, cpu, res, mem):
        return dict(engine=dict(minflt=10, majflt=maj, cpu_ticks=cpu, resident_mib=res),
                    system=dict(mem_available_mib=mem, gpus={"card2": {"temp_c": 70.0}}))
    probes = [dict(rep=1, read_ms=1000.0, read_rate=200.0, gen_rate=25.0, elapsed_s=10.0),
              dict(rep=2, read_ms=2000.0, read_rate=100.0, gen_rate=25.0, elapsed_s=20.0)]
    samples = [snap(10, 1000, 100.0, 50000), snap(15, 3000, 104.0, 49000),
               snap(15, 3000, 104.0, 49000), snap(30, 5000, 110.0, 47000)]
    rows = slots_window.degrade_summary(probes, samples)
    assert rows[0]["engine_majflt"] == 5 and rows[1]["engine_majflt"] == 15
    assert rows[0]["engine_minflt"] == 0
    assert rows[0]["engine_cpu_s"] == pytest.approx(2000 / slots_window.CLK_TCK)
    assert rows[1]["engine_resident_growth_mib"] == 6.0
    assert rows[0]["mem_available_drop_mib"] == 1000
    assert rows[1]["mem_available_drop_mib"] == 2000
    assert rows[1]["gpu_post"]["card2"]["temp_c"] == 70.0
    assert rows[0]["read_rate"] == 200.0 and rows[1]["read_rate"] == 100.0


def _knob_rows(off_rates, on_rates, read_ms=None):
    read_ms = read_ms or {}

    def arm(label, rates, on):
        probes = [dict(rep=i + 1, gen_rate=rate, elapsed_s=1.0,
                       read_ms=read_ms.get((label, i + 1), 50000.0))
                  for i, rate in enumerate(rates)]
        return dict(label=label, knob_on=on, probes=probes)

    return [arm("a", off_rates[:3], False), arm("b", on_rates, True),
            arm("c", off_rates[3:], False)]


def test_knob_comparison_candidate_with_slow_fraction():
    rows = _knob_rows([30.0, 30.0, 30.0, 30.0, 30.0, 30.0], [33.0, 33.0, 33.0],
                      read_ms={("b", 1): 200000.0, ("b", 2): 300000.0})
    out = slots_window.knob_comparison(rows)
    assert out["verdict"] == "KNOB_CANDIDATE"
    assert out["on_off_decode_ratio"] == pytest.approx(1.1)
    assert out["control_drift"] == pytest.approx(1.0)
    assert out["off_slow_prefill_fraction"] == 0.0
    assert out["on_slow_prefill_fraction"] == pytest.approx(2 / 3)
    assert out["off_read_ms_median"] == 50000.0


def test_knob_comparison_no_gain_and_drift_gate():
    rows = _knob_rows([30.0, 30.0, 30.0, 30.0, 30.0, 30.0], [31.0, 31.0, 31.0])
    assert slots_window.knob_comparison(rows)["verdict"] == "KNOB_NO_GAIN"
    rows = _knob_rows([30.0, 30.0, 30.0, 22.0, 22.0, 22.0], [33.0, 33.0, 33.0])
    assert slots_window.knob_comparison(rows)["verdict"] == "INCONCLUSIVE_CONTROL_DRIFT"


# ---- L2 helper-stream-split comparison ----


def _l2_rows(off_rates, on_rates):
    def arm(label, rates, on):
        probes = [dict(rep=i + 1, gen_rate=rate, elapsed_s=1.0, read_ms=50000.0)
                  for i, rate in enumerate(rates)]
        return dict(label=label, l2_streams=on, probes=probes)

    return [arm("a", off_rates[:3], False), arm("b", on_rates, True),
            arm("c", off_rates[3:], False)]


def test_l2_comparison_verdicts():
    rows = _l2_rows([30.0] * 6, [33.0] * 3)
    out = slots_window.l2_comparison(rows)
    assert out["verdict"] == "L2_CANDIDATE"
    assert out["on_off_decode_ratio"] == pytest.approx(1.1)
    assert out["control_drift"] == pytest.approx(1.0)
    assert slots_window.l2_comparison(_l2_rows([30.0] * 6, [31.0] * 3))["verdict"] == "L2_NO_GAIN"
    drift = slots_window.l2_comparison(_l2_rows([30.0] * 3 + [22.0] * 3, [33.0] * 3))
    assert drift["verdict"] == "INCONCLUSIVE_CONTROL_DRIFT"


# ---- L8 bf16 tap-wire comparison ----


def _l8_rows(local_rates, f32_rates, bf16_rates, *, bf16_acc=None):
    def arm(label, rates, on, bf16, acc):
        probes = [dict(rep=i + 1, gen_rate=rate, elapsed_s=1.0, read_ms=50000.0,
                       offered=100, accepted=(acc if acc is not None else 43))
                  for i, rate in enumerate(rates)]
        return dict(label=label, draft_helper=on, bf16_taps=bf16, probes=probes)

    return [arm("a", local_rates, False, False, None), arm("b", f32_rates, True, False, None),
            arm("c", bf16_rates, True, True, bf16_acc), arm("d", local_rates, False, False, None)]


def test_l8_wire_comparison_gain_guard_and_drift():
    rows = _l8_rows([30.0] * 3, [28.0] * 3, [30.0] * 3)
    out = slots_window.l8_wire_comparison(rows)
    assert out["verdict"] == "L8_WIRE_GAIN"
    assert out["bf16_f32_decode_ratio"] == pytest.approx(30.0 / 28.0)
    assert out["control_drift"] == pytest.approx(1.0)
    assert out["bf16_acceptance"] == pytest.approx(0.43)
    # acceptance drops more than 2 points: no adoption even with the decode gain
    worse = _l8_rows([30.0] * 3, [28.0] * 3, [30.0] * 3, bf16_acc=38)
    assert slots_window.l8_wire_comparison(worse)["verdict"] == "L8_NO_GAIN"
    # no decode gain
    flat = _l8_rows([30.0] * 3, [28.0] * 3, [28.5] * 3)
    assert slots_window.l8_wire_comparison(flat)["verdict"] == "L8_NO_GAIN"
    # control drift outside 0.9-1.1
    drift_rows = _l8_rows([30.0] * 3, [28.0] * 3, [30.0] * 3)
    drift_rows[3]["probes"] = [dict(p, gen_rate=22.0) for p in drift_rows[3]["probes"]]
    assert slots_window.l8_wire_comparison(drift_rows)["verdict"] == "INCONCLUSIVE_CONTROL_DRIFT"


# ---- split-overlap comparison ----


def _overlap_rows(off_rates, on_rates):
    def arm(label, rates, on):
        probes = [dict(rep=i + 1, gen_rate=rate, elapsed_s=1.0, read_ms=50000.0)
                  for i, rate in enumerate(rates)]
        return dict(label=label, split_overlap=on, probes=probes)

    return [arm("a", off_rates[:3], False), arm("b", on_rates, True),
            arm("c", off_rates[3:], False)]


def test_overlap_comparison_verdicts():
    rows = _overlap_rows([30.0] * 6, [33.0] * 3)
    out = slots_window.overlap_comparison(rows)
    assert out["verdict"] == "OVERLAP_CANDIDATE"
    assert out["on_off_decode_ratio"] == pytest.approx(1.1)
    assert out["control_drift"] == pytest.approx(1.0)
    assert slots_window.overlap_comparison(_overlap_rows([30.0] * 6, [31.0] * 3))["verdict"] == "OVERLAP_NO_GAIN"
    drift = slots_window.overlap_comparison(_overlap_rows([30.0] * 3 + [22.0] * 3, [33.0] * 3))
    assert drift["verdict"] == "INCONCLUSIVE_CONTROL_DRIFT"


# ---- helper-off comparison ----


def _helper_off_rows(off_rates, on_rate, *, off_reqs=0, on_reqs=250):
    def arm(label, rates, on, reqs):
        probes = [dict(rep=i + 1, gen_rate=r, read_rate=200.0, elapsed_s=45.0,
                       read_ms=44000.0, helper_reqs=reqs) for i, r in enumerate(rates)]
        return dict(label=label, helper_off=on, probes=probes)

    return [arm("a", off_rates[:3], True, off_reqs),
            arm("b", [on_rate] * 3, False, on_reqs),
            arm("c", off_rates[3:], True, off_reqs)]


def test_helper_off_comparison_verdicts():
    assert slots_window.helper_off_comparison(
        _helper_off_rows([28.0] * 6, 33.0))["verdict"] == "HELPER_GAIN_CONFIRMED"
    assert slots_window.helper_off_comparison(
        _helper_off_rows([32.0] * 6, 33.0))["verdict"] == "HELPER_GAIN_SMALL"
    assert slots_window.helper_off_comparison(
        _helper_off_rows([34.0] * 6, 33.0))["verdict"] == "HELPER_SLOWER"
    assert slots_window.helper_off_comparison(
        _helper_off_rows([28.0] * 6, 33.0, off_reqs=5))["verdict"] == "HELPER_ENGAGEMENT_BROKEN"
    assert slots_window.helper_off_comparison(
        _helper_off_rows([28.0] * 6, 33.0, on_reqs=0))["verdict"] == "HELPER_ENGAGEMENT_BROKEN"
    assert slots_window.helper_off_comparison(
        _helper_off_rows([30.0] * 3 + [22.0] * 3, 33.0))["verdict"] == "INCONCLUSIVE_CONTROL_DRIFT"

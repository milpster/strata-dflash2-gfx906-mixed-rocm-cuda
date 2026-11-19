#!/usr/bin/env python3
"""CPU/fake-only tests for the offline raw-token lifecycle adapter. No GPU, no socket,
no engine, no server import.

  python3 tests/test_ram_raw_cpu.py        # stdlib only
The adapter is exercised through injected fakes (helper + engine factory + clock). The
real `serve.server` import and the destructive spawn path must NEVER be reached here;
several tests assert that directly. Fixtures live under the project-local, git-ignored
out/tests-tmp; the real benchmark/cache/prod trees are untouched.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# pyright: reportMissingImports=false
RPC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RPC))
sys.path.insert(0, str(RPC / "gpu-rounds"))
import ram_acceptance as RA  # noqa: E402  sibling helper the adapter reuses
import ram_raw as RR  # noqa: E402  the module under test

PROD_CONFIG = Path("/mnt/srcds/Strata/prod-config-v0238.json")
SCRATCH = RPC / "out" / "tests-tmp"


def setUpModule():  # guard real process spawning for the whole module run
    global _POPEN_CALLS, _REAL_POPEN
    _POPEN_CALLS, _REAL_POPEN = [], subprocess.Popen
    subprocess.Popen = _refuse_spawn  # pyright: ignore[assignment]


def tearDownModule():
    subprocess.Popen = _REAL_POPEN  # pyright: ignore[assignment]


def _refuse_spawn(*_a, **_k):
    _POPEN_CALLS.append(1)
    raise AssertionError("no real process may spawn in offline tests")


def fresh_scratch(case: unittest.TestCase) -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=SCRATCH))
    case.addCleanup(shutil.rmtree, root, True)
    return root


def base_config() -> dict:
    return json.loads(PROD_CONFIG.read_text())


def plan_args(out: Path, arm: str = "candidate") -> dict:
    return dict(
        base_cfg=base_config(), arm=arm, out_dir=out,
        exe=str(out / "slots" / arm / "rpcw-hip" / "strata"),
        socket1=str(out / "run" / f"{arm}.sock"), socket2=str(out / "run" / f"{arm}2.sock"),
        cache_dir=str(out / "cache" / arm), engine_log=str(out / "logs" / f"{arm}.engine.log"),
        helper_bin=str(out / "slots" / arm / "rpcw-cuda" / "strata-expert-helper-patient"),
        pack="/mnt/srcds/ai/iq3xxs-ud",
        experts="/mnt/srcds/ai/Qwen3.8-Flash-Next-UD-IQ3_XXS-00001-of-00004.gguf",
        supplied_files={})


# ---- state-hash engine log fixtures (format verified against generate.cpp) ------

def state_line(**over) -> str:
    vals = {"L": "1a", "gdn": "2b", "ple": "3c", "tail": "4d", "dead": "0",
            "pooled": "5e", "pooled_full": "6f", "kv": "7a", "ple_prev": "8b"}
    vals.update(over)
    return "strata serve: STATE_HASH " + " ".join(f"{k}={v}" for k, v in vals.items())

GOOD_STATE = state_line()


# ---- fakes: helper + engine (never a real process) ------------------------------

class FakeHelper:
    def __init__(self, ready: bool = True, alive: bool = True):
        self._ready, self._alive = ready, alive
        self.calls: list[str] = []

    def launch(self):
        self.calls.append("launch")

    def ready(self):
        return self._ready

    def alive(self):
        return self._alive

    def terminate(self):
        self.calls.append("terminate")

    def wait(self):
        self.calls.append("wait")


class FakeEngine:
    def __init__(self, tokens, last, info=None, log_text="", raise_exc=None):
        self._tokens, self.last, self.info, self._log, self._raise = tokens, last, info or {}, log_text, raise_exc
        self.closed = False

    def generate(self, ids, max_new, sampling, cancel):
        if self._raise:
            raise self._raise
        yield from self._tokens

    def state_log_text(self):
        return self._log

    def close(self):
        self.closed = True


class CountingFactory:
    def __init__(self, engine=None, raise_exc=None):
        self.calls = 0
        self._engine, self._raise = engine, raise_exc

    def __call__(self, exe, args, env, log):
        self.calls += 1
        if self._raise:
            raise self._raise
        return self._engine


class FakeClock:
    def __init__(self, values):
        self.values, self.i = list(values), 0

    def __call__(self):
        if self.i < len(self.values):
            v = self.values[self.i]
            self.i += 1
            return v
        return self.values[-1] if self.values else 0.0


def complete_last(**over) -> dict:
    last = {"finish": "eos", "generated": 3, "prompt_ms": 10.0, "decode_ms": 20.0}
    last.update(over)
    return last


def fixture(**over) -> RR.PairFixture:
    base = dict(ids=[100, 101, 102], max_new=8, sampling={"min_p": 0.5, "seed": 7})
    base.update(over)
    return RR.PairFixture(**base)


def good_result_tokens() -> list[int]:
    return [10, 11, 12]


# ---- safety: import/CLI paths touch nothing ------------------------------------

class ImportAndCliIsInactive(unittest.TestCase):
    def test_import_never_loads_server(self):
        self.assertNotIn("serve", sys.modules)
        self.assertNotIn("serve.server", sys.modules)

    def test_default_and_check_env_and_execute_refusal_launch_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(RR.main([]), 0)                      # default: inactive
            RR.main(["check-env"])                                 # reports, launches nothing
            with self.assertRaises(RR.AdapterBlocked):
                RR.main(["execute", "--confirm-production-down"])   # reserved; refuses
        self.assertNotIn("serve", sys.modules)
        self.assertNotIn("serve.server", sys.modules)
        self.assertEqual(_POPEN_CALLS, [])

    def test_execute_gate_blocks_without_prod_absence(self):
        # confirm + import env present, but a live production engine identity is seen.
        with self.assertRaises(RR.AdapterBlocked):
            RR.assert_execute_allowed(confirm=True, env={"STRATA_ENGINE_READY_S": "2400"},
                                      probes={"prod_engine_serving": lambda: 4242})
        self.assertEqual(_POPEN_CALLS, [])

    def test_execute_gate_blocks_missing_import_env(self):
        with self.assertRaises(RR.AdapterBlocked):
            RR.assert_execute_allowed(confirm=True, env={}, probes={})

    def test_execute_gate_blocks_without_confirmation(self):
        with self.assertRaises(RR.AdapterBlocked):
            RR.assert_execute_allowed(confirm=False, env={"STRATA_ENGINE_READY_S": "2400"}, probes={})

    def test_execute_gate_passes_when_absent(self):
        RR.assert_execute_allowed(confirm=True, env={"STRATA_ENGINE_READY_S": "2400"},
                                  probes={"prod_engine_serving": lambda: 0, "prod_socket": lambda: False})

    def test_production_absent_treats_probe_error_as_present(self):
        def boom():
            raise OSError("proc unreadable")
        absent, reasons = RR.production_absent({"x": boom})
        self.assertFalse(absent)
        self.assertTrue(any("assume present" in r for r in reasons))


# ---- A. config / argv / env / provenance construction --------------------------

class PlanAndArgv(unittest.TestCase):
    def test_prepare_builds_canonical_tuple(self):
        out = fresh_scratch(self)
        plan = RR.prepare_plan(**plan_args(out))
        self.assertEqual(plan["status"], "PREPARED")
        self.assertEqual(plan["gpu"], RR.GPU_STATUS)
        self.assertTrue(plan["dry_run_only"])
        argv = plan["expected_engine_argv"]
        self.assertNotIn("--serve", argv)
        self.assertIn("--layer-split", argv)
        self.assertEqual(RA._flag_value(argv, "--layer-split"), "30")
        self.assertEqual(RA._flag_value(argv, "--expert-helper-socket"), plan["test_config"]["expert_helper_socket"])
        self.assertEqual(RA._flag_value(argv, "--expert-helper-socket2"), plan["test_config"]["expert_helper_socket2"])
        self.assertNotEqual(plan["test_config"]["expert_helper_socket"],
                            plan["test_config"]["expert_helper_socket2"])
        self.assertEqual(RA.min_free_floor_mib(plan["test_config"]), RA.FLOOR_MIB)
        # helper command keeps canonical device/slots/fill and binds two distinct sockets.
        hargv = plan["expected_helper_argv"]
        self.assertEqual(RA._flag_value(hargv, "--slots"), "4600")
        self.assertEqual(RA._flag_value(hargv, "--fill-mib"), "7100")
        self.assertNotEqual(RA._flag_value(hargv, "--socket"), RA._flag_value(hargv, "--socket2"))
        self.assertIn("STRATA_HELPER_REPLY_TIMEOUT_MS=600000", plan["expected_helper_env"])
        self.assertTrue(RA.manifest_is_public(plan["provenance"]))
        self.assertEqual(_POPEN_CALLS, [])

    def test_prepare_rejects_non_floor(self):
        out = fresh_scratch(self)
        base = base_config()
        base["args"] = base["args"] + ["--conversation-cache-min-free-mib", "1280"]
        with self.assertRaises(RA.ConfigRejected):
            RR.prepare_plan(**{**plan_args(out), "base_cfg": base})

    def test_build_engine_argv_rejects_serve_in_config(self):
        cfg = {"args": ["--serve", "--conversation-cache-dir", "/x"], "gpu": ["0", "1"], "layer_split": 30}
        with self.assertRaises(RA.ConfigRejected):
            RR.build_engine_argv(cfg, "/exe/strata")

    def test_env_precedence_config_env_wins(self):
        cfg = {"env": {"STRATA_STATE_HASH": "1", "HSA_OVERRIDE_GFX_VERSION": "9.0.6"}}
        merged = RR.plan_engine_env_precedence(cfg, {"STRATA_STATE_HASH": "0", "PATH": "/keep"})
        self.assertEqual(merged["STRATA_STATE_HASH"], "1")     # config.env overrides launcher base
        self.assertEqual(merged["HSA_OVERRIDE_GFX_VERSION"], "9.0.6")
        self.assertEqual(merged["PATH"], "/keep")             # inherited base preserved

    def test_import_time_env_ok(self):
        self.assertEqual(RR.import_time_env_ok({}), (False, ["STRATA_ENGINE_READY_S"]))
        self.assertEqual(RR.import_time_env_ok({"STRATA_ENGINE_READY_S": "2400"}), (True, []))

    def test_cli_prepare_manifest_writes_plan(self):
        out = fresh_scratch(self)
        a = plan_args(out)
        manifest = {k: (str(v) if isinstance(v, Path) else v) for k, v in a.items() if k != "base_cfg"}
        manifest["base_config"] = str(PROD_CONFIG)
        mpath = out / "manifest.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(RR.main(["prepare", "--manifest", str(mpath), "--write"]), 0)
        self.assertTrue((Path(a["out_dir"]) / "ram_raw_plan.json").exists())
        with contextlib.redirect_stdout(buf):
            self.assertEqual(RR.main(["helper-command", "--manifest", str(mpath)]), 0)
        self.assertEqual(_POPEN_CALLS, [])
        self.assertNotIn("serve", sys.modules)


# ---- argv reconciliation + state hashing ---------------------------------------

class VerifyAndHash(unittest.TestCase):
    def test_verify_observed_argv_accepts_canonical(self):
        expected = ["--engine-x", "--layer-split", "30"]
        observed = ["/exe/strata", "--serve", "--engine-x", "--layer-split", "30"]
        self.assertEqual(RR.verify_observed_argv("/exe/strata", expected, observed), [])

    def test_verify_observed_argv_rejects_double_serve(self):
        expected = ["--layer-split", "30"]
        observed = ["/exe/strata", "--serve", "--serve", "--layer-split", "30"]
        self.assertTrue(RR.verify_observed_argv("/exe/strata", expected, observed))

    def test_verify_observed_argv_rejects_extra_fallback(self):
        expected = ["--layer-split", "30"]
        observed = ["/exe/strata", "--serve", "--layer-split", "30", "--mtp"]
        self.assertTrue(RR.verify_observed_argv("/exe/strata", expected, observed))

    def test_parse_state_hashes_full_present(self):
        got = RR.parse_state_hashes(GOOD_STATE, RR.REQUIRED_STATE_KEYS)
        self.assertEqual(set(got), set(RR.REQUIRED_STATE_KEYS))

    def test_parse_state_hashes_missing_key_returns_empty(self):
        line = GOOD_STATE.replace("kv=7a", "")
        self.assertEqual(RR.parse_state_hashes(line, RR.REQUIRED_STATE_KEYS), {})

    def test_parse_state_hashes_no_line_returns_empty(self):
        self.assertEqual(RR.parse_state_hashes("nothing here", RR.REQUIRED_STATE_KEYS), {})

    def test_argv_drift_flags(self):
        self.assertEqual(RR._argv_drift(["--layer-split", "30"]), [])
        self.assertTrue(RR._argv_drift(["--mtp"]))
        self.assertTrue(RR._argv_drift(["--serve"]))


# ---- B/C. run_pair lifecycle via fakes -----------------------------------------

class RunPairLifecycle(unittest.TestCase):
    def _engine(self, tokens=None, last=None, log=None):
        return FakeEngine(tokens if tokens is not None else good_result_tokens(),
                          last if last is not None else complete_last(),
                          log_text=GOOD_STATE if log is None else log)

    def test_happy_single_pair_order(self):
        helper, events = FakeHelper(), []
        engine = self._engine()
        factory = CountingFactory(engine=engine)
        fx = fixture(expected_output_ids=good_result_tokens(),
                     expected_state=RR.parse_state_hashes(GOOD_STATE, RR.REQUIRED_STATE_KEYS))
        res = RR.run_pair(exe="/exe", args=["--layer-split", "30"], env={}, log="x.log",
                          fixture=fx, engine_factory=factory, helper=helper, events=events,
                          clock=FakeClock([0]))
        self.assertEqual(res.status, "PASS")
        self.assertEqual(factory.calls, 1)
        self.assertTrue(engine.closed)
        self.assertEqual(helper.calls, ["launch", "terminate", "wait"])
        self.assertEqual(events, ["helper_launch", "helper_ready", "engine_new", "generate",
                                  "engine_close", "helper_terminate", "helper_wait"])
        self.assertIsInstance(res.prompt_passes, list)   # PP/TG wired via RA/parsing, no crash

    def test_ignores_none_heartbeats(self):
        engine = self._engine(tokens=[5, None, 7])
        res = RR.run_pair(exe="/exe", args=[], env={}, log="x",
                          fixture=fixture(expected_output_ids=[5, 7]),
                          engine_factory=CountingFactory(engine=engine), helper=FakeHelper(),
                          clock=FakeClock([0]))
        self.assertEqual(res.generated_ids, [5, 7])

    def test_helper_ready_timeout_no_engine_and_cleaned(self):
        helper, events = FakeHelper(ready=False), []
        factory = CountingFactory()
        res = RR.run_pair(exe="/exe", args=[], env={}, log="x", fixture=fixture(),
                          engine_factory=factory, helper=helper, events=events,
                          clock=FakeClock([0, 1.0, 1.0]), ready_deadline_s=1.0)
        self.assertEqual(res.status, "FAIL")
        self.assertIn("helper readiness deadline", " ".join(res.problems))
        self.assertEqual(factory.calls, 0)                       # no engine leaked
        self.assertEqual(helper.calls, ["launch", "terminate", "wait"])  # cleaned, not leaked

    def test_engine_factory_error_cleans_pair(self):
        helper, events = FakeHelper(), []
        factory = CountingFactory(raise_exc=RuntimeError("engine boot failed"))
        res = RR.run_pair(exe="/exe", args=[], env={}, log="x", fixture=fixture(),
                          engine_factory=factory, helper=helper, events=events, clock=FakeClock([0]))
        self.assertEqual(res.status, "FAIL")
        self.assertIn("engine boot failed", " ".join(res.problems))
        self.assertEqual(factory.calls, 1)                       # exactly one attempt, no retry
        self.assertEqual(helper.calls, ["launch", "terminate", "wait"])

    def test_midstream_eof_incomplete(self):
        engine = self._engine(tokens=[1, 2], last={})            # no finish/generated
        res = RR.run_pair(exe="/exe", args=[], env={}, log="x", fixture=fixture(),
                          engine_factory=CountingFactory(engine=engine), helper=FakeHelper(),
                          clock=FakeClock([0]))
        self.assertEqual(res.status, "FAIL")
        self.assertIn("incomplete/DONEless/EOF", " ".join(res.problems))

    def test_request_deadline(self):
        engine = self._engine(tokens=list(range(100)))
        res = RR.run_pair(exe="/exe", args=[], env={}, log="x", fixture=fixture(),
                          engine_factory=CountingFactory(engine=engine), helper=FakeHelper(),
                          clock=FakeClock([0, 0, 50.0, 50.0]), request_deadline_s=1.0)
        self.assertEqual(res.status, "FAIL")
        self.assertIn("request_deadline", res.events)
        self.assertEqual(res.events[-3:], ["engine_close", "helper_terminate", "helper_wait"])

    def test_missing_state(self):
        engine = self._engine(log="")                             # no STATE_HASH line
        res = RR.run_pair(exe="/exe", args=[], env={}, log="x", fixture=fixture(),
                          engine_factory=CountingFactory(engine=engine), helper=FakeHelper(),
                          clock=FakeClock([0]))
        self.assertEqual(res.status, "FAIL")
        self.assertIn("missing/empty required state hashes", " ".join(res.problems))

    def test_state_mismatch(self):
        engine = self._engine()
        wrong = dict(RR.parse_state_hashes(GOOD_STATE, RR.REQUIRED_STATE_KEYS), kv="ffff")
        res = RR.run_pair(exe="/exe", args=[], env={}, log="x",
                          fixture=fixture(expected_state=wrong),
                          engine_factory=CountingFactory(engine=engine), helper=FakeHelper(),
                          clock=FakeClock([0]))
        self.assertEqual(res.status, "FAIL")
        self.assertIn("state hash mismatch", " ".join(res.problems))

    def test_id_mismatch(self):
        engine = self._engine(tokens=[1, 2, 3])
        res = RR.run_pair(exe="/exe", args=[], env={}, log="x",
                          fixture=fixture(expected_output_ids=[1, 2, 999]),
                          engine_factory=CountingFactory(engine=engine), helper=FakeHelper(),
                          clock=FakeClock([0]))
        self.assertEqual(res.status, "FAIL")
        self.assertIn("generated raw token IDs mismatch", " ".join(res.problems))

    def test_nonfinite_metric(self):
        engine = self._engine(last=complete_last(prompt_ms=float("nan")))
        res = RR.run_pair(exe="/exe", args=[], env={}, log="x", fixture=fixture(),
                          engine_factory=CountingFactory(engine=engine), helper=FakeHelper(),
                          clock=FakeClock([0]))
        self.assertEqual(res.status, "FAIL")
        self.assertIn("nonfinite prompt_ms", " ".join(res.problems))

    def test_argv_drift_refuses_before_any_pair(self):
        helper, events = FakeHelper(), []
        factory = CountingFactory()
        res = RR.run_pair(exe="/exe", args=["--mtp"], env={}, log="x", fixture=fixture(),
                          engine_factory=factory, helper=helper, events=events, clock=FakeClock([0]))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(factory.calls, 0)                       # no engine started
        self.assertEqual(helper.calls, [])                       # no pair leaked
        self.assertEqual(events, [])

    def test_close_before_terminate_order(self):
        engine = self._engine()
        events: list[str] = []
        RR.run_pair(exe="/exe", args=[], env={}, log="x", fixture=fixture(),
                    engine_factory=CountingFactory(engine=engine), helper=FakeHelper(),
                    events=events, clock=FakeClock([0]))
        self.assertLess(events.index("engine_close"), events.index("helper_terminate"))
        self.assertLess(events.index("helper_terminate"), events.index("helper_wait"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

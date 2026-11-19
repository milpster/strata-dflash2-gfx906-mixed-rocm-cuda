#!/usr/bin/env python3
"""CPU/fake-only tests for the offline raw-token lifecycle adapter. No GPU, no socket,
no engine, no server import.

  python3 tests/test_ram_raw_cpu.py        # stdlib only
The adapter's wired seams are exercised through injected fakes (helper handle, engine-handle
factory, worker launcher, watchdog, clock). The real `serve.server` import, the destructive
spawn path and any real process must NEVER be reached here; several tests assert that
directly. Fixtures live under the project-local, git-ignored out/tests-tmp; the real
benchmark/cache/prod trees are untouched.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
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
GOOD_STATE_BYTES = GOOD_STATE.encode()


# ---- fakes: helper handle, engine handle, watchdog, worker launcher --------------

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

    def wait(self, timeout: float = 120.0):
        self.calls.append("wait")


class FakeEngineHandle:
    """Mimics the seam run_worker consumes (mirrors RealEngineHandle's real wire API)."""
    def __init__(self, argv, tokens=(10, 11, 12), last=None, info=None, log=b"",
                 raise_exc=None, no_log_window=False, close_raises=False):
        self.argv, self._tokens = list(argv), tokens
        self.last = {"finish": "eos", "generated": 3, "prompt_ms": 10.0, "decode_ms": 20.0} if last is None else last
        self.info = {"version": "x"} if info is None else info
        self._log, self._raise, self._no_lw = log, raise_exc, no_log_window
        self._close_raises, self.closed, self.pid = close_raises, False, 4242
        self.proc = type("P", (), {"pid": 4242})()

    def generate(self, ids, max_new, sampling, cancel):
        # Faithful to server.generate: a blocked/no-output generate only stops once `cancel`
        # is set (here: returns immediately if already cancelled, else yields int + None beats).
        if self._raise:
            raise self._raise
        if cancel.is_set():
            return
        for t in self._tokens:
            if isinstance(t, int):
                yield t
            else:
                yield t  # None heartbeats pass through unchanged

    def log_window(self):
        return self._log

    def close(self):
        if self._close_raises:
            raise RuntimeError("engine stuck")
        self.closed = True


class NoSeamHandle:
    """Minimal faithful engine-handle WITHOUT a log_window seam (forces the bounded-file read).
    Like the real engine, it appends the request's STATE_HASH line to the log during generate,
    i.e. AFTER run_worker captured the entry offset, so the bounded window must catch it."""
    def __init__(self, argv, tokens=(1,), log_path=None):
        self.argv, self._tokens, self._log_path = list(argv), tokens, log_path
        self.last = {"finish": "eos", "generated": 3, "prompt_ms": 1.0, "decode_ms": 1.0}
        self.info, self.pid, self.closed = {"version": "x"}, 4242, False

    def generate(self, ids, max_new, sampling, cancel):
        for t in self._tokens:
            yield t
        if self._log_path:
            with open(self._log_path, "ab") as fh:
                fh.write(b"\n" + GOOD_STATE_BYTES)

    def close(self):
        self.closed = True


class CountingFactory:
    def __init__(self, handle=None, raise_exc=None):
        self.calls = 0
        self._handle, self._raise = handle, raise_exc

    def __call__(self, exe, args, env, log_path):
        self.calls += 1
        if self._raise:
            raise self._raise
        return self._handle


def never_watchdog(seconds, on_fire):
    return lambda: None                       # happy path: watchdog never fires


class ImmediateWatchdog:
    """Fires synchronously the moment it is armed: a stalled no-yield case, no sleeps."""
    def __init__(self):
        self.armed = 0

    def __call__(self, seconds, on_fire):
        self.armed += 1
        on_fire()
        return lambda: None


def fixture(**over) -> RR.PairFixture:
    base = dict(ids=[100, 101, 102], max_new=8, sampling={"min_p": 0.5, "seed": 7})
    base.update(over)
    return RR.PairFixture(**base)


def clean_proof() -> RR.StopProof:
    return RR.StopProof(exit_code=0, verdict_text="stop-all: before=2 after=0 — ALL STOPPED")


def absent_probes() -> dict:
    return {"live_serving_processes": lambda: 0, "supervisor_alive": lambda: False,
            "isolated_sockets_free": lambda: []}


def good_handle(exe, args, **over) -> FakeEngineHandle:
    over.setdefault("log", GOOD_STATE_BYTES)
    handle = FakeEngineHandle([exe, "--serve", *args], **over)
    if over.get("no_log_window"):
        del handle.log_window                               # exercise the bounded-file read path
    return handle


# ---- safety: import/CLI/preflight paths touch nothing ---------------------------

class ImportAndCliIsInactive(unittest.TestCase):
    def test_import_never_loads_server(self):
        self.assertNotIn("serve", sys.modules)
        self.assertNotIn("serve.server", sys.modules)

    def test_default_and_check_env_launch_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(RR.main([]), 0)
            RR.main(["check-env"])
        self.assertNotIn("serve", sys.modules)
        self.assertEqual(_POPEN_CALLS, [])

    def test_run_without_confirm_refuses(self):
        out = fresh_scratch(self)
        manifest = self._manifest(out)
        with self.assertRaises(RR.AdapterBlocked):
            RR.main(["run", "--worker", "--manifest", manifest, "--fixture", self._fixture(out),
                     "--stop-proof", self._proof(out)])
        self.assertNotIn("serve", sys.modules)
        self.assertEqual(_POPEN_CALLS, [])

    def test_preflight_dirty_or_missing_proof_refuses(self):
        out = fresh_scratch(self)
        manifest = self._manifest(out)
        bad = out / "bad-proof.txt"
        bad.write_text("stop-all: before=3 after=3 — STILL ALIVE", encoding="utf-8")
        for proof in (bad, out / "missing-proof.txt"):
            with self.assertRaises(RR.AdapterBlocked):
                RR.main(["preflight", "--manifest", manifest, "--stop-proof", str(proof)])
        self.assertNotIn("serve", sys.modules)
        self.assertEqual(_POPEN_CALLS, [])

    @staticmethod
    def _manifest(out: Path) -> str:
        a = plan_args(out)
        m = {k: (str(v) if isinstance(v, Path) else v) for k, v in a.items() if k != "base_cfg"}
        m["base_config"] = str(PROD_CONFIG)
        p = out / "manifest.json"
        p.write_text(json.dumps(m), encoding="utf-8")
        return str(p)

    @staticmethod
    def _fixture(out: Path) -> str:
        p = out / "fixture.json"
        p.write_text(json.dumps({"ids": [1, 2, 3], "max_new": 4, "sampling": {"min_p": 0.5}}), encoding="utf-8")
        return str(p)

    @staticmethod
    def _proof(out: Path) -> str:
        p = out / "proof.txt"
        p.write_text("ALL STOPPED", encoding="utf-8")
        return str(p)


class PreflightGate(unittest.TestCase):
    def test_empty_probes_block(self):
        absent, reasons = RR.production_absent({})
        self.assertFalse(absent)
        self.assertEqual(len(reasons), len(RR.REQUIRED_PROBES))   # every required probe missing

    def test_probe_error_treated_as_present(self):
        def boom():
            raise OSError("proc unreadable")
        probes = dict(absent_probes(), live_serving_processes=boom)
        absent, reasons = RR.production_absent(probes)
        self.assertFalse(absent)
        self.assertTrue(any("assume present" in r for r in reasons))

    def test_absence_passes_when_all_absent(self):
        self.assertEqual(RR.production_absent(absent_probes()), (True, []))

    def test_stop_proof_clean(self):
        self.assertTrue(clean_proof().is_clean)
        self.assertFalse(RR.StopProof(1, "ALL STOPPED").is_clean)
        self.assertFalse(RR.StopProof(0, "STILL ALIVE").is_clean)

    def test_execute_gate_blocks_on_each_stage(self):
        with self.assertRaises(RR.AdapterBlocked):        # no confirm
            RR.assert_execute_allowed(confirm=False, env={"STRATA_ENGINE_READY_S": "1"},
                                      stop_proof=clean_proof(), probes=absent_probes())
        with self.assertRaises(RR.AdapterBlocked):        # dirty stop proof
            RR.assert_execute_allowed(confirm=True, env={"STRATA_ENGINE_READY_S": "1"},
                                      stop_proof=RR.StopProof(0, "STILL ALIVE"), probes=absent_probes())
        with self.assertRaises(RR.AdapterBlocked):        # empty probes
            RR.assert_execute_allowed(confirm=True, env={"STRATA_ENGINE_READY_S": "1"},
                                      stop_proof=clean_proof(), probes={})
        with self.assertRaises(RR.AdapterBlocked):        # import-time env missing
            RR.assert_execute_allowed(confirm=True, env={}, stop_proof=clean_proof(), probes=absent_probes())
        RR.assert_execute_allowed(confirm=True, env={"STRATA_ENGINE_READY_S": "2400"},
                                  stop_proof=clean_proof(), probes=absent_probes())   # passes


# ---- A. config / argv / env / scope construction --------------------------------

class PlanAndArgv(unittest.TestCase):
    def test_prepare_builds_canonical_tuple(self):
        out = fresh_scratch(self)
        plan = RR.prepare_plan(**plan_args(out))
        argv = plan["expected_engine_argv"]
        self.assertNotIn("--serve", argv)
        self.assertEqual(RA._flag_value(argv, "--layer-split"), "30")
        self.assertEqual(RA._flag_value(argv, "--expert-helper-socket"), plan["test_config"]["expert_helper_socket"])
        self.assertEqual(RA._flag_value(argv, "--expert-helper-socket2"), plan["test_config"]["expert_helper_socket2"])
        self.assertEqual(RA.min_free_floor_mib(plan["test_config"]), RA.FLOOR_MIB)
        hargv = plan["expected_helper_argv"]
        self.assertEqual(RA._flag_value(hargv, "--slots"), "4600")
        self.assertEqual(RA._flag_value(hargv, "--fill-mib"), "7100")
        self.assertNotEqual(RA._flag_value(hargv, "--socket"), RA._flag_value(hargv, "--socket2"))
        # Helper stays unscoped; only the engine worker is wrapped in the engine scope.
        self.assertNotIn("systemd-run", hargv)
        scoped = RR.scoped_worker_argv(["python", "ram_raw.py", "run", "--worker"])
        self.assertEqual(scoped[:len(RR.ENGINE_SCOPE)], list(RR.ENGINE_SCOPE))
        self.assertTrue(RA.manifest_is_public(plan["provenance"]))
        self.assertEqual(_POPEN_CALLS, [])

    def test_build_worker_command_forwards_contract(self):
        cmd = RR.build_worker_command(manifest_path="/m.json", fixture_path="/f.json", confirm=True)
        self.assertIn("run", cmd)
        self.assertIn("--worker", cmd)
        self.assertIn("--confirm-production-down", cmd)
        self.assertIn("/m.json", cmd)
        self.assertIn("/f.json", cmd)

    def test_build_engine_argv_rejects_serve_in_config(self):
        cfg = {"args": ["--serve", "--conversation-cache-dir", "/x"], "gpu": ["0", "1"], "layer_split": 30}
        with self.assertRaises(RA.ConfigRejected):
            RR.build_engine_argv(cfg, "/exe/strata")

    def test_env_precedence_config_env_wins(self):
        cfg = {"env": {"STRATA_STATE_HASH": "1", "HSA_OVERRIDE_GFX_VERSION": "9.0.6"}}
        merged = RR.plan_engine_env_precedence(cfg, {"STRATA_STATE_HASH": "0", "PATH": "/keep"})
        self.assertEqual(merged["STRATA_STATE_HASH"], "1")
        self.assertEqual(merged["PATH"], "/keep")

    def test_import_time_env_ok(self):
        self.assertEqual(RR.import_time_env_ok({}), (False, ["STRATA_ENGINE_READY_S"]))
        self.assertEqual(RR.import_time_env_ok({"STRATA_ENGINE_READY_S": "2400"}), (True, []))

    def test_cli_prepare_manifest_writes_plan(self):
        out = fresh_scratch(self)
        a = plan_args(out)
        m = {k: (str(v) if isinstance(v, Path) else v) for k, v in a.items() if k != "base_cfg"}
        m["base_config"] = str(PROD_CONFIG)
        mpath = out / "manifest.json"
        mpath.write_text(json.dumps(m), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(RR.main(["prepare", "--manifest", str(mpath), "--write"]), 0)
            self.assertEqual(RR.main(["helper-command", "--manifest", str(mpath)]), 0)
        self.assertTrue((Path(a["out_dir"]) / "ram_raw_plan.json").exists())
        self.assertNotIn("serve", sys.modules)
        self.assertEqual(_POPEN_CALLS, [])


# ---- exact this-arm argv check (no cache-dir exception) -------------------------

class ExactArgv(unittest.TestCase):
    def test_exact_match_ok(self):
        expected = ["--layer-split", "30", "--conversation-cache-dir", "/iso/cache"]
        observed = ["/exe/strata", "--serve", *expected]
        self.assertEqual(RR.exact_argv_problems("/exe/strata", expected, observed), [])

    def test_production_cache_value_rejected(self):
        expected = ["--conversation-cache-dir", "/isolated/cache"]
        observed = ["/exe/strata", "--serve", "--conversation-cache-dir", "/mnt/prod/cache"]
        self.assertTrue(RR.exact_argv_problems("/exe/strata", expected, observed))

    def test_double_serve_rejected(self):
        expected = ["--layer-split", "30"]
        observed = ["/exe/strata", "--serve", "--serve", "--layer-split", "30"]
        self.assertTrue(RR.exact_argv_problems("/exe/strata", expected, observed))

    def test_extra_fallback_flag_rejected(self):
        expected = ["--layer-split", "30"]
        observed = ["/exe/strata", "--serve", "--layer-split", "30", "--mtp"]
        self.assertTrue(RR.exact_argv_problems("/exe/strata", expected, observed))


# ---- B. real worker protocol via a faithful fake handle -------------------------

class RunWorker(unittest.TestCase):
    def _run(self, handle, args=("--layer-split", "30"), watchdog=never_watchdog, **over):
        exe = "/exe/strata"
        factory = over.pop("factory", None) or CountingFactory(handle=handle)
        res = RR.run_worker(exe=exe, args=list(args), env={}, log_path=over.pop("log_path", "x.log"),
                            fixture=over.pop("fixture", fixture()), engine_factory=factory,
                            watchdog=watchdog, **over)
        return res, factory

    def test_happy_capture_and_close(self):
        handle = good_handle("/exe/strata", ["--layer-split", "30"])
        fx = fixture(expected_output_ids=[10, 11, 12],
                     expected_state=RR.parse_state_hashes(GOOD_STATE, RR.REQUIRED_STATE_KEYS))
        res, factory = self._run(handle, fixture=fx)
        self.assertEqual(res.status, "PASS")
        self.assertEqual(factory.calls, 1)
        self.assertTrue(handle.closed)
        self.assertEqual(res.engine_pid, 4242)
        self.assertEqual(res.state_hashes, RR.parse_state_hashes(GOOD_STATE, RR.REQUIRED_STATE_KEYS))
        self.assertIsInstance(res.prompt_passes, list)
        self.assertEqual(res.events, ["engine_new", "argv_check", "generate", "engine_close"])

    def test_ignores_none_heartbeats(self):
        handle = good_handle("/exe/strata", [], tokens=[5, None, 7])
        res, _ = self._run(handle, args=[], fixture=fixture(expected_output_ids=[5, 7]))
        self.assertEqual(res.generated_ids, [5, 7])
        self.assertEqual(res.status, "PASS")

    def test_log_window_from_engine_handle(self):
        handle = good_handle("/exe/strata", [], log=GOOD_STATE_BYTES)
        res, _ = self._run(handle, args=[])
        self.assertEqual(res.engine_log_bytes, GOOD_STATE_BYTES)

    def test_log_window_file_fallback(self):
        out = fresh_scratch(self)
        logf = out / "engine.log"
        logf.write_bytes(b"prefix\n")
        # A handle WITHOUT a log_window seam forces the real bounded-file read path.
        handle = NoSeamHandle(["/exe/strata", "--serve"], tokens=[1], log_path=str(logf))
        res, _ = self._run(handle, args=[], log_path=str(logf))
        self.assertIn(b"STATE_HASH", res.engine_log_bytes)
        self.assertEqual(res.state_hashes, RR.parse_state_hashes(GOOD_STATE, RR.REQUIRED_STATE_KEYS))

    def test_argv_mismatch_stops_before_generate(self):
        exe = "/exe/strata"
        handle = FakeEngineHandle([exe, "--serve", "--conversation-cache-dir", "/prod/cache"], log=GOOD_STATE_BYTES)
        res, factory = self._run(handle, args=("--conversation-cache-dir", "/isolated/cache"))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(factory.calls, 1)
        self.assertTrue(handle.closed)                              # closed even though never generated
        self.assertNotIn("generate", res.events)

    def test_factory_error_records_and_no_close(self):
        factory = CountingFactory(raise_exc=RuntimeError("engine boot failed"))
        res, _ = self._run(None, factory=factory)
        self.assertEqual(res.status, "FAIL")
        self.assertIn("engine boot failed", " ".join(res.problems))
        self.assertEqual(factory.calls, 1)
        self.assertNotIn("engine_close", res.events)                # nothing was built to close

    def test_midstream_eof_incomplete(self):
        handle = good_handle("/exe/strata", [], tokens=[1, 2], last={})
        res, _ = self._run(handle, args=[])
        self.assertEqual(res.status, "FAIL")
        self.assertIn("incomplete/DONEless/EOF", " ".join(res.problems))

    def test_missing_state(self):
        handle = good_handle("/exe/strata", [], log=b"")
        res, _ = self._run(handle, args=[])
        self.assertEqual(res.status, "FAIL")
        self.assertIn("missing/empty required state hashes", " ".join(res.problems))

    def test_state_mismatch(self):
        wrong = dict(RR.parse_state_hashes(GOOD_STATE, RR.REQUIRED_STATE_KEYS), kv="ffff")
        handle = good_handle("/exe/strata", [])
        res, _ = self._run(handle, args=[], fixture=fixture(expected_state=wrong))
        self.assertEqual(res.status, "FAIL")
        self.assertIn("state hash mismatch", " ".join(res.problems))

    def test_id_mismatch(self):
        handle = good_handle("/exe/strata", [], tokens=[1, 2, 3])
        res, _ = self._run(handle, args=[], fixture=fixture(expected_output_ids=[1, 2, 999]))
        self.assertEqual(res.status, "FAIL")
        self.assertIn("generated raw token IDs mismatch", " ".join(res.problems))

    def test_nonfinite_metrics(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            handle = good_handle("/exe/strata", [], last={"finish": "eos", "generated": 3,
                                                          "prompt_ms": bad, "decode_ms": 1.0})
            res, _ = self._run(handle, args=[])
            self.assertEqual(res.status, "FAIL")
            self.assertIn("nonfinite prompt_ms", " ".join(res.problems))

    def test_argv_drift_refuses_before_build(self):
        factory = CountingFactory()
        res, _ = self._run(None, args=("--mtp",), factory=factory)
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(factory.calls, 0)
        self.assertEqual(res.events, [])

    def test_stalled_no_yield_is_cancelled(self):
        # A generate that stops only on `cancel` (faithful to the real wire); the watchdog fires
        # synchronously, so this is bounded WITHOUT sleeps and yields nothing.
        handle = good_handle("/exe/strata", [], tokens=[])
        wd = ImmediateWatchdog()
        res, _ = self._run(handle, args=[], watchdog=wd)
        self.assertEqual(res.status, "FAIL")
        self.assertIn("watchdog cancelled", " ".join(res.problems))
        self.assertTrue(handle.closed)                              # bounded shutdown still closed it

    def test_close_exception_recorded(self):
        handle = good_handle("/exe/strata", [], close_raises=True)
        res, _ = self._run(handle, args=[])
        self.assertIn("engine.close", " ".join(res.problems))       # recorded, not swallowed silently

    def test_parse_state_hashes_cases(self):
        self.assertEqual(set(RR.parse_state_hashes(GOOD_STATE, RR.REQUIRED_STATE_KEYS)), set(RR.REQUIRED_STATE_KEYS))
        self.assertEqual(RR.parse_state_hashes(GOOD_STATE.replace("kv=7a", ""), RR.REQUIRED_STATE_KEYS), {})
        self.assertEqual(RR.parse_state_hashes("nothing", RR.REQUIRED_STATE_KEYS), {})


# ---- log_window bounded file read ----------------------------------------------

class LogWindow(unittest.TestCase):
    def test_offset_and_limit_bounded(self):
        out = fresh_scratch(self)
        p = out / "e.log"
        p.write_bytes(b"0123456789")
        self.assertEqual(RR.log_window(p, 2, 3), b"234")
        self.assertEqual(RR.log_window(p, 0, 1 << 20), b"0123456789")
        self.assertEqual(RR.log_window(out / "missing.log", 0), b"")


# ---- RealEngineHandle passthrough over a fake engine (no real engine) ----------

class RealHandle(unittest.TestCase):
    def test_passthrough_and_log_window(self):
        out = fresh_scratch(self)
        logf = out / "e.log"
        logf.write_bytes(b"prelude\n")
        engine = good_handle("/exe", [])                             # a fake that mimics StrataEngine
        handle = RR.RealEngineHandle(engine, str(logf), ["/exe", "--serve", "--x"])
        self.assertEqual(handle.argv, ["/exe", "--serve", "--x"])
        self.assertEqual(handle.pid, 4242)
        self.assertEqual(handle.info, {"version": "x"})
        engine._tokens = [7]
        self.assertEqual(list(handle.generate([1], 1, {}, threading.Event())), [7])
        logf.write_bytes(b"prelude\n" + GOOD_STATE_BYTES)
        self.assertEqual(handle.log_window(), GOOD_STATE_BYTES)       # reads only past the start offset
        handle.close()
        self.assertTrue(engine.closed)


# ---- B. outer owner: helper outside scope, close-before-terminate, no leak ------

class OwnedPair(unittest.TestCase):
    def _launcher(self, events, status="PASS"):
        def launch():
            events.append("engine_close")                             # the scoped worker's finally
            return RR.WorkerResult(status=status, generated_ids=[1])
        return launch

    def test_happy_close_before_terminate(self):
        helper, events = FakeHelper(), []
        outcome = RR.run_owned(helper=helper, worker_launcher=self._launcher(events), stop_proof=clean_proof(),
                               confirm=True, env={"STRATA_ENGINE_READY_S": "2400"}, probes=absent_probes(),
                               events=events)
        self.assertEqual(outcome.status, "PASS")
        self.assertEqual(helper.calls, ["launch", "terminate", "wait"])
        self.assertLess(events.index("engine_close"), events.index("helper_terminate"))
        self.assertLess(events.index("helper_terminate"), events.index("helper_wait"))

    def test_helper_not_ready_no_worker_and_cleaned(self):
        helper, events = FakeHelper(ready=False), []
        launched = {"n": 0}

        def launcher():
            launched["n"] += 1
            return RR.WorkerResult(status="PASS", generated_ids=[])
        outcome = RR.run_owned(helper=helper, worker_launcher=launcher, stop_proof=clean_proof(),
                               confirm=True, env={"STRATA_ENGINE_READY_S": "2400"}, probes=absent_probes(),
                               events=events)
        self.assertEqual(outcome.status, "FAIL")
        self.assertEqual(launched["n"], 0)                            # worker never started
        self.assertEqual(helper.calls, ["launch", "terminate", "wait"])         # helper cleaned, not leaked

    def test_helper_exits_cleaned(self):
        helper, events = FakeHelper(alive=False), []
        outcome = RR.run_owned(helper=helper, worker_launcher=self._launcher(events), stop_proof=clean_proof(),
                               confirm=True, env={"STRATA_ENGINE_READY_S": "2400"}, probes=absent_probes(),
                               events=events)
        self.assertEqual(outcome.status, "FAIL")
        self.assertEqual(helper.calls, ["launch", "terminate", "wait"])

    def test_worker_crash_still_cleans_helper(self):
        helper, events = FakeHelper(), []

        def boom():
            raise RuntimeError("worker died")
        outcome = RR.run_owned(helper=helper, worker_launcher=boom, stop_proof=clean_proof(),
                               confirm=True, env={"STRATA_ENGINE_READY_S": "2400"}, probes=absent_probes(),
                               events=events)
        self.assertEqual(outcome.status, "FAIL")
        self.assertIn("worker died", " ".join(outcome.problems))
        self.assertEqual(helper.calls, ["launch", "terminate", "wait"])         # owned helper never leaks

    def test_helper_launched_once_worker_once(self):
        helper, events = FakeHelper(), []
        RR.run_owned(helper=helper, worker_launcher=self._launcher(events), stop_proof=clean_proof(),
                     confirm=True, env={"STRATA_ENGINE_READY_S": "2400"}, probes=absent_probes(), events=events)
        self.assertEqual(events.count("helper_launch"), 1)
        self.assertEqual(events.count("worker_launch"), 1)

    def test_preflight_refuses_before_helper_launch(self):
        helper, events = FakeHelper(), []
        with self.assertRaises(RR.AdapterBlocked):                    # empty probes -> block
            RR.run_owned(helper=helper, worker_launcher=self._launcher(events), stop_proof=clean_proof(),
                         confirm=True, env={"STRATA_ENGINE_READY_S": "2400"}, probes={}, events=events)
        self.assertEqual(helper.calls, [])                            # preflight blocked before any launch


if __name__ == "__main__":
    unittest.main(verbosity=2)

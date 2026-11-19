#!/usr/bin/env python3
"""CPU/fake-only tests for the offline raw-token lifecycle adapter. No GPU, no socket,
no engine, no server import.

  python3 tests/test_ram_raw_cpu.py        # stdlib only

The adapter's wired CLI callgraph — `main(["preflight"])`, `main(["run"])`,
`main(["run","--worker"])` — is exercised END-TO-END through the single `Seams` injection point
with fakes for EVERY external effect (popen, pgrep, /proc readers, server import, helper handle,
socket probe, result read). The real `serve.server` import, the destructive `StrataEngine`
constructor / `end_orphans` path and any real process must NEVER be reached here; several tests
assert that directly. Fixtures live under the project-local, git-ignored out/tests-tmp; the real
benchmark/cache/prod trees are untouched.

These are NOT function-only tests standing beside the CLI: the positive cases drive the actual
`main` branches (gate -> run_owned -> scoped worker -> run_worker -> write result JSON), and the
negative cases assert the gate refuses with zero engine construction / zero real spawn.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
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
    global _POPEN_CALLS, _REAL_POPEN, _RUN_CALLS, _REAL_RUN
    _POPEN_CALLS, _REAL_POPEN = [], subprocess.Popen
    _RUN_CALLS, _REAL_RUN = [], subprocess.run
    subprocess.Popen = _refuse_spawn  # pyright: ignore[assignment]
    subprocess.run = _refuse_run      # pyright: ignore[assignment]


def tearDownModule():
    subprocess.Popen = _REAL_POPEN  # pyright: ignore[assignment]
    subprocess.run = _REAL_RUN      # pyright: ignore[assignment]


def _refuse_spawn(*_a, **_k):
    _POPEN_CALLS.append(1)
    raise AssertionError("no real process may spawn in offline tests")


def _refuse_run(*_a, **_k):
    _RUN_CALLS.append(1)
    raise AssertionError("no real subprocess.run may execute in offline tests")


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
    return "strata serve: STATE_HASH L=0 " + " ".join(f"{k}={v}" for k, v in vals.items())


GOOD_STATE = state_line()
GOOD_STATE_BYTES = GOOD_STATE.encode()
GOOD_HASH = RR.parse_state_hashes(GOOD_STATE, RR.REQUIRED_STATE_KEYS)


# ---- fakes: helper handle, engine handle, watchdog, worker launcher --------------

class FakeHelper:
    """Conforms to the SubprocessHelper interface run_owned consumes (wait_ready/start_time/pid)."""
    def __init__(self, ready: bool = True, alive: bool = True, pid: int | None = 70001,
                 start_time: int | None = 4321):
        self._ready, self._alive = ready, alive
        self.calls: list[str] = []
        self._pid, self._start = pid, start_time

    @property
    def pid(self):
        return self._pid

    @property
    def start_time(self):
        return self._start

    def launch(self):
        self.calls.append("launch")

    def wait_ready(self, deadline_s, *, clock, sleep):
        self.calls.append("ready")
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
        self.exe = ""
        self.proc = type("P", (), {"pid": 4242, "poll": lambda self=None: None})()

    def generate(self, ids, max_new, sampling, cancel):
        if self._raise:
            raise self._raise
        if cancel.is_set():
            return
        for t in self._tokens:
            yield t  # int IDs and None heartbeats pass through unchanged

    def log_window(self):
        return self._log

    def close(self):
        if self._close_raises:
            raise RuntimeError("engine stuck")
        self.closed = True


class NoSeamHandle:
    """Minimal engine-handle WITHOUT a log_window seam (forces the bounded-file read path)."""
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


# ---- CLI fixtures + fake seams --------------------------------------------------

def write_manifest(out: Path, arm: str = "candidate") -> tuple[str, dict]:
    """Write manifest.json + fixture.json into scratch; return (manifest_path, plan)."""
    a = plan_args(out, arm)
    plan = RR.prepare_plan(**a)
    m = {k: (str(v) if isinstance(v, Path) else v) for k, v in a.items() if k != "base_cfg"}
    m["base_config"] = str(PROD_CONFIG)
    mpath = out / "manifest.json"
    mpath.write_text(json.dumps(m), encoding="utf-8")
    fix = out / "fixture.json"
    fix.write_text(json.dumps({"ids": [1, 2, 3], "max_new": 4, "sampling": {"min_p": 0.5}}), encoding="utf-8")
    return str(mpath), plan


def write_clean_proof(out: Path) -> str:
    p = out / "proof.txt"
    p.write_text("stop-all: before=2 after=0 — ALL STOPPED", encoding="utf-8")
    Path(str(p) + ".code").write_text("0\n", encoding="utf-8")   # required exit-code sidecar
    return str(p)


def set_ready_env(case: unittest.TestCase):
    """The run/worker gates read the import-time env from os.environ; supply it for the test only."""
    prev = os.environ.get("STRATA_ENGINE_READY_S")
    os.environ["STRATA_ENGINE_READY_S"] = "2400"

    def _restore():
        if prev is None:
            os.environ.pop("STRATA_ENGINE_READY_S", None)
        else:
            os.environ["STRATA_ENGINE_READY_S"] = prev
    case.addCleanup(_restore)


class FakeProc:
    """A subprocess.Popen stand-in already exited with a return code."""
    def __init__(self, rc: int = 0):
        self._rc, self.terminated, self.killed = rc, False, False

    def poll(self):
        return self._rc

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout: float = 0):
        return self._rc


class SeamKit:
    """Assembles RR.Seams with every external effect faked + a call log for negative assertions."""
    def __init__(self, out: Path, plan: dict):
        self.out, self.plan = out, plan
        self.popen_calls, self.pgrep_calls, self.simport_calls = [], 0, 0
        self.read_json_calls = 0
        self.pgrep_result: tuple[str, list[str]] = ("absent", [])
        self.sockets_present = True
        self.proc_identity_result: dict = {"exe": str(plan["expected_helper_argv"][0]), "start": 4321}
        self._proc_factory = lambda argv, **kw: FakeProc(0)

    def helper_factory(self, helper):
        return lambda plan: helper

    def seams(self, *, helper=None, server_import=None, process_reader=None,
              engine_factory=None) -> RR.Seams:
        def pgrep(pattern, run):
            self.pgrep_calls += 1
            return self.pgrep_result

        def popen(argv, **kw):
            self.popen_calls.append(list(argv))
            return self._proc_factory(argv, **kw)

        def socket_probe(path):
            return self.sockets_present

        def simport(serve_dir):
            self.simport_calls += 1
            return server_import(serve_dir) if server_import else types.SimpleNamespace()

        def proc_identity(pid):
            return self.proc_identity_result

        def read_json(out_dir, arm):
            self.read_json_calls += 1
            return RR.worker_result_paths(out_dir, arm)[0]

        return RR.Seams(
            popen=popen,
            run=lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""),  # never used (pgrep faked)
            pgrep=pgrep,
            socket_probe=socket_probe,
            process_reader=process_reader or (lambda pid: {"argv": [], "exe": ""}),
            proc_identity=proc_identity,
            self_pids=lambda: {65000},
            server_import=simport,
            engine_factory=engine_factory or RR.default_engine_factory,
            helper_factory=self.helper_factory(helper) if helper is not None else None,
            read_json=read_json,
            supervisor_state=self.out / "no-supervisor.json",   # absent -> supervisor_alive False
        )


def make_fake_server(expected_args, exe, ctor, tokens=(10, 11, 12), log=GOOD_STATE_BYTES,
                     last=None):
    """A fake `server` module surface: engine_args echoes the frozen argv; StrataEngine counts ctors."""
    def _engine_args(cfg, e, announce=False):
        return list(expected_args)

    class FE:
        def __init__(self, e, args, cwd=None, log="", env=None):
            ctor["n"] += 1
            self._log = log
            self.proc = types.SimpleNamespace(pid=4242, poll=lambda: None)
            self.last = last or {"finish": "eos", "generated": len(tokens), "prompt_ms": 1.0, "decode_ms": 1.0}
            self.info = {"v": "x"}

        def generate(self, ids, max_new, sampling, cancel):
            if self._log:                                   # a real engine appends its STATE_HASH line to stderr/log
                Path(self._log).parent.mkdir(parents=True, exist_ok=True)
                with open(self._log, "ab") as fh:
                    fh.write(b"\n" + GOOD_STATE_BYTES)
            for t in tokens:
                yield t

        def close(self):
            pass

    return types.SimpleNamespace(engine_args=_engine_args, child_env=lambda cfg: {}, StrataEngine=FE)


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
        self.assertEqual(_RUN_CALLS, [])

    def test_cli_prepare_manifest_writes_plan(self):
        out = fresh_scratch(self)
        mpath, _ = write_manifest(out)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(RR.main(["prepare", "--manifest", mpath, "--write"]), 0)
            self.assertEqual(RR.main(["helper-command", "--manifest", mpath]), 0)
        self.assertTrue((out / "ram_raw_plan.json").exists())
        self.assertNotIn("serve", sys.modules)
        self.assertEqual(_POPEN_CALLS, [])


# ---- POSITIVE + NEGATIVE CLI INTEGRATION through main() -------------------------

class CliPreflight(unittest.TestCase):
    def test_preflight_absent_and_clean_exit_zero(self):
        out = fresh_scratch(self)
        mpath, plan = write_manifest(out)
        proof = write_clean_proof(out)
        kit = SeamKit(out, plan)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = RR.main(["preflight", "--manifest", mpath, "--stop-proof", proof], seams=kit.seams())
        self.assertEqual(rc, 0)
        self.assertEqual(_POPEN_CALLS, [])                 # preflight never spawns
        self.assertEqual(kit.simport_calls, 0)             # and never imports the server

    def test_preflight_refuses_without_code_sidecar(self):
        out = fresh_scratch(self)
        mpath, plan = write_manifest(out)
        p = out / "proof.txt"
        p.write_text("ALL STOPPED", encoding="utf-8")       # no <proof>.code sidecar
        kit = SeamKit(out, plan)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            rc = RR.main(["preflight", "--manifest", mpath, "--stop-proof", str(p)], seams=kit.seams())
        self.assertEqual(rc, 2)
        self.assertIn("stop proof", err.getvalue().lower())

    def test_preflight_refuses_nonzero_exit_code(self):
        out = fresh_scratch(self)
        mpath, plan = write_manifest(out)
        p = out / "proof.txt"
        p.write_text("ALL STOPPED", encoding="utf-8")
        Path(str(p) + ".code").write_text("1\n", encoding="utf-8")
        kit = SeamKit(out, plan)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(RR.main(["preflight", "--manifest", mpath, "--stop-proof", str(p)], seams=kit.seams()), 2)

    def test_preflight_refuses_when_production_present(self):
        out = fresh_scratch(self)
        mpath, plan = write_manifest(out)
        proof = write_clean_proof(out)
        kit = SeamKit(out, plan)
        kit.pgrep_result = ("present", ["4321"])            # a live serving identity
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(RR.main(["preflight", "--manifest", mpath, "--stop-proof", proof], seams=kit.seams()), 2)


class CliRunOuter(unittest.TestCase):
    def test_run_happy_single_owner_and_result(self):
        out = fresh_scratch(self)
        mpath, plan = write_manifest(out)
        proof = write_clean_proof(out)
        set_ready_env(self)
        # Pre-write the scoped worker's persisted result the outer will read back.
        RR.write_worker_result(RR.WorkerResult(status="PASS", generated_ids=[10, 11, 12],
                                               engine_last={"finish": "eos", "generated": 3,
                                                            "prompt_ms": 1.0, "decode_ms": 1.0},
                                               engine_log_bytes=GOOD_STATE_BYTES, state_hashes=GOOD_HASH),
                               out, plan["arm"])
        helper = FakeHelper()
        kit = SeamKit(out, plan)
        with contextlib.redirect_stdout(io.StringIO()) as buf, contextlib.redirect_stderr(io.StringIO()):
            rc = RR.main(["run", "--manifest", mpath, "--fixture", str(out / "fixture.json"),
                          "--stop-proof", proof, "--confirm-production-down"],
                         seams=kit.seams(helper=helper))
        payload = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(_POPEN_CALLS, [])                          # only the FAKE popen was used
        self.assertEqual(helper.calls, ["launch", "ready", "terminate", "wait"])   # exactly one owner
        self.assertEqual(kit.simport_calls, 0)                       # outer never imports the server
        self.assertIn("engine_scope" if False else RR.ENGINE_SCOPE[0], " ".join(kit.popen_calls[0]))
        # The worker invocation is wrapped in the canonical engine scope (helper itself never is).
        self.assertEqual(kit.popen_calls[0][:len(RR.ENGINE_SCOPE)], list(RR.ENGINE_SCOPE))

    def test_run_without_confirm_raises_and_spawns_nothing(self):
        out = fresh_scratch(self)
        mpath, plan = write_manifest(out)
        proof = write_clean_proof(out)
        set_ready_env(self)
        kit = SeamKit(out, plan)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RR.AdapterBlocked):
                RR.main(["run", "--manifest", mpath, "--fixture", str(out / "fixture.json"),
                         "--stop-proof", proof], seams=kit.seams(helper=FakeHelper()))
        self.assertEqual(_POPEN_CALLS, [])
        self.assertEqual(kit.simport_calls, 0)

    def test_run_ready_timeout_never_launches_worker(self):
        out = fresh_scratch(self)
        mpath, plan = write_manifest(out)
        proof = write_clean_proof(out)
        set_ready_env(self)
        kit = SeamKit(out, plan)
        helper = FakeHelper(ready=False)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = RR.main(["run", "--manifest", mpath, "--fixture", str(out / "fixture.json"),
                          "--stop-proof", proof, "--confirm-production-down"],
                         seams=kit.seams(helper=helper))
        self.assertEqual(rc, 4)
        self.assertEqual(kit.popen_calls, [])                        # scoped worker never started
        self.assertEqual(helper.calls, ["launch", "ready", "terminate", "wait"])   # cleaned, not leaked


class CliWorker(unittest.TestCase):
    def _worker_seams(self, out, plan, *, ctor, expected_args, exe, tokens=(10, 11, 12)):
        server = make_fake_server(expected_args, exe, ctor, tokens=tokens)
        reader = lambda pid: {"argv": [exe, "--serve", *expected_args], "exe": exe}
        kit = SeamKit(out, plan)
        kit.pgrep_result = ("present", ["4321"])                      # the owned helper is the only live match
        kit.proc_identity_result = {"exe": str(plan["expected_helper_argv"][0]), "start": 555}
        return kit, kit.seams(server_import=lambda sd: server, process_reader=reader)

    def test_worker_happy_raw_ids_and_files(self):
        out = fresh_scratch(self)
        mpath, plan = write_manifest(out)
        proof = write_clean_proof(out)
        set_ready_env(self)
        # Pin the frozen expected raw output on the fixture the worker reads back.
        (out / "fixture.json").write_text(json.dumps(
            {"ids": [1, 2, 3], "max_new": 4, "sampling": {"min_p": 0.5},
             "expected_output_ids": [10, 11, 12], "expected_state": GOOD_HASH}), encoding="utf-8")
        ctor = {"n": 0}
        kit, seams = self._worker_seams(out, plan, ctor=ctor,
                                        expected_args=plan["expected_engine_argv"], exe=plan["exe"])
        with contextlib.redirect_stdout(io.StringIO()) as buf, contextlib.redirect_stderr(io.StringIO()):
            rc = RR.main(["run", "--worker", "--manifest", mpath, "--fixture", str(out / "fixture.json"),
                          "--stop-proof", proof, "--confirm-production-down",
                          "--owned-helper-pid", "4321", "--owned-helper-start", "555"], seams=seams)
        self.assertEqual(rc, 0)
        self.assertEqual(ctor["n"], 1)                                # engine constructed exactly once
        self.assertEqual(kit.simport_calls, 1)
        jpath, wpath = RR.worker_result_paths(out, plan["arm"])
        self.assertTrue(jpath.exists() and wpath.exists())
        result = RR.read_worker_result(jpath)
        self.assertEqual(result.generated_ids, [10, 11, 12])
        self.assertEqual(result.state_hashes, GOOD_HASH)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(_POPEN_CALLS, [])                            # no real process for the worker either

    def test_worker_refuses_missing_owned_helper_proof(self):
        out = fresh_scratch(self)
        mpath, plan = write_manifest(out)
        proof = write_clean_proof(out)
        set_ready_env(self)
        ctor = {"n": 0}
        kit, seams = self._worker_seams(out, plan, ctor=ctor,
                                        expected_args=plan["expected_engine_argv"], exe=plan["exe"])
        kit.proc_identity_result = {"exe": str(plan["expected_helper_argv"][0]), "start": 999}   # wrong start
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            rc = RR.main(["run", "--worker", "--manifest", mpath, "--fixture", str(out / "fixture.json"),
                          "--stop-proof", proof, "--confirm-production-down",
                          "--owned-helper-pid", "4321", "--owned-helper-start", "555"], seams=seams)
        self.assertEqual(rc, 2)
        self.assertEqual(ctor["n"], 0)                                # engine never constructed on refusal
        self.assertIn("start", err.getvalue())

    def test_worker_refuses_on_substring_basename_identity(self):
        # A helper exe that only SHARES a basename (different dir) must NOT satisfy the exact proof.
        out = fresh_scratch(self)
        mpath, plan = write_manifest(out)
        proof = write_clean_proof(out)
        set_ready_env(self)
        ctor = {"n": 0}
        kit, seams = self._worker_seams(out, plan, ctor=ctor,
                                        expected_args=plan["expected_engine_argv"], exe=plan["exe"])
        kit.proc_identity_result = {"exe": "/somewhere/else/" + Path(plan["expected_helper_argv"][0]).name,
                                    "start": 555}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = RR.main(["run", "--worker", "--manifest", mpath, "--fixture", str(out / "fixture.json"),
                          "--stop-proof", proof, "--confirm-production-down",
                          "--owned-helper-pid", "4321", "--owned-helper-start", "555"], seams=seams)
        self.assertEqual(rc, 2)
        self.assertEqual(ctor["n"], 0)

    def test_worker_refuses_unowned_helper_present(self):
        out = fresh_scratch(self)
        mpath, plan = write_manifest(out)
        proof = write_clean_proof(out)
        set_ready_env(self)
        ctor = {"n": 0}
        kit, seams = self._worker_seams(out, plan, ctor=ctor,
                                        expected_args=plan["expected_engine_argv"], exe=plan["exe"])
        kit.pgrep_result = ("present", ["4321", "99999"])            # an extra unowned serving identity
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = RR.main(["run", "--worker", "--manifest", mpath, "--fixture", str(out / "fixture.json"),
                          "--stop-proof", proof, "--confirm-production-down",
                          "--owned-helper-pid", "4321", "--owned-helper-start", "555"], seams=seams)
        self.assertEqual(rc, 2)
        self.assertEqual(ctor["n"], 0)

    def test_worker_refuses_when_socket_missing(self):
        out = fresh_scratch(self)
        mpath, plan = write_manifest(out)
        proof = write_clean_proof(out)
        set_ready_env(self)
        ctor = {"n": 0}
        kit, seams = self._worker_seams(out, plan, ctor=ctor,
                                        expected_args=plan["expected_engine_argv"], exe=plan["exe"])
        kit.sockets_present = False                                   # isolated sockets not live
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = RR.main(["run", "--worker", "--manifest", mpath, "--fixture", str(out / "fixture.json"),
                          "--stop-proof", proof, "--confirm-production-down",
                          "--owned-helper-pid", "4321", "--owned-helper-start", "555"], seams=seams)
        self.assertEqual(rc, 2)
        self.assertEqual(ctor["n"], 0)

    def test_worker_refuses_without_confirm(self):
        out = fresh_scratch(self)
        mpath, plan = write_manifest(out)
        proof = write_clean_proof(out)
        set_ready_env(self)
        ctor = {"n": 0}
        kit, seams = self._worker_seams(out, plan, ctor=ctor,
                                        expected_args=plan["expected_engine_argv"], exe=plan["exe"])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = RR.main(["run", "--worker", "--manifest", mpath, "--fixture", str(out / "fixture.json"),
                          "--stop-proof", proof,
                          "--owned-helper-pid", "4321", "--owned-helper-start", "555"], seams=seams)
        self.assertEqual(rc, 2)
        self.assertEqual(ctor["n"], 0)


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
        self.assertNotIn("systemd-run", hargv)                        # helper stays unscoped
        scoped = RR.scoped_worker_argv(["python", "ram_raw.py", "run", "--worker"])
        self.assertEqual(scoped[:len(RR.ENGINE_SCOPE)], list(RR.ENGINE_SCOPE))
        self.assertTrue(RA.manifest_is_public(plan["provenance"]))
        self.assertEqual(_POPEN_CALLS, [])

    def test_build_worker_command_forwards_owned_identity(self):
        cmd = RR.build_worker_command(manifest_path="/m.json", fixture_path="/f.json", stop_proof="/p.txt",
                                      owned_helper_pid=4321, confirm=True, owned_helper_start=555)
        self.assertIn("--worker", cmd)
        self.assertIn("--confirm-production-down", cmd)
        self.assertEqual(cmd[cmd.index("--owned-helper-pid") + 1], "4321")
        self.assertEqual(cmd[cmd.index("--owned-helper-start") + 1], "555")

    def test_build_engine_argv_rejects_serve_in_config(self):
        cfg = {"args": ["--serve", "--conversation-cache-dir", "/x"], "gpu": ["0", "1"], "layer_split": 30}
        with self.assertRaises(RA.ConfigRejected):
            RR.build_engine_argv(cfg, "/exe/strata")

    def test_clean_helper_env_is_env_i_not_full_merge(self):
        env = RR.clean_helper_env({"PATH": "/usr/bin", "HOME": "/h", "TERM": "xterm",
                                   "STRATA_DF_WINDOW": "leak", "DFLASH_KNOB": "leak"})
        self.assertEqual(env["STRATA_HELPER_REPLY_TIMEOUT_MS"], "600000")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("STRATA_DF_WINDOW", env)                     # inherited knobs never carried
        self.assertNotIn("DFLASH_KNOB", env)

    def test_engine_scope_env_unsets_and_sets(self):
        env = RR.engine_scope_env({"CUDA_VISIBLE_DEVICES": "0", "LD_PRELOAD": "x", "PATH": "/keep"})
        for k in RR.ENGINE_UNSET:
            self.assertNotIn(k, env)
        for k, v in RR.ENGINE_SET_DEFAULT.items():
            self.assertEqual(env[k], v)
        self.assertIn("LD_LIBRARY_PATH", env)
        self.assertEqual(env["PATH"], "/keep")                        # other inherited vars kept

    def test_import_time_env_ok(self):
        self.assertEqual(RR.import_time_env_ok({}), (False, ["STRATA_ENGINE_READY_S"]))
        self.assertEqual(RR.import_time_env_ok({"STRATA_ENGINE_READY_S": "2400"}), (True, []))


# ---- D. phase-correct gates + stop proof ----------------------------------------

class Gates(unittest.TestCase):
    def test_empty_probes_block(self):
        absent, reasons = RR.production_absent({})
        self.assertFalse(absent)
        self.assertEqual(len(reasons), len(RR.REQUIRED_PROBES))

    def test_probe_error_treated_as_present(self):
        def boom():
            raise OSError("proc unreadable")
        absent, reasons = RR.production_absent(dict(absent_probes(), live_serving_processes=boom))
        self.assertFalse(absent)
        self.assertTrue(any("assume present" in r for r in reasons))

    def test_pgrep_rc_semantics(self):
        ok = lambda rc, out="": types.SimpleNamespace(returncode=rc, stdout=out)
        self.assertEqual(RR.run_pgrep("pat", lambda c, **k: ok(0, "12 34\n")), ("present", ["12", "34"]))
        self.assertEqual(RR.run_pgrep("pat", lambda c, **k: ok(1)), ("absent", []))
        self.assertEqual(RR.run_pgrep("pat", lambda c, **k: ok(2))[0], "error")        # rc2 -> error, NOT absent

    def test_run_gate_blocks_on_each_stage(self):
        with self.assertRaises(RR.AdapterBlocked):                # no confirm
            RR.run_gate(confirm=False, env={"STRATA_ENGINE_READY_S": "1"}, stop_proof=clean_proof(), probes=absent_probes())
        with self.assertRaises(RR.AdapterBlocked):                # dirty stop proof
            RR.run_gate(confirm=True, env={"STRATA_ENGINE_READY_S": "1"}, stop_proof=RR.StopProof(0, "STILL ALIVE"),
                        probes=absent_probes())
        with self.assertRaises(RR.AdapterBlocked):                # empty probes
            RR.run_gate(confirm=True, env={"STRATA_ENGINE_READY_S": "1"}, stop_proof=clean_proof(), probes={})
        with self.assertRaises(RR.AdapterBlocked):                # import-time env missing
            RR.run_gate(confirm=True, env={}, stop_proof=clean_proof(), probes=absent_probes())
        RR.run_gate(confirm=True, env={"STRATA_ENGINE_READY_S": "2400"}, stop_proof=clean_proof(),
                    probes=absent_probes())                       # passes

    def test_load_stop_proof_requires_sidecar(self):
        out = fresh_scratch(self)
        p = out / "proof.txt"
        p.write_text("ALL STOPPED", encoding="utf-8")
        self.assertIsNone(RR._load_stop_proof(str(p)))            # missing .code sidecar
        Path(str(p) + ".code").write_text("0\n", encoding="utf-8")
        self.assertTrue(RR._load_stop_proof(str(p)).is_clean)
        Path(str(p) + ".code").write_text("1\n", encoding="utf-8")
        self.assertFalse(RR._load_stop_proof(str(p)).is_clean)    # nonzero exit -> not clean

    def test_owned_helper_proof_exact_not_substring(self):
        out = fresh_scratch(self)
        helper_bin = str(out / "slots/cand/rpcw-cuda/strata-expert-helper-patient")
        socks = [str(out / "run/cand.sock"), str(out / "run/cand2.sock")]
        identity = {"pid": 1, "exe": helper_bin, "start": 555}
        good = RR.owned_helper_proof(owned_pid=1, owned_start=555, proc_identity=lambda pid: identity,
                                     helper_bin=helper_bin, out_dir=str(out),
                                     socket_probe=lambda p: True, sockets=socks)
        self.assertEqual(good, [])
        wrong = RR.owned_helper_proof(owned_pid=1, owned_start=555,
                                      proc_identity=lambda pid: {"pid": 1, "exe": "/other/dir/strata-expert-helper-patient", "start": 555},
                                      helper_bin=helper_bin, out_dir=str(out), socket_probe=lambda p: True, sockets=socks)
        self.assertTrue(wrong)                                    # basename substring is NOT enough


# ---- exact this-arm argv check (no cache-dir exception) -------------------------

class ExactArgv(unittest.TestCase):
    def test_exact_match_ok(self):
        expected = ["--layer-split", "30", "--conversation-cache-dir", "/iso/cache"]
        self.assertEqual(RR.exact_argv_problems("/exe/strata", expected, ["/exe/strata", "--serve", *expected]), [])

    def test_production_cache_value_rejected(self):
        observed = ["/exe/strata", "--serve", "--conversation-cache-dir", "/mnt/prod/cache"]
        self.assertTrue(RR.exact_argv_problems("/exe/strata", ["--conversation-cache-dir", "/isolated/cache"], observed))

    def test_double_serve_rejected(self):
        self.assertTrue(RR.exact_argv_problems("/exe/strata", ["--layer-split", "30"],
                                               ["/exe/strata", "--serve", "--serve", "--layer-split", "30"]))


# ---- C. real engine factory: drift rejected BEFORE the destructive constructor --

class RealFactory(unittest.TestCase):
    def _deps(self, server, reader):
        return RR.EngineDeps(server_import=lambda: server, process_reader=reader)

    def test_resolved_drift_aborts_before_constructor(self):
        ctor = {"n": 0}
        drift_args = ["--layer-split", "30", "--conversation-cache-dir", "/prod/cache"]   # ≠ expected
        server = make_fake_server(drift_args, "/exe", ctor)
        build = RR.default_engine_factory({"cwd": None}, self._deps(server, lambda pid: {"argv": [], "exe": ""}))
        with self.assertRaises(RR.AdapterBlocked):
            build("/exe", ["--layer-split", "30", "--conversation-cache-dir", "/isolated/cache"], {}, "log")
        self.assertEqual(ctor["n"], 0)                            # StrataEngine ctor (end_orphans) never reached

    def test_valid_build_constructs_once_and_reads_proc_argv(self):
        ctor = {"n": 0}
        expected = ["--layer-split", "30"]
        observed_argv = ["/exe", "--serve", "--layer-split", "30", "--from-proc"]
        server = make_fake_server(expected, "/exe", ctor)
        reader = lambda pid: {"argv": observed_argv, "exe": "/exe"}     # /proc differs from resolver's list
        build = RR.default_engine_factory({"cwd": "/cwd"}, self._deps(server, reader))
        handle = build("/exe", expected, {}, "log")
        self.assertEqual(ctor["n"], 1)
        self.assertEqual(handle.argv, observed_argv)              # OBSERVED /proc argv, not the resolver copy

    def test_real_engine_handle_argv_from_reader_not_engine(self):
        out = fresh_scratch(self)
        logf = out / "e.log"
        logf.write_bytes(b"prelude\n")
        engine = good_handle("/exe", ["--serve", "--resolver"])   # engine's own list is IGNORED
        reader = lambda pid: {"argv": ["/exe", "--serve", "--proc"], "exe": "/exe"}
        handle = RR.RealEngineHandle(engine, str(logf), reader)
        self.assertEqual(handle.argv, ["/exe", "--serve", "--proc"])   # provenance from /proc only
        self.assertEqual(handle.pid, 4242)


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
        fx = fixture(expected_output_ids=[10, 11, 12], expected_state=GOOD_HASH)
        res, f = self._run(handle, fixture=fx)
        self.assertEqual(res.status, "PASS")
        self.assertEqual(f.calls, 1)
        self.assertTrue(handle.closed)
        self.assertEqual(res.state_hashes, GOOD_HASH)
        self.assertEqual(res.events, ["engine_new", "argv_check", "generate", "engine_close"])

    def test_ignores_none_heartbeats(self):
        handle = good_handle("/exe/strata", [], tokens=[5, None, 7])
        res, _ = self._run(handle, args=[], fixture=fixture(expected_output_ids=[5, 7]))
        self.assertEqual(res.generated_ids, [5, 7])

    def test_log_window_file_fallback(self):
        out = fresh_scratch(self)
        logf = out / "engine.log"
        logf.write_bytes(b"prefix\n")
        handle = NoSeamHandle(["/exe/strata", "--serve"], tokens=[1], log_path=str(logf))
        res, _ = self._run(handle, args=[], log_path=str(logf))
        self.assertIn(b"STATE_HASH", res.engine_log_bytes)
        self.assertEqual(res.state_hashes, GOOD_HASH)

    def test_argv_mismatch_stops_before_generate(self):
        handle = FakeEngineHandle(["/exe/strata", "--serve", "--conversation-cache-dir", "/prod/cache"], log=GOOD_STATE_BYTES)
        res, f = self._run(handle, args=("--conversation-cache-dir", "/isolated/cache"))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(f.calls, 1)
        self.assertNotIn("generate", res.events)

    def test_factory_error_records_and_no_close(self):
        factory = CountingFactory(raise_exc=RuntimeError("engine boot failed"))
        res, _ = self._run(None, factory=factory)
        self.assertEqual(res.status, "FAIL")
        self.assertNotIn("engine_close", res.events)

    def test_midstream_eof_incomplete(self):
        handle = good_handle("/exe/strata", [], tokens=[1, 2], last={})
        res, _ = self._run(handle, args=[])
        self.assertIn("incomplete/DONEless/EOF", " ".join(res.problems))

    def test_missing_state(self):
        handle = good_handle("/exe/strata", [], log=b"")
        res, _ = self._run(handle, args=[])
        self.assertIn("missing/empty required state hashes", " ".join(res.problems))

    def test_nonfinite_metrics(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            handle = good_handle("/exe/strata", [], last={"finish": "eos", "generated": 3,
                                                          "prompt_ms": bad, "decode_ms": 1.0})
            res, _ = self._run(handle, args=[])
            self.assertIn("nonfinite prompt_ms", " ".join(res.problems))

    def test_argv_drift_refuses_before_build(self):
        factory = CountingFactory()
        res, _ = self._run(None, args=("--mtp",), factory=factory)
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(factory.calls, 0)

    def test_stalled_no_yield_is_cancelled(self):
        handle = good_handle("/exe/strata", [], tokens=[])
        res, _ = self._run(handle, args=[], watchdog=ImmediateWatchdog())
        self.assertEqual(res.status, "FAIL")
        self.assertIn("watchdog cancelled", " ".join(res.problems))
        self.assertTrue(handle.closed)

    def test_parse_state_hashes_cases(self):
        self.assertEqual(set(RR.parse_state_hashes(GOOD_STATE, RR.REQUIRED_STATE_KEYS)), set(RR.REQUIRED_STATE_KEYS))
        self.assertEqual(RR.parse_state_hashes(GOOD_STATE.replace("kv=7a", ""), RR.REQUIRED_STATE_KEYS), {})


# ---- log_window bounded file read ----------------------------------------------

class LogWindow(unittest.TestCase):
    def test_offset_and_limit_bounded(self):
        out = fresh_scratch(self)
        p = out / "e.log"
        p.write_bytes(b"0123456789")
        self.assertEqual(RR.log_window(p, 2, 3), b"234")
        self.assertEqual(RR.log_window(out / "missing.log", 0), b"")


# ---- B. outer owner: helper outside scope, close-before-terminate, no leak ------

class OwnedPair(unittest.TestCase):
    def _launcher(self, events, status="PASS"):
        def launch():
            events.append("engine_close")                             # the scoped worker's finally
            return RR.WorkerResult(status=status, generated_ids=[1])
        return launch

    def test_happy_close_before_terminate(self):
        helper, events = FakeHelper(), []
        outcome = RR.run_owned(helper=helper, worker_launcher=self._launcher(events), events=events)
        self.assertEqual(outcome.status, "PASS")
        self.assertEqual(helper.calls, ["launch", "ready", "terminate", "wait"])
        self.assertLess(events.index("engine_close"), events.index("helper_terminate"))
        self.assertLess(events.index("helper_terminate"), events.index("helper_wait"))

    def test_helper_not_ready_no_worker_and_cleaned(self):
        helper, events = FakeHelper(ready=False), []
        launched = {"n": 0}
        outcome = RR.run_owned(helper=helper, worker_launcher=lambda: launched.__setitem__("n", launched["n"] + 1),
                               events=events)
        self.assertEqual(outcome.status, "FAIL")
        self.assertEqual(launched["n"], 0)
        self.assertEqual(helper.calls, ["launch", "ready", "terminate", "wait"])

    def test_helper_exits_cleaned(self):
        helper, events = FakeHelper(alive=False), []
        outcome = RR.run_owned(helper=helper, worker_launcher=self._launcher(events), events=events)
        self.assertEqual(outcome.status, "FAIL")
        self.assertEqual(helper.calls, ["launch", "ready", "terminate", "wait"])

    def test_worker_crash_still_cleans_helper(self):
        helper, events = FakeHelper(), []

        def boom():
            raise RuntimeError("worker died")
        outcome = RR.run_owned(helper=helper, worker_launcher=boom, events=events)
        self.assertIn("worker died", " ".join(outcome.problems))
        self.assertEqual(helper.calls, ["launch", "ready", "terminate", "wait"])

    def test_helper_launched_once_worker_once(self):
        helper, events = FakeHelper(), []
        RR.run_owned(helper=helper, worker_launcher=self._launcher(events), events=events)
        self.assertEqual(events.count("helper_launch"), 1)
        self.assertEqual(events.count("worker_launch"), 1)

    def test_helper_wait_timeout_still_returns(self):
        helper, events = FakeHelper(), []
        helper.wait = lambda timeout=120.0: (_ for _ in ()).throw(subprocess.TimeoutExpired("h", 1))
        outcome = RR.run_owned(helper=helper, worker_launcher=self._launcher(events), events=events)
        self.assertEqual(outcome.status, "PASS")                      # wait failure must not leak or crash the owner


# ---- thin launcher: ONE owner (the Python outer run), no shell second owner -----

class ThinLauncher(unittest.TestCase):
    LAUNCH = Path(__file__).resolve().parents[1] / "gpu-rounds" / "ram_raw_launch.sh"

    def test_launcher_has_no_second_owner_and_execs_run(self):
        text = self.LAUNCHER_TEXT = self.LAUNCH.read_text(encoding="utf-8")
        # No shell-side helper start/wait/kill, scoped worker, or trap — the Python `run` owns all.
        for forbidden in ("helper_pid", "kill -", "trap ", "mapfile", "worker_rc", "listening on",
                          "systemd-run --user"):
            self.assertNotIn(forbidden, text, f"launcher must not contain shell-side owner logic: {forbidden}")
        # It must hand control to exactly the Python outer owner via exec.
        self.assertIn('ram_raw.py" run', text)
        self.assertIn("--confirm-production-down", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

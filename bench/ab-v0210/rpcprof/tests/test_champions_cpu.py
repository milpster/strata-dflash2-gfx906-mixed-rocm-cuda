#!/usr/bin/env python3
"""CPU-only tests for the 2026-11-18 champions panel. No GPU, no endpoint, no engine.

  python3 tests/test_champions_cpu.py          # stdlib only
  python3 -m unittest discover -s tests -v     # same thing
"""
from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

RPC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RPC))
sys.path.insert(0, str(RPC / "gpu-rounds"))
import champions_log as LOG  # noqa: E402
import champions_panel as PANEL  # noqa: E402
import champions_prepare as PREP  # noqa: E402

OUT = PREP.DEFAULT_OUT

# Nothing a test creates, rewrites, renames or deletes may live in the real measurement
# tree. OUT now holds completed benchmark evidence (arms/current, summary/, logs/*.engine.log),
# so every mutable fixture goes under here instead: project-local, git-ignored, and removed
# when the test that made it ends.
SCRATCH = RPC / "out" / "tests-tmp"

# Read-only trees a test may open but must never modify.
EVIDENCE_DIRS = ("arms", "summary", "logs", "run", "archive")
EVIDENCE_FILES = ("manifest.json", "manifest.sha256")


def fresh_scratch(case: unittest.TestCase) -> Path:
    """Per-test scratch root, removed on teardown. Never the real benchmark tree."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=SCRATCH))
    case.addCleanup(shutil.rmtree, root, True)
    return root


def _stamp(path: Path) -> str:
    stat_ = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"size={stat_.st_size} mtime={int(stat_.st_mtime)} sha256={digest}"


def evidence_snapshot() -> dict[str, str]:
    """Inventory of the real benchmark evidence: stat + sha256, read-only by construction.

    Taken at import time, before any test can run, and compared again afterwards, so a
    test that writes into, renames or deletes measurement data fails loudly instead of
    quietly invalidating a completed arm.
    """
    snapshot: dict[str, str] = {}
    for name in EVIDENCE_DIRS:
        root = OUT / name
        if not root.is_dir():
            snapshot[f"{name}/"] = "absent"
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                snapshot[str(path.relative_to(OUT))] = _stamp(path)
    for name in EVIDENCE_FILES:
        path = OUT / name
        if path.is_file():
            snapshot[name] = _stamp(path)
    return snapshot


EVIDENCE_BEFORE = evidence_snapshot()
MAIN = ("serve: prompt 16000 tokens = {r} reused + {k} read in {ms} ms (177.8 tok/s), "
        "512 generated in 21000 ms (24.4 tok/s), timeouts=0 fallbacks=0")
CONT = ("serve: prompt 1200 tokens = 0 reused + 1200 read in 100 ms (12.0 tok/s), "
        "8 generated in 400 ms (20.0 tok/s)")


def pair_windows(fresh_ms: float = 90000.0, cached_ms: float = 21000.0) -> tuple[bytes, bytes]:
    return (f"{MAIN.format(r=0, k=16000, ms=fresh_ms)}\n{CONT}\n".encode(),
            f"{CONT}\n{MAIN.format(r=16000, k=0, ms=cached_ms)}\n".encode())


class Corpus(unittest.TestCase):
    def test_exact_bytes_and_determinism(self):
        for shape in PREP.SHAPES:
            for pair in range(shape.pairs):
                a = PREP.build_paired_prompt(shape.name, pair, shape.ctx)
                b = PREP.build_paired_prompt(shape.name, pair, shape.ctx)
                self.assertEqual(len(a), shape.ctx)
                self.assertEqual(a, b)

    def test_prompt_identical_across_arms(self):
        """Arms must read byte-identical input: the corpus is not arm-parameterised."""
        for entry in json.loads((OUT / "manifest.json").read_text())["corpus"]:
            on_disk = Path(entry["path"]).read_bytes()
            self.assertEqual(on_disk, PREP.build_paired_prompt(entry["shape"], entry["pair"],
                                                               entry["bytes"]))
            self.assertEqual(len(on_disk), entry["bytes"])
            self.assertEqual(PREP.sha256_bytes(on_disk), entry["sha256"])

    def test_distinct_early_prefix_per_repeat(self):
        """The identity tag is at the HEAD: that is what actually defeats prefix reuse."""
        prompts = {pair: PREP.build_paired_prompt("short16k", pair, 88400)
                   for pair in range(4)}
        tags = {p.split(b"\n", 1)[0] for p in prompts.values()}
        self.assertEqual(len(tags), 4)
        for prompt in prompts.values():
            self.assertTrue(prompt.startswith(b"[champs work=short16k pair="))
        # differing only in the tail would leave the leading tokens cache-hit-able
        self.assertEqual(len({p[:200] for p in prompts.values()}), 4)

    def test_no_arm_name_inside_prompt(self):
        for name in PREP.ARM_ORDER:
            for entry in json.loads((OUT / "manifest.json").read_text())["corpus"]:
                self.assertNotIn(name.encode(), Path(entry["path"]).read_bytes())


class Driver(unittest.TestCase):
    # Pinned from the untouched implementation, so an edit to build_prompt that changes
    # existing ab9 leg bytes fails here rather than silently invalidating old results.
    LEGACY_SHA = {
        ("A", "8000-1"): "863b264e228d2fc8ccb10dbb2394ef497942e25059500078ece02db30e33ac0c",
        ("m2", "80000-1"): "acb0359c03d7192ef6d53988e8ceeb3281eda134878a5d311d662b515e248afe",
    }

    def test_legacy_builder_stable(self):
        driver = __import__("ab9_driver")
        for (arm, leg), digest in self.LEGACY_SHA.items():
            ctx = 8000 if leg == "8000-1" else 80000
            prompt = driver.build_prompt(arm, leg, ctx)
            self.assertEqual(len(prompt.encode()), ctx)
            self.assertTrue(prompt.rstrip().endswith(f"[ab9 arm={arm} leg={leg} ctx={ctx}]"))
            self.assertEqual(PREP.sha256_bytes(prompt.encode()), digest)

    def test_bad_prompt_length_rejected_before_any_socket(self):
        """A --prompt-file whose size disagrees with --ctx must fail closed, no request.

        --url points at a closed local port, never 8009, so even a harness bug could not
        put traffic on the production backend from a test.
        """
        corpus = json.loads((OUT / "manifest.json").read_text())["corpus"][0]["path"]
        result = subprocess.run(
            [sys.executable, str(RPC / "ab9_driver.py"), "--arm", "x", "--leg", "l",
             "--kind", "tg", "--ctx", "99", "--prompt-file", str(corpus),
             "--url", "http://127.0.0.1:1/v1/chat/completions",
             "--out", str(fresh_scratch(self)),
             "--elog", "/nonexistent.elog", "--hlog", "/nonexistent.hlog"],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prompt-file length", result.stderr + result.stdout)

    def test_pinned_prompt_sha_enforced(self):
        corpus = Path(json.loads((OUT / "manifest.json").read_text())["corpus"][0]["path"])
        result = subprocess.run(
            [sys.executable, str(RPC / "ab9_driver.py"), "--arm", "x", "--leg", "l",
             "--kind", "tg", "--ctx", str(corpus.stat().st_size), "--prompt-file", str(corpus),
             "--prompt-sha256", "0" * 64,
             "--url", "http://127.0.0.1:1/v1/chat/completions",
             "--out", str(fresh_scratch(self)),
             "--elog", "/nonexistent", "--hlog", "/nonexistent"],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prompt-file sha256", result.stderr + result.stdout)


class _NoNetwork:
    """Fails the moment anything in the module under test opens a socket."""

    def __enter__(self):
        self._socket, self._urlopen = socket.socket, urllib.request.urlopen

        def refuse(*_a, **_k):
            raise AssertionError("network use in a CPU-only path")

        socket.socket = refuse
        urllib.request.urlopen = refuse
        return self

    def __exit__(self, *_exc):
        socket.socket, urllib.request.urlopen = self._socket, self._urlopen


class LogParser(unittest.TestCase):
    def test_ignores_internal_continuation_lines(self):
        fresh, cached = pair_windows()
        first, second, unmatched = LOG.match_pair(LOG.parse_window(fresh), LOG.parse_window(cached))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(len(unmatched), 2, "the two continuation lines must be reported, not used")
        self.assertEqual((first.reused, first.read), (0, 16000))
        self.assertEqual((second.reused, second.read), (16000, 0))

    def test_refuses_ambiguous_window(self):
        noisy = LOG.parse_window(f"{MAIN.format(r=0, k=16000, ms=1)}\n"
                                 f"{MAIN.format(r=0, k=16000, ms=2)}\n".encode())
        noisy_cached = LOG.parse_window(f"{MAIN.format(r=16000, k=0, ms=3)}\n"
                                        f"{MAIN.format(r=16000, k=0, ms=4)}\n".encode())
        first, second, unmatched = LOG.match_pair(noisy, noisy_cached)
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(unmatched), 4)

    def test_fresh_and_cached_are_never_swapped(self):
        """A reused first leg is not fresh PP; a zero-read first leg is not a prefill."""
        first, second = pair_windows()
        reused_fresh = LOG.parse_window(f"{MAIN.format(r=16000, k=0, ms=1)}\n".encode())
        cached = LOG.parse_window(f"{MAIN.format(r=16000, k=0, ms=1)}\n".encode())
        leg = LOG.classify(_blank_leg(), reused_fresh[0], cached[0])
        self.assertFalse(leg.pp_usable)
        self.assertEqual(leg.fresh_reason, "fresh-leg-reused-16000")
        zero = LOG.parse_window(f"{MAIN.format(r=0, k=0, ms=1)}\n".encode())
        leg = LOG.classify(_blank_leg(), zero[0], cached[0])
        self.assertFalse(leg.pp_usable)
        self.assertEqual(leg.fresh_reason, "fresh-leg-zero-read")
        self.assertTrue(second)

    def test_summary_drops_missing_and_reused_pp(self):
        fresh, cached = pair_windows()
        fresh_pass = LOG.parse_window(fresh)[0]
        cached_pass = LOG.parse_window(cached)[1]

        good = _blank_leg("short16k-p1")
        good.fresh, good.cached = fresh_pass, cached_pass

        reused = _blank_leg("short16k-p2")          # a cache hit is not fresh PP
        reused.fresh, reused.cached = fresh_pass, cached_pass
        reused.fresh_reason = "fresh-leg-reused-16384"

        missing = _blank_leg("short16k-p3")          # no cached timing line at all
        missing.fresh = fresh_pass

        shape = LOG.summarise([good, reused, missing])["shapes"]["short16k"]
        # Partial credit is deliberate and per-metric: a leg with a cache-contaminated fresh
        # line still contributes a valid cached TG, and a leg with no cached line still
        # contributes a valid fresh PP. Only the broken measurement is dropped.
        self.assertEqual(shape["pp_tok_s"]["n"], 2)   # good + missing
        self.assertEqual(shape["tg_tok_s"]["n"], 2)   # good + reused
        self.assertEqual({d["leg"] for d in shape["pairs_dropped"]},
                         {"short16k-p2", "short16k-p3"})
        self.assertEqual(shape["fresh_reused"], [0])

    def test_medians_ignore_outliers_rather_than_mean_them(self):
        line = ("serve: prompt 16000 tokens = 0 reused + 16000 read in 100 ms "
                "({tps} tok/s), 512 generated in 21000 ms (24.4 tok/s)")
        cached = LOG.parse_window(pair_windows()[1])[1]
        legs = []
        for name, tps in [("a", 90.0), ("b", 180.0), ("c", 200.0)]:
            leg = _blank_leg(name)
            leg.fresh = LOG.parse_window(line.format(tps=tps).encode())[0]
            leg.cached = cached
            legs.append(leg)
        shape = LOG.summarise(legs)["shapes"]["short16k"]
        self.assertEqual(shape["pp_tok_s"]["median"], 180.0)
        self.assertEqual(shape["pp_tok_s"]["spread"], 110.0)

    def test_leg_round_trips_through_jsonl(self):
        leg = LOG.classify(_blank_leg(), *
                           [LOG.parse_window(pair_windows()[0])[0],
                            LOG.parse_window(pair_windows()[1])[1]])
        restored = LOG.LegResult.from_dict(json.loads(json.dumps(leg.to_dict())))
        self.assertEqual(restored.to_dict(), leg.to_dict())
        self.assertTrue(restored.pp_usable and restored.tg_usable)


def _blank_leg(name: str = "short16k-p0") -> LOG.LegResult:
    return LOG.LegResult(arm="current", leg=name, shape="short16k", pair=1,
                         measured=True, max_tokens=512)


class FrozenArtifacts(unittest.TestCase):
    def test_dry_run_never_touches_network_or_process_control(self):
        """The default path must not request, not kill, and not start anything."""
        blocked = _NoNetwork()
        blocked.__enter__()
        self.addCleanup(blocked.__exit__, None, None, None)
        real_run, real_popen = subprocess.run, subprocess.Popen
        seen: list[list[str]] = []

        def spy_run(cmd, *a, **k):
            seen.append([str(part) for part in cmd])
            return real_run(cmd, *a, **k)

        def forbidden_popen(*_a, **_k):
            raise AssertionError("dry-run must not spawn processes it needs")

        subprocess.run, subprocess.Popen = spy_run, forbidden_popen
        try:
            self.assertEqual(PANEL.dry_run(OUT), 0)
        finally:
            subprocess.run, subprocess.Popen = real_run, real_popen
        for cmd in seen:
            self.assertTrue(cmd[0].startswith("git"), f"unexpected subprocess in dry-run: {cmd}")

    def test_launcher_names_avoid_the_sweep(self):
        for name in PREP.ARM_ORDER:
            launcher = OUT / "bin" / f"champs-{name}.sh"
            self.assertTrue(launcher.is_file())
            for part in PREP.FORBIDDEN_NAME_PARTS:
                self.assertNotIn(part, launcher.name)

    def test_launcher_retarget_is_minimal_and_canonical_state_kept(self):
        text = (OUT / "bin" / "champs-nov16.sh").read_text()
        self.assertIn(f'BIN="{OUT / "slots" / "nov16"}"', text)
        self.assertIn(f'CONFIG="{OUT / "configs" / "nov16.json"}"', text)
        self.assertIn(f'SOCKET="{OUT / "run" / "nov16.sock"}"', text)
        # stop-all finds the pair through the canonical shared state/lock: never retargeted
        self.assertIn('--state "$ROOT/run/prod-swift15-f16kv-mixed-supervisor.json"', text)
        self.assertIn('--lock "$ROOT/run/mixed-supervisor.lock"', text)

    def test_restore_target_is_the_canonical_production_launcher(self):
        restore = (RPC / "gpu-rounds" / "champions_restore.sh").read_text()
        self.assertIn("/mnt/srcds/Strata/launch-v0238.sh", restore)
        self.assertIn("prod-config-v0238.json", restore)

    def test_slots_point_at_the_era_binaries(self):
        for arm in PREP.ARMS:
            link = OUT / "slots" / arm.name / "rpcw-hip" / "strata"
            self.assertEqual(link.resolve(), arm.engine)

    def test_config_quality_keys_identical_and_capacity_frozen(self):
        manifest = json.loads((OUT / "manifest.json").read_text())
        self.assertTrue(manifest["config_identical_across_arms"])
        for arm in manifest["arms"].values():
            self.assertEqual(arm["quality_identity"]["--max-context"], "262144")
            self.assertEqual(arm["quality_identity"]["--kv"], "int8")
            self.assertEqual(arm["quality_identity"]["--conversation-cache-disk-mib"], "24576")


class PanelExecution(unittest.TestCase):
    """Drives the real run_pair/run_arm/collect code against fake children.

    No engine, no endpoint, no process control: Popen, the launcher boot and stop-all
    are the only things substituted.

    Every mutable path here is sandboxed. The panel takes a scratch copy of the manifest
    whose engine log points into the scratch tree, so its arms/, summary/ and archive/
    land there too; read-only inputs (corpus bytes, frozen launchers and configs) stay
    real and are never written. Running these tests must not be able to disturb a
    completed arm — SuiteLeavesBenchmarkDataUntouched below proves that.
    """

    def setUp(self):
        self.scratch = fresh_scratch(self)
        self.out = self.scratch / "champions-20261118"
        self.out.mkdir()
        manifest = json.loads((OUT / "manifest.json").read_text())
        for spec in manifest["arms"].values():
            spec["engine_log"] = str(self.out / "logs" / Path(spec["engine_log"]).name)
        (self.out / "manifest.json").write_text(json.dumps(manifest))

        self.arm = "current"
        self.panel = PANEL.Panel(self.out, health=False)
        self.shape = next(s for s in self.panel.manifest["shapes"] if s["name"] == "short16k")
        self.spec = self.panel.manifest["arms"][self.arm]
        self.spec["helper_log"] = str(self.scratch / "helper.log")
        self.panel.boot = lambda arm: {"arm": arm}          # type: ignore[assignment]
        self.panel.unlink_sockets = lambda arm: None        # type: ignore[assignment]
        self.engine_log = Path(self.spec["engine_log"])
        self.engine_log.parent.mkdir(parents=True, exist_ok=True)
        self.engine_log.unlink(missing_ok=True)

    # -- helpers ------------------------------------------------------------------
    def _fakes(self, results: list[dict]) -> dict:
        """Fake Popen that replays scripted outcomes for successive run_pair calls."""
        calls: list[list[str]] = []
        state = {"index": -1}

        class FakeProcess:
            def __init__(self, cmd):
                state["index"] += 1
                self.script = results[state["index"] % len(results)]
                self.cmd = [str(part) for part in cmd]
                calls.append(self.cmd)
                self.polls = self.script.get("polls", 0)
                self.done = False

            def poll(self):
                if self.polls > 0:
                    self.polls -= 1
                    if self.script.get("on_poll"):
                        self.script["on_poll"]()
                    return None
                if not self.done:
                    self.done = True
                    if self.script.get("writer"):
                        self.script["writer"](self.cmd)
                    return None
                return self.script.get("exit", 0)

            def kill(self):
                pass

            def wait(self):
                return self.script.get("exit", 0)

        real = subprocess.Popen
        subprocess.Popen = lambda cmd, **_k: FakeProcess(cmd)
        self.addCleanup(setattr, subprocess, "Popen", real)
        return {"calls": calls}

    def _write_pair(self, cmd: list[str], *, reuse_fresh: bool = False) -> None:
        """Emit an engine log and driver record that the real parser will read back."""
        leg = cmd[cmd.index("--leg") + 1]
        arm = cmd[cmd.index("--arm") + 1]
        out = Path(cmd[cmd.index("--out") + 1])
        out.mkdir(parents=True, exist_ok=True)
        ctx = int(cmd[cmd.index("--ctx") + 1])
        shape = cmd[cmd.index("--champs-shape") + 1]
        pair = int(cmd[cmd.index("--champs-pair") + 1])
        fresh = MAIN.format(r=16000 if reuse_fresh else 0, k=0 if reuse_fresh else 16000,
                            ms=90000)
        first = f"{fresh}\n{CONT}\n".encode()
        second = f"{CONT}\n{MAIN.format(r=16000, k=0, ms=21000)}\n".encode()
        start = self.engine_log.stat().st_size if self.engine_log.exists() else 0
        with self.engine_log.open("ab") as sink:
            sink.write(first + second)
        mid, end = start + len(first), start + len(first) + len(second)
        record = {
            "arm": arm, "leg": leg, "kind": "tg", "ctx": ctx, "max_tokens": 512,
            "walls": [10.0, 4.0], "content_from": "content", "prompt_bytes": ctx,
            "prompt_sha256": next(c["sha256"] for c in self.panel.manifest["corpus"]
                                  if c["shape"] == shape and c["pair"] == pair),
            "requests": [
                {"index": 0, "wall_s": 10.0, "content_from": "content", "response_sha256": "a",
                 "elog_start": start, "elog_end": mid},
                {"index": 1, "wall_s": 4.0, "content_from": "content", "response_sha256": "b",
                 "elog_start": mid, "elog_end": end},
            ],
            "elog": str(self.engine_log), "elog_start": start, "elog_end": end,
            "mem": {}, "champs": {"shape": shape, "pair": pair, "measured": pair >= 1,
                                  "retries": 0},
        }
        (out / f"{arm}-{leg}.json").write_text(json.dumps(record))

    def _sandbox_restore(self, sandbox: Path, stop_exit: int, verdict: str) -> Path:
        """Rewrite the real restore script onto sandbox paths with a stub stop wrapper."""
        script = (RPC / "gpu-rounds" / "champions_restore.sh").read_text()
        rewritten = script.replace('R=/mnt/srcds/Strata', f'R={sandbox / "strata"}')
        rewritten = rewritten.replace(
            'OUT=$R/bench/ab-v0210/rpcprof/out/champions-20261118', f'OUT={sandbox / "out"}')
        bin_dir = sandbox / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (sandbox / "strata").mkdir(parents=True, exist_ok=True)
        restore = bin_dir / "champions_restore.sh"
        restore.write_text(rewritten)
        (bin_dir / "champions_stop.sh").write_text(
            f"#!/usr/bin/env bash\necho 'stop-all: {verdict}'\nexit {stop_exit}\n")
        (sandbox / "strata" / "launch-v0238.sh").write_text(
            f"#!/usr/bin/env bash\ntouch {sandbox / 'LAUNCHED'}\n")
        for path in (restore, bin_dir / "champions_stop.sh",
                     sandbox / "strata" / "launch-v0238.sh"):
            path.chmod(0o755)
        return restore

    # -- gap 1: live output while the child runs ----------------------------------
    def test_engine_log_is_streamed_while_the_driver_runs(self):
        pumped: list[int] = []
        real_pump = PANEL.Panel._pump_all
        PANEL.Panel._pump_all = lambda self: (pumped.append(1), real_pump(self))[1]
        self.addCleanup(setattr, PANEL.Panel, "_pump_all", real_pump)

        def on_poll():
            with self.engine_log.open("a") as sink:
                sink.write("strata serve: prompt 16000 tokens = 0 reused + 16000 read in "
                           "100 ms (160.0 tok/s), 8 generated in 100 ms (80.0 tok/s)\n")

        self._fakes([{"exit": 0, "polls": 3, "on_poll": on_poll}])
        original = PANEL.PUMP_S
        PANEL.PUMP_S = 0
        self.addCleanup(setattr, PANEL, "PUMP_S", original)
        self.panel.run_pair(self.arm, self.shape, 0)
        self.assertGreaterEqual(len(pumped), 3, "no live pumping during the request")

    # -- gap 2: anomalies abort rather than fake success, and never retry ----------
    def test_driver_nonzero_aborts_the_arm_without_retrying(self):
        fakes = self._fakes([{"exit": 3}])
        self.assertIsNone(self.panel.run_pair(self.arm, self.shape, 0))
        self.assertEqual(len(fakes["calls"]), 1, "a failed pair must not be retried")

    def test_timeout_kills_and_never_relaunches(self):
        fakes = self._fakes([{"polls": 10_000}])
        shape = dict(self.shape, request_timeout_s=0)
        self.assertIsNone(self.panel.run_pair(self.arm, shape, 0))
        self.assertEqual(len(fakes["calls"]), 1,
                         "relaunching while server-side work is still in flight would "
                         "overlap benchmark requests")

    def test_run_arm_stops_at_the_first_unusable_pair(self):
        self._fakes([{"exit": 0, "writer": lambda cmd: self._write_pair(cmd, reuse_fresh=True)}])
        self.panel.stop_all = lambda: True                  # type: ignore[assignment]
        self.assertFalse(self.panel.run_arm(self.arm))
        legs = (self.panel.arm_dir(self.arm) / "legs.jsonl").read_text().splitlines()
        self.assertEqual(len(legs), 1, "the arm must stop at the first bad pair")

    def test_zero_samples_cannot_report_success(self):
        self._fakes([{"exit": 1}])
        self.panel.stop_all = lambda: True                  # type: ignore[assignment]
        self.assertFalse(self.panel.run_arm(self.arm))
        summary = json.loads((self.out / "summary" / f"{self.arm}.summary.json").read_text())
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["pairs_complete"], 0)
        self.assertEqual(summary["pairs_planned"], self.panel.planned_pairs())
        for shape in summary["shapes"].values():
            self.assertEqual(shape["pp_tok_s"]["n"], 0)
            self.assertEqual(shape["tg_tok_s"]["n"], 0)

    def test_good_pair_is_complete_and_counted(self):
        self._fakes([{"exit": 0, "writer": self._write_pair}])
        result = self.panel.run_pair(self.arm, self.shape, 0)
        self.assertIsNotNone(result)
        self.assertTrue(result.complete, result.reasons)
        self.assertEqual(result.context_tokens, 16000)
        self.assertEqual(result.intended_bytes, self.shape["ctx_bytes"])

    # -- gap 3: a failed stop never leads to a launch ------------------------------
    def test_stop_all_requires_zero_exit_as_well_as_the_verdict(self):
        real = subprocess.run
        subprocess.run = lambda *_a, **_k: subprocess.CompletedProcess(
            [], returncode=1, stdout="stop-all: before=3 after=3 ALL STOPPED", stderr="")
        self.addCleanup(setattr, subprocess, "run", real)
        self.assertFalse(self.panel.stop_all())

    def test_failed_stop_prevents_any_boot(self):
        self.panel.stop_all = lambda: False                 # type: ignore[assignment]
        booted: list[str] = []
        self.panel.boot = lambda arm: booted.append(arm)    # type: ignore[assignment]
        self.assertFalse(self.panel.run_arm(self.arm))
        self.assertEqual(booted, [], "a busy box must not get a second pair")

    def test_restore_script_refuses_to_launch_after_a_failed_stop(self):
        root = self.scratch / "restore"
        restore = self._sandbox_restore(root, stop_exit=1, verdict="before=2 after=2 "
                                                                   "STILL ALIVE")
        result = subprocess.run(["bash", str(restore)], capture_output=True, text=True,
                                timeout=120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RESTORE-FAIL: stop-all was not clean", result.stdout)
        self.assertFalse((root / "LAUNCHED").exists(),
                         "restore launched a pair despite an unclean stop")

    def test_restore_log_path_excludes_launcher_separator(self):
        """The launcher separates the two paths with ';', so the parse must not keep it.

        Exercises the real assignment line lifted out of champions_restore.sh, parsed by
        a real bash, against an announcement shaped exactly like the launcher's output.
        """
        script = (RPC / "gpu-rounds" / "champions_restore.sh").read_text()
        assignment = next(line for line in script.splitlines()
                          if line.strip().startswith("LOG=$(sed"))
        raw = str(self.scratch)
        boot = Path(raw) / "launcher.out"
        expected = str(Path(raw) / "production.log")
        boot.write_text(f"Starting mixed backend on 8009; log: {expected}; "
                        f"helper: /path/helper.log (recover)\n")
        result = subprocess.run(
            ["bash", "-c", 'BOOT=$1\n' + assignment + '\nprintf "%s" "$LOG"', "_", str(boot)],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout, expected)

    def test_restore_script_proceeds_when_the_stop_is_clean(self):
        root = self.scratch / "restore"
        restore = self._sandbox_restore(root, stop_exit=0, verdict="before=0 after=0 - "
                                                                    "ALL STOPPED")
        result = subprocess.run(["bash", str(restore), "--timeout", "3"],
                                capture_output=True, text=True, timeout=120)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("stop-all was not clean", result.stdout)
        self.assertTrue((root / "LAUNCHED").exists(), "a clean stop did not proceed")
        self.assertIn("RESTORE-FAIL", result.stdout)

    # -- gap 4: a rerun cannot inherit earlier evidence ----------------------------
    def test_stale_supervisor_output_and_old_legs_are_not_counted(self):
        directory = self.panel.arm_dir(self.arm)
        self.assertNotEqual(directory.parent.parent, OUT, "the sandbox must not be the real tree")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "supervisor.out").write_text(
            "Starting mixed backend on 127.0.0.1:8009; log: /old/engine.log; "
            "helper: /old/helper.log\n")
        (directory / "legs.jsonl").write_text(json.dumps(_blank_leg("stale").to_dict()) + "\n")
        self.engine_log.write_text("strata serve: prompt 1 tokens = 0 reused + 1 read in 1 ms "
                                   "(1.0 tok/s), 1 generated in 1 ms (1.0 tok/s)\n")

        self.panel.fresh_run_dir(self.arm)

        self.assertFalse((directory / "supervisor.out").exists(),
                         "a stale announce line must not be able to satisfy the boot gate")
        self.assertFalse((directory / "legs.jsonl").exists(), "stale legs must not survive")
        self.assertFalse(self.engine_log.exists())
        archived = list((self.out / "archive").rglob("supervisor.out"))
        self.assertTrue(archived, "prior evidence must be preserved, not deleted")
        self.assertIn("Starting mixed backend", archived[0].read_text())
        self.assertTrue(list((self.out / "archive").rglob(self.engine_log.name)))

    def test_collect_on_a_fresh_dir_is_incomplete(self):
        self.panel.fresh_run_dir(self.arm)
        self.assertFalse(self.panel.collect(self.arm))


class SuiteLeavesBenchmarkDataUntouched(unittest.TestCase):
    """Regression guard against this very file destroying a completed arm.

    PanelExecution used to construct its Panel on the real out/ tree: setUp unlinked the
    real engine log, fresh_run_dir renamed the real arms/<arm> and summary into archive,
    and collect overwrote the real summary. A rerun of a single test would therefore have
    deleted finished measurements.

    The snapshot is taken at import, before any test runs; unittest loads classes in
    alphabetical order, so this one (S after P, L, D, C) asserts after every destructive
    test has had its chance.
    """

    def test_no_real_evidence_file_was_written_renamed_or_deleted(self):
        after = evidence_snapshot()
        missing = sorted(set(EVIDENCE_BEFORE) - set(after))
        changed = {path: (EVIDENCE_BEFORE[path], stamp)
                   for path, stamp in after.items()
                   if path in EVIDENCE_BEFORE and EVIDENCE_BEFORE[path] != stamp}
        self.assertEqual(missing, [], "a test deleted real measurement evidence")
        self.assertEqual(changed, {}, "a test rewrote real measurement evidence")

    def test_snapshot_actually_covers_a_completed_arm(self):
        """A guard that inventories nothing proves nothing; assert it saw the evidence."""
        covered = [path for path in EVIDENCE_BEFORE if path.startswith("arms/current/")]
        self.assertTrue(any(path.endswith("legs.jsonl") for path in covered),
                        f"guard is not watching the arm records: {covered}")
        self.assertTrue(any(path.endswith("engine.log") for path in EVIDENCE_BEFORE))


if __name__ == "__main__":
    unittest.main(verbosity=2)

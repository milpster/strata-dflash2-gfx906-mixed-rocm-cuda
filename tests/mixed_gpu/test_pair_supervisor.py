"""Real CPU-process recovery tests; never use production processes or sockets."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/mixed_gpu/pair_supervisor.py"
FIXTURE = Path(__file__).parent / "fixtures/mixed_pair_fake.py"
GUARD = Path(__file__).parent / "fixtures/launcher_guard.sh"
if sys.platform.startswith("linux"):
    spec = importlib.util.spec_from_file_location("pair_supervisor", SCRIPT)
    pair = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pair)
    staging_spec = importlib.util.spec_from_file_location(
        "staging_affinity", ROOT / "tools/mixed_gpu/staging_affinity.py")
    staging = importlib.util.module_from_spec(staging_spec)
    staging_spec.loader.exec_module(staging)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux process/socket ownership")
class ScratchTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "logs").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="pair-test-", dir=ROOT / "logs")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)


class RecoveryTests(ScratchTest):
    def start_pair(self, extra_env=None, extra_args=()):
        launcher = self.root / "launch.sh"
        launcher.write_text('exec "$FAKE_PY" "$FAKE_PAIR" launcher "$@"\n')
        config = self.root / "config.json"
        config.write_text(json.dumps({"cwd": str(self.root), "exe": sys.executable, "log": "engine.log"}))
        self.state = self.root / "state.json"
        self.environment = dict(os.environ, FAKE_PY=sys.executable, FAKE_PAIR=str(FIXTURE),
                                FAKE_PAIR_ROOT=str(self.root), PAIR_SUPERVISOR=str(SCRIPT),
                                **(extra_env or {}))
        self.command = [sys.executable, str(SCRIPT), "run", "--launcher", str(launcher),
                        "--config", str(config), "--state", str(self.state),
                        "--lock", str(self.root / "lock"),
                        "--socket", str(self.root / "one.sock"), "--socket", str(self.root / "two.sock"),
                        "--interval", "0.03", "--stop-timeout", "0.15", "--backoff", "0.15",
                        "--max-backoff", "0.3", "--startup-timeout", "5", *extra_args]
        output = (self.root / "supervisor.log").open("w")
        self.addCleanup(output.close)
        self.process = subprocess.Popen(self.command, env=self.environment, stdout=output, stderr=output)
        self.addCleanup(self.stop_pair)

    def stop_pair(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=12)
        audit = subprocess.run([sys.executable, str(FIXTURE), "--check", str(self.root)],
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(audit.returncode, 0, audit.stdout + audit.stderr)
        self.assertFalse((self.root / "one.sock").exists())
        self.assertFalse((self.root / "two.sock").exists())

    def wait_state(self, predicate):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                state = json.loads(self.state.read_text())
                if predicate(state):
                    return state
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            if self.process.poll() is not None:
                break
            time.sleep(0.02)
        self.fail(f"State deadline expired. Log:\n{(self.root / 'supervisor.log').read_text()}")

    def attached(self, generation=1):
        return self.wait_state(lambda state: state["state"] == "attached" and state["generation"] >= generation)

    def assert_replaced(self, before):
        after = self.attached(before["generation"] + 1)
        for name in ("helper", "server", "engine"):
            self.assertNotEqual(after[f"{name}_pid"], before[f"{name}_pid"])
            self.assertFalse(pair.alive(before[f"{name}_pid"], before[f"{name}_start"]))
        return after

    def test_each_peer_death_restarts_both_and_reaps_isolated_engine(self):
        self.start_pair()
        for name in ("helper", "server", "engine"):
            with self.subTest(peer=name):
                before = self.attached()
                os.kill(before[f"{name}_pid"], signal.SIGKILL)
                self.assert_replaced(before)
        self.stop_pair()
        self.assertEqual(json.loads(self.state.read_text())["state"], "stopped")
        self.assertFalse((self.root / "one.sock").exists())
        self.assertFalse((self.root / "two.sock").exists())

    def test_secondary_channel_failure_restarts_live_helper(self):
        self.start_pair()
        before = self.attached()
        with Path(before["helper_log"]).open("a") as log:
            log.write("expert-helper: second channel: request failed: helper timeout\n")
        self.assert_replaced(before)

    def test_engine_channel_close_restarts_peers_that_are_still_alive(self):
        self.start_pair()
        before = self.attached()
        with (self.root / "engine.log").open("a") as log:
            log.write("expert-helper: unexpected reply frame type 8; closing the channel\n")
        self.assert_replaced(before)

    def test_engine_cpu_fallback_restarts_pair_and_ignores_old_failure_next_boot(self):
        self.start_pair()
        before = self.attached()
        with (self.root / "engine.log").open("a") as log:
            log.write("strata generate: the expert helper is absent; the CPU path serves its rows for the whole run\n")
        after = self.assert_replaced(before)
        with (self.root / "engine.log").open("a") as log:
            log.write("strata generate: session is up\n")
        self.attached(after["generation"])
        self.assertEqual(json.loads(self.state.read_text())["generation"], after["generation"])

    def test_historical_engine_failure_does_not_poison_startup(self):
        (self.root / "engine.log").write_text("strata generate: helper connect failed\n")
        self.start_pair()
        self.assertEqual(self.attached()["generation"], 1)

    def test_helper_fill_without_engine_ack_never_becomes_attached(self):
        self.start_pair({"FAKE_NO_ENGINE_ACK": "1"}, ("--startup-timeout", "1"))
        state = self.wait_state(lambda state: state["state"] == "backoff")
        self.assertNotIn("attached_at", state)

    def test_alternative_positive_fill_message_is_accepted(self):
        self.start_pair({"FAKE_ALT_FILL": "1"})
        self.assertEqual(self.attached()["generation"], 1)

    def test_helper_death_before_server_start_is_retried(self):
        self.start_pair({"FAKE_HELPER_ONLY": "1"})
        before = self.wait_state(lambda state: state.get("helper_pid", 0) > 0 and state.get("server_pid") == 0)
        os.kill(before["helper_pid"], signal.SIGKILL)
        after = self.wait_state(lambda state: state["generation"] >= 2 and state.get("helper_pid", 0) > 0)
        self.assertNotEqual(before["helper_pid"], after["helper_pid"])
        self.assertNotIn("attached_at", after)

    def test_stop_during_startup_leaves_no_orphans_or_socket_files(self):
        for attempt in range(4):
            with self.subTest(attempt=attempt):
                self.start_pair({"FAKE_HELPER_ONLY": "1"})
                self.wait_state(lambda state: state.get("supervisor_pid") == self.process.pid
                                and state["state"] == "starting")
                self.stop_pair()

    def test_early_server_exit_reaps_unobserved_isolated_engine(self):
        self.start_pair({"FAKE_SERVER_EXIT": "1"}, ("--backoff", "2", "--max-backoff", "2"))
        self.wait_state(lambda state: state["state"] == "backoff")
        audit = subprocess.run([sys.executable, str(FIXTURE), "--check", str(self.root)],
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(audit.returncode, 0, audit.stdout + audit.stderr)

    def test_recovered_peer_processes_keep_hostapp_favored_priority(self):
        baseline = os.getpriority(os.PRIO_PROCESS, os.getpid())
        self.start_pair(extra_args=("--nice", "1"))
        before = self.attached()
        for name in ("helper", "server", "engine"):
            self.assertEqual(os.getpriority(os.PRIO_PROCESS, before[f"{name}_pid"]), min(19, baseline + 1))
        os.kill(before["helper_pid"], signal.SIGKILL)
        after = self.assert_replaced(before)
        for name in ("helper", "server", "engine"):
            self.assertEqual(os.getpriority(os.PRIO_PROCESS, after[f"{name}_pid"]), min(19, baseline + 1))

    def test_stage_affinity_applied_after_attach_and_each_new_engine(self):
        allowed = sorted(os.sched_getaffinity(0))
        if 0 not in allowed:
            self.skipTest("host does not expose CPU 0")
        target = [cpu for cpu in allowed if cpu != 0]
        self.start_pair(extra_args=("--stage-cpus", ",".join(map(str, target))))
        first = self.attached()
        applied = self.wait_state(
            lambda state: state.get("stage_affinity", {}).get("state") == "applied")
        self.assertEqual(applied["engine_pid"], first["engine_pid"])
        self.assertGreaterEqual(applied["stage_affinity"]["moved"], 1)
        saved = json.loads(Path(applied["stage_affinity"]["snapshot"]).read_text())
        self.assertEqual(saved["pid"], first["engine_pid"])
        stage_threads = [thread for thread in staging.threads(first["engine_pid"])
                         if thread["name"] == "strata-stage"]
        self.assertTrue(stage_threads)
        self.assertEqual([thread["cpus"] for thread in stage_threads], [target] * len(stage_threads))
        # The engine re-pins staging threads at every start, so a recovery
        # generation must re-apply the move for the new incarnation.
        os.kill(first["engine_pid"], signal.SIGKILL)
        second = self.assert_replaced(first)
        again = self.wait_state(
            lambda state: state["generation"] == second["generation"]
            and state.get("stage_affinity", {}).get("state") == "applied")
        self.assertNotEqual(again["stage_affinity"]["snapshot"],
                            applied["stage_affinity"]["snapshot"])
        self.assertEqual(json.loads(Path(again["stage_affinity"]["snapshot"]).read_text())["pid"],
                         second["engine_pid"])

    def test_stage_cpus_rejecting_cpu_zero_refuses_to_run(self):
        self.start_pair(extra_args=("--stage-cpus", "0,1"))
        self.assertEqual(self.process.wait(timeout=12), 2)
        self.assertIn("must exclude CPU 0",
                      (self.root / "supervisor.log").read_text())

    def test_unfilled_live_helper_never_becomes_attached_and_is_retried(self):
        self.start_pair({"FAKE_NO_FILL": "1"}, ("--startup-timeout", "1"))
        state = self.wait_state(lambda state: state["state"] == "backoff")
        self.assertNotIn("attached_at", state)
        self.assertIn("startup deadline", state["reason"])
        self.wait_state(lambda state: state["generation"] >= 2)

    def test_fast_failures_back_off_and_cap_delay(self):
        self.start_pair()
        generation = 1
        for expected in (0.15, 0.3, 0.3):
            before = self.attached(generation)
            os.kill(before["helper_pid"], signal.SIGKILL)
            state = self.wait_state(lambda state: state["generation"] == before["generation"]
                                    and state["state"] == "backoff")
            self.assertEqual(state["delay_s"], expected)
            generation = before["generation"] + 1

    def test_stop_during_backoff_publishes_stopped(self):
        self.start_pair(extra_args=("--backoff", "5", "--max-backoff", "5"))
        before = self.attached()
        os.kill(before["helper_pid"], signal.SIGKILL)
        self.wait_state(lambda state: state["state"] == "backoff")
        self.stop_pair()
        self.assertEqual(json.loads(self.state.read_text())["state"], "stopped")

    def test_duplicate_owner_does_not_touch_existing_pair(self):
        self.start_pair()
        before = self.attached()
        duplicate = subprocess.run(self.command, env=self.environment, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("Another pair supervisor", duplicate.stderr)
        self.assertIsNone(self.process.poll())
        self.assertEqual(json.loads(self.state.read_text())["helper_pid"], before["helper_pid"])


class OwnershipTests(ScratchTest):
    def test_replaced_socket_and_foreign_file_are_preserved(self):
        path = self.root / "sock"
        with socket.socket(socket.AF_UNIX) as original, socket.socket(socket.AF_UNIX) as replacement:
            original.bind(str(path))
            owned = {path: pair.socket_token(path)}
            path.unlink()
            replacement.bind(str(path))
            pair.release_sockets(owned)
            self.assertTrue(path.exists())
            path.unlink()
            path.write_text("foreign")
            pair.release_sockets(owned)
            self.assertEqual(path.read_text(), "foreign")

    def test_pid_start_token_is_required(self):
        self.assertFalse(pair.alive(os.getpid(), "not-this-start"))
        self.assertEqual(pair.descendants(os.getpid(), {os.getpid(): "old-start"}), {os.getpid(): "old-start"})

    def test_startup_refusal_does_not_retry(self):
        launcher = self.root / "refuse.sh"
        launcher.write_text("exit 3\n")
        state = self.root / "state.json"
        result = subprocess.run([sys.executable, str(SCRIPT), "run", "--launcher", str(launcher),
                                 "--state", str(state), "--lock", str(self.root / "lock"),
                                 "--interval", "0.02", "--stop-timeout", "0.1"],
                                capture_output=True, text=True, timeout=6)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(state.read_text())["state"], "failed")
        self.assertEqual(json.loads(state.read_text())["generation"], 1)

    def test_preexisting_socket_path_is_never_removed(self):
        path = self.root / "sock"
        path.write_text("foreign")
        result = subprocess.run([sys.executable, str(SCRIPT), "run", "--launcher", "/does/not/exist",
                                 "--state", str(self.root / "state"), "--lock", str(self.root / "lock"),
                                 "--socket", str(path)], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(), "foreign")

    def test_peer_that_dies_before_publication_still_records_retryable_startup(self):
        record = self.root / "record.json"
        result = subprocess.run([sys.executable, str(SCRIPT), "record", str(sys.maxsize), "0", "wrapper.log", "helper.log"],
                                env=dict(os.environ, STRATA_PAIR_RECORD=str(record)),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(record.read_text())["helper_start"], "")

    def test_foreign_peer_publication_is_rejected_without_signalling_it(self):
        foreign = subprocess.Popen(["sleep", "30"])
        try:
            launcher = self.root / "foreign.sh"
            launcher.write_text('exec "$FAKE_PY" "$PAIR_SUPERVISOR" record "$FOREIGN_PID" 0 wrapper.log helper.log\n')
            result = subprocess.run([sys.executable, str(SCRIPT), "run", "--launcher", str(launcher),
                                     "--state", str(self.root / "state"), "--lock", str(self.root / "lock"),
                                     "--interval", "0.02", "--stop-timeout", "0.1"],
                                    env=dict(os.environ, FAKE_PY=sys.executable, PAIR_SUPERVISOR=str(SCRIPT),
                                             FOREIGN_PID=str(foreign.pid)), capture_output=True, text=True, timeout=6)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("outside the owned group", result.stderr)
            self.assertIsNone(foreign.poll())
        finally:
            foreign.terminate()
            foreign.wait(timeout=5)

    def test_tail_skips_history_buffers_partial_lines_and_handles_truncation(self):
        path = self.root / "engine.log"
        path.write_text("old failure\n")
        tail = pair.LogTail(path)
        self.assertEqual(tail.read_lines(), [])
        with path.open("a") as log:
            log.write("helper connect ")
        self.assertEqual(tail.read_lines(), [])
        with path.open("a") as log:
            log.write("failed\n")
        self.assertEqual(tail.read_lines(), ["helper connect failed"])
        self.assertEqual(tail.read_lines(), [])
        path.write_text("new\n")
        self.assertEqual(tail.read_lines(), ["new"])

    def test_tail_reads_replaced_file_even_when_it_is_larger(self):
        path = self.root / "engine.log"
        path.write_text("old\n")
        tail = pair.LogTail(path)
        replacement = self.root / "new.log"
        replacement.write_text("new helper connect failed\n")
        replacement.replace(path)
        self.assertEqual(tail.read_lines(), ["new helper connect failed"])

    def test_zero_fill_and_listening_alone_are_not_readiness(self):
        for line in ("expert-helper: filled 0 slots (0.00 GiB)", "expert-helper: listening on one.sock",
                     "expert-helper: fill stopped: budget; device free now 8192 MiB (0 slots, 0.00 GiB uploaded)"):
            self.assertIsNone(pair.FILLED.search(line))

    def test_permanent_degradation_and_request_only_fallback_are_distinguished(self):
        self.assertTrue(pair.helper_degraded("expert-helper: reading the reply failed; closing the channel"))
        self.assertTrue(pair.helper_degraded("strata: prefill helper: the claimed rows fall back to the CPU"))
        self.assertTrue(pair.helper_degraded("strata generate: the expert helper is absent"))
        self.assertFalse(pair.helper_degraded("expert-helper: the late frame was drained - this request falls back, the channel stays open"))


class LauncherTests(ScratchTest):
    def test_shell_syntax(self):
        scripts = [*ROOT.glob("launch-prod-*-mixed*.sh"), ROOT / "tools/xxs-i8kv-cutover-test.sh", GUARD]
        for script in scripts:
            with self.subTest(script=script.name):
                result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_launchers_supervise_by_default_and_single_run_is_explicit(self):
        launchers = sorted(ROOT.glob("launch-prod-*-mixed*.sh"))
        self.assertEqual(len(launchers), 6)
        for launcher in launchers:
            for mode in ((), ("--recover",)):
                with self.subTest(launcher=launcher.name, mode=mode):
                    capture = self.root / "exec.txt"
                    capture.unlink(missing_ok=True)
                    result = subprocess.run(["bash", str(GUARD), str(launcher), *mode],
                                             env=dict(os.environ, PAIR_EXEC_CAPTURE=str(capture)),
                                             capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    args = capture.read_text().splitlines()
                    self.assertEqual(args[1:3], [str(SCRIPT), "run"])
                    self.assertEqual(args[args.index("--launcher") + 1], str(launcher))
                    self.assertEqual(args[args.index("--lock") + 1], str(ROOT / "run/mixed-supervisor.lock"))
                    self.assertIn("--config", args)
                    self.assertEqual(args[args.index("--nice") + 1], "1")
            for mode, expected in (("--single-run", 1), ("--pair-child", 2), ("--unknown", 2)):
                with self.subTest(launcher=launcher.name, mode=mode):
                    capture = self.root / "exec.txt"
                    capture.unlink(missing_ok=True)
                    environment = dict(os.environ, PAIR_EXEC_CAPTURE=str(capture))
                    environment.pop("STRATA_PAIR_RECORD", None)
                    result = subprocess.run(["bash", str(GUARD), str(launcher), mode], env=environment,
                                             capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertFalse(capture.exists())
                    if mode == "--single-run":
                        self.assertIn("port 8009 occupied", result.stderr)


if __name__ == "__main__":
    unittest.main()

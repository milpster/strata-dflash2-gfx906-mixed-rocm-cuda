"""Real CPU-process recovery tests; never use production processes or sockets."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


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

    def test_stale_helper_log_content_does_not_poison_startup(self):
        # The append-only port_ab helper log already holds a previous generation's
        # fill and end-of-channel marker when a new pair starts. Neither is this
        # generation's evidence: the boot must not be declared attached or ended
        # before its own helper writes.
        stale = self.root / "helper-port-ab.log"
        stale.write_text("expert-helper: filled 4093 slots (6.25 GiB)\n"
                         "expert-helper: second channel: request failed: helper timeout\n")
        self.start_pair({"FAKE_HELPER_LOG_NAME": stale.name, "FAKE_KEEP_HELPER_LOG": "1",
                         "FAKE_NO_FILL": "1", "FAKE_ENGINE_ACK_WITHOUT_FILL": "1"},
                        ("--startup-timeout", "1"))
        state = self.wait_state(lambda state: state["state"] == "backoff")
        self.assertEqual(state["generation"], 1)
        self.assertNotIn("attached_at", state)
        # Neither the stale marker ended generation 1 nor its fill reported it attached.
        self.assertEqual(state["reason"], "helper did not attach and fill before the startup deadline")

    def test_marker_in_the_next_generations_rotated_helper_log_ends_that_generation(self):
        # Production launchers name the helper log per generation, so generation 2
        # tails a different file than generation 1. The tail must follow the newly
        # published path; a marker written there is this generation's failure.
        self.start_pair()
        before = self.attached()
        os.kill(before["engine_pid"], signal.SIGKILL)
        second = self.attached(before["generation"] + 1)
        self.assertNotEqual(second["helper_log"], before["helper_log"])
        with Path(second["helper_log"]).open("a") as log:
            log.write("expert-helper: the engine said bye; exiting\n")
        state = self.wait_state(lambda state: state["state"] == "backoff")
        self.assertEqual(state["generation"], second["generation"])
        self.assertEqual(state["reason"], "helper channel ended")

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

    def test_frozen_read_ticker_restarts_the_pair(self):
        self.start_pair(extra_args=("--stall-timeout", "0.3"))
        self.attached()
        with (self.root / "server.log").open("a") as log:
            log.write("[strata] reading the prompt: 100 of 200 tokens, 10 s so far\n")
        stalled = self.wait_state(
            lambda state: state["state"] == "backoff"
            and "stalled at 100 of 200" in state.get("reason", ""))
        self.assertIn("stalled at 100 of 200", stalled["reason"])

    def test_completed_request_resets_the_stall_tracker(self):
        self.start_pair(extra_args=("--stall-timeout", "0.3"))
        self.attached()
        with (self.root / "server.log").open("a") as log:
            log.write("[strata] reading the prompt: 100 of 200 tokens, 10 s so far\n")
        time.sleep(0.15)
        with (self.root / "engine.log").open("a") as log:
            log.write("strata serve: prompt 100 tokens = 0 reused + 100 read in 10 ms, 1 generated in 5 ms\n")
        time.sleep(0.6)  # Idle after a finished request must never restart the pair.
        state = json.loads(self.state.read_text())
        self.assertEqual((state["generation"], state["state"]), (1, "attached"))
        with (self.root / "server.log").open("a") as log:  # A new request then stalls.
            log.write("[strata] reading the prompt: 0 of 50 tokens, 0 s so far\n")
        self.wait_state(lambda state: state["state"] == "backoff"
                        and "stalled at 0 of 50" in state.get("reason", ""))

    def test_generation_disarms_the_read_stall_tracker(self):
        self.start_pair(extra_args=("--stall-timeout", "0.3"))
        before = self.attached()
        for progress in (
            "thinking: 18282 of budget 49152 (max 65536)",
            "thinking: 18282 of max 65536",
            "answering: 18282 of max 65536",
            "writing a tool call: read: 18282 of max 65536",
            "tool call complete: 18282 of max 65536",
            "reading the prompt: 18282 of max 65536",
        ):
            with self.subTest(progress=progress):
                with (self.root / "server.log").open("a") as log:
                    log.write("[strata] reading the prompt: 98,075 of 98,076 tokens, 20 s so far\n")
                    log.write(f"[strata] {progress} tokens, 30.4 tok/s, 621 s\n")
                # No request-completion line: generation can outlive the read deadline.
                time.sleep(0.6)
                state = json.loads(self.state.read_text())
                self.assertIsNone(self.process.poll())
                self.assertEqual((state["generation"], state["state"]), (1, "attached"))
                for name in ("helper", "server", "engine"):
                    self.assertEqual(state[f"{name}_pid"], before[f"{name}_pid"])

    def test_read_after_generation_rearms_the_stall_tracker(self):
        self.start_pair(extra_args=("--stall-timeout", "0.3"))
        self.attached()
        with (self.root / "server.log").open("a") as log:
            log.write("[strata] reading the prompt: 98,075 of 98,076 tokens, 20 s so far\n")
            log.write("[strata] thinking: 31 of max 65536 tokens, 30.2 tok/s, 21 s\n")
            log.write("[strata] reading the prompt: 0 of 50 tokens, 0 s so far\n")
        self.wait_state(lambda state: state["state"] == "backoff"
                        and "stalled at 0 of 50" in state.get("reason", ""))

    def test_thinking_budget_preamble_does_not_disarm_read_stall_tracking(self):
        self.start_pair(extra_args=("--stall-timeout", "0.3"))
        self.attached()
        with (self.root / "server.log").open("a") as log:
            log.write("[strata] reading the prompt: 100 of 200 tokens, 10 s so far\n")
            log.write("[strata] thinking capped at 49152 of max_tokens 65536 to leave room for the answer\n")
        self.wait_state(lambda state: state["state"] == "backoff"
                        and "stalled at 100 of 200" in state.get("reason", ""))

    def test_progressing_read_ticker_does_not_restart(self):
        self.start_pair(extra_args=("--stall-timeout", "0.3"))
        self.attached()
        for count in (100, 150, 200):
            time.sleep(0.1)
            with (self.root / "server.log").open("a") as log:
                log.write(f"[strata] reading the prompt: {count} of 200 tokens, 1 s so far\n")
        time.sleep(0.15)
        with (self.root / "engine.log").open("a") as log:
            log.write("strata serve: prompt 200 tokens = 0 reused + 200 read in 10 ms, 1 generated in 5 ms\n")
        time.sleep(0.5)
        state = json.loads(self.state.read_text())
        self.assertEqual((state["generation"], state["state"]), (1, "attached"))

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


class StartupDeadlineTests(ScratchTest):
    """Deterministic startup-watchdog checks with a scripted clock and fake children.

    Reproduces the 2026-11-23 wedged-GPU incident without processes or wall time:
    the deadline counts from generation start and must fire on an unattached
    startup, must re-arm every generation, and must never false-fire on an
    attached pair; a stop request pre-empts a pending deadline; and a launcher
    that never published its peers is refused exactly once.
    """

    _leader_proc = None

    @classmethod
    def _leader(cls):
        if cls._leader_proc is None or cls._leader_proc.poll() is not None:
            cls._leader_proc = subprocess.Popen(["sleep", "600"], start_new_session=True)
        return cls._leader_proc

    @classmethod
    def tearDownClass(cls):
        if cls._leader_proc is not None:
            cls._leader_proc.terminate()
            cls._leader_proc.wait(timeout=5)
            cls._leader_proc = None

    class Launcher:
        """Fake subprocess child. Its pid is this (live, same-group) process, so the
        real ownership proofs hold; the signalling primitives are patched in the test."""

        def __init__(self, pid):
            self.pid = pid
            self.kills = 0

        def poll(self):
            return None  # Never exits on its own.

        def kill(self):
            self.kills += 1

        def wait(self, timeout=None):
            return 0

    def supervise_scripted(self, startup_timeout, record, fill_pass=None, stop_pass=None,
                           backoff=0.0):
        """Runs pair.supervise() on a scripted clock with fake children.

        Scripted time passes where wall time really passes: each stop.wait of the
        interval advances the clock by the interval. monotonic() only reads it —
        note the deadline check is short-circuited by "not healthy", so a check
        clock could never advance once the pair attached. fill_pass writes the
        helper's fill line during that wait (so the same pass evaluates it), and
        stop_pass makes that wait return "stop requested", i.e. the loop leaves
        before that pass runs. Nothing here touches a real process tree, signal
        disposition, socket, GPU, or wall clock.
        """
        state = self.root / "state.json"
        helper_log = self.root / "helper.log"
        helper_log.write_text("expert-helper: listening on one.sock\n")
        self.launchers = []
        self.published = []
        # supervise() installs SIGINT/SIGTERM handlers on the real process. Keep
        # this test's own disposition intact: capture the attempt, restore after.
        original_handlers = {sig: signal.getsignal(sig)
                             for sig in (signal.SIGINT, signal.SIGTERM)}
        # The launcher stand-in is a live, dedicated process-group leader (exactly
        # what start_new_session=True produces), so the supervisor's ownership
        # proofs (alive, start token, getpgid == launcher pid) hold against /proc.
        self.launcher_pid = self._leader().pid
        token = pair.process_token(self.launcher_pid)[1]
        # Both stand-in peers are the launcher's own pid: a live process in the
        # launcher's (== its own, start_new_session) process group, so the alive
        # and group proofs hold against the real /proc.
        record_peers = {"helper_pid": self.launcher_pid, "helper_start": token,
                        "server_pid": self.launcher_pid, "server_start": token,
                        "helper_log": str(helper_log), "engine_log": str(self.root / "engine.log")}

        def record_signal(signum, handler):
            return original_handlers[signum]  # Captured, never installed.

        def fake_popen(command, **kwargs):
            child = StartupDeadlineTests.Launcher(self.launcher_pid)
            self.launchers.append(child)
            if record is not None:  # Every launcher boot republishes its peers.
                record.write_text(json.dumps(record_peers))
            # A fresh launcher starts from an unattached baseline.
            helper_log.write_text("expert-helper: listening on one.sock\n")
            return child

        def fake_stop_group(child, timeout, known):
            child.kill()

        def fake_write_json(path, data):
            self.published.append({"state": data.get("state"), "generation": data.get("generation"),
                                   "reason": data.get("reason"), "state_path": str(path)})

        clock = {"now": 0.0, "calls": 0}

        def fake_monotonic():
            clock["calls"] += 1
            return clock["now"]  # Time passes in wait(); this is only a read.

        args = argparse.Namespace(
            launcher=self.root / "launch.sh", config=None, state=state,
            lock=self.root / "lock", socket=[], interval=1.0, stop_timeout=0.0,
            startup_timeout=startup_timeout, backoff=backoff, max_backoff=2.0, nice=0,
            stage_cpus=None, stall_timeout=0.0)
        workdir = self.root / "work"
        workdir.mkdir()

        waits = []
        WAIT_CAP = 100

        class FakeEvent:
            """Stops only when the script says so; a regression fails, never hangs."""

            def __init__(self):
                self._flag = False

            def is_set(self):
                return self._flag

            def set(self):
                self._flag = True

            def wait(self, timeout=None):
                if timeout == 0.0:  # Backoff release: no scripted interval passes.
                    return self._flag
                waits.append(timeout)
                if len(waits) > WAIT_CAP:
                    raise AssertionError(
                        f"scripted supervisor exceeded {WAIT_CAP} interval waits: "
                        f"states={[p['state'] for p in self_states]}")
                clock["now"] += timeout
                if fill_pass is not None and len(waits) == fill_pass:
                    helper_log.write_text("expert-helper: filled 4096 slots (7.0 GiB)\n")
                if stop_pass is not None and len(waits) >= stop_pass:
                    self._flag = True
                return self._flag

        self_states = self.published

        class FakeWorkdir:
            name = workdir

            def __enter__(self):
                return workdir

            def __exit__(self, *_exc):
                return False

        try:
            with mock.patch.object(pair.signal, "signal", side_effect=record_signal), \
                    mock.patch.object(pair.subprocess, "Popen", side_effect=fake_popen), \
                    mock.patch.object(pair, "enable_subreaper", return_value=None), \
                    mock.patch.object(pair, "stop_group", side_effect=fake_stop_group), \
                    mock.patch.object(pair, "write_json", side_effect=fake_write_json), \
                    mock.patch.object(pair.os, "kill"), \
                    mock.patch.object(pair.time, "monotonic", side_effect=fake_monotonic), \
                    mock.patch.object(pair.tempfile, "TemporaryDirectory", return_value=FakeWorkdir()):
                with mock.patch.object(pair.threading, "Event", FakeEvent):
                    code = pair.supervise(args)
        finally:
            for sig, handler in original_handlers.items():
                if signal.getsignal(sig) is not handler:
                    signal.signal(sig, handler)
        self.elapsed = clock["now"]
        self.clock_calls = clock["calls"]
        return code

    def test_deadline_retries_a_hung_startup_that_published_its_peers(self):
        # The wedged-device class: helper and server are alive and published, the
        # engine never attaches. With T = 2 s, pass k starts at elapsed k seconds,
        # so the pass-3 check (3.0 > 2.0) is the first expiry: the generation is
        # torn down and a fresh one starts. stop_pass 5 releases in gen 2 pass 2.
        code = self.supervise_scripted(2.0, self.root / "work" / "peers.json", stop_pass=5)
        self.assertEqual(code, 0)
        recovering = next(p for p in self.published
                          if p["state"] == "recovering" and p["generation"] == 1)
        self.assertIn("startup deadline", recovering["reason"])
        self.assertIn("backoff", [p["state"] for p in self.published])
        # Gen 2: 'starting', one 'monitoring' pass, then the stop releases.
        self.assertEqual([p["state"] for p in self.published if p["generation"] == 2],
                         ["starting", "monitoring", "stopping", "stopped"])
        self.assertEqual(len(self.launchers), 2)  # The hung generation was replaced.
        self.assertEqual(self.launchers[0].kills, 1)  # Its tree was stopped, not leaked.
        self.assertGreater(self.elapsed, 2.0)  # Expiry really passed before the stop.

    def test_unpublished_launcher_hitting_the_deadline_is_refused_once(self):
        # A launcher that never wrote its peers record is a configuration refusal,
        # not a wedged device: publish failed, do not spin new generations. The
        # bounded FakeEvent turns any future regression here into a failure.
        code = self.supervise_scripted(2.0, None)
        self.assertEqual(code, 1)
        failed = next(p for p in self.published if p["state"] == "failed")
        self.assertIn("startup deadline", failed["reason"])
        self.assertNotIn("backoff", [p["state"] for p in self.published])
        self.assertEqual(len(self.launchers), 1)
        self.assertEqual(self.launchers[0].kills, 1)
        self.assertLess(self.clock_calls, 20)  # Refusal is immediate, not a spin.

    def test_attached_startup_never_false_fires_the_deadline(self):
        # The fill lands during pass 2's wait, so pass 2 attaches. Passes 3-5 then
        # run attached at elapsed 3-5 s, past T = 2 s: the deadline is guarded by
        # "not healthy" (nothing re-arms per pass), so it must never fire.
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        code = self.supervise_scripted(2.0, self.root / "work" / "peers.json",
                                       fill_pass=2, stop_pass=6)
        self.assertEqual(code, 0)
        for sig, handler in before.items():
            self.assertIs(signal.getsignal(sig), handler)  # Real disposition restored.
        self.assertEqual([p["state"] for p in self.published],
                         ["starting", "monitoring", "attached", "stopping", "stopped"])
        self.assertFalse([p for p in self.published if p["reason"] and "deadline" in p["reason"]])
        self.assertGreater(self.elapsed, 2 * 2.0)  # Ran at least 2x past expiry attached.
        self.assertEqual(self.launchers[0].kills, 1)  # Shutdown still tears the tree down.

    def test_stop_before_the_deadline_reports_the_stop_not_a_timeout(self):
        # The 2026-11-23 manual stop: TERM landed ~25 s before the generation's
        # 1200 s deadline and the generation reported "stopping", never a timeout.
        # Here stop_pass 2 releases at the pass-2 entry (elapsed 2 s, T = 2 s), so
        # no check ever reads past the deadline and the reason stays "launcher
        # exited" — the stop pre-empts expiry.
        code = self.supervise_scripted(2.0, self.root / "work" / "peers.json", stop_pass=2)
        self.assertEqual(code, 0)
        self.assertEqual([p["state"] for p in self.published],
                         ["starting", "monitoring", "stopping", "stopped"])
        self.assertFalse([p for p in self.published if p["reason"] and "deadline" in p["reason"]])
        self.assertLess(self.elapsed, 4.0)  # Never a pass beyond T = 2 s.
        self.assertEqual(self.launchers[0].kills, 1)


class LauncherTests(ScratchTest):
    def test_shell_syntax(self):
        scripts = [*ROOT.glob("launch-prod-*-mixed*.sh"), ROOT / "tools/xxs-i8kv-cutover-test.sh", GUARD]
        for script in scripts:
            with self.subTest(script=script.name):
                result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_launchers_supervise_by_default_and_single_run_is_explicit(self):
        launchers = sorted(ROOT.glob("launch-prod-*-mixed*.sh"))
        self.assertEqual(len(launchers), 7)
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
                    if launcher.name == "launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh":
                        # The promoted prod tuple must keep the operator-set watchdog.
                        self.assertIn("--startup-timeout", args)
                        self.assertEqual(args[args.index("--startup-timeout") + 1], "900",
                                         "the promoted prod launcher must carry the 900 s watchdog")
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

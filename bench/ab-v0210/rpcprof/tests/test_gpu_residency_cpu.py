#!/usr/bin/env python3
"""Isolated stdlib tests: fake proc/sys/state/logs, no GPU, engine or endpoint."""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

RPC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RPC / "gpu-rounds"))
import watch_gpu_residency


class Fixture:
    def __init__(self, case):
        scratch = RPC / "out" / "tests-tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        case.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc = self.root / "proc"
        self.sys = self.root / "sys"
        self.state = self.root / "supervisor.json"
        self.log = self.root / "engine.log"
        self.wrapper = self.root / "wrapper.log"
        self.log.write_bytes(b"old request bytes\n")
        self.wrapper.write_text("WRONG-WRAPPER-LOG\n")
        entry = self.proc / "4242"
        entry.mkdir(parents=True)
        (entry / "stat").write_text("4242 (strata (engine) worker) S " + " ".join(["1"] * 18 + ["555"]) + "\n")
        (entry / "fdinfo").mkdir()
        self.fdinfo("7")
        device = self.sys / "class" / "drm" / "card2" / "device"
        device.mkdir(parents=True)
        (device / "uevent").write_text("PCI_SLOT_NAME=0000:04:00.0\n")
        for name, value in {"mem_info_vram_total": 17163091968, "mem_info_vram_used": 15669239808,
                            "mem_info_gtt_used": 1500643328, "gpu_busy_percent": 0}.items():
            (device / name).write_text(str(value) + "\n")
        connector = device.parent.parent / "card2-DP-1"
        connector.mkdir()
        (connector / "device").symlink_to(device)
        self.write_state()

    def write_state(self, **updates):
        state = {"supervisor_pid": 999, "generation": 1, "engine_pid": 4242, "engine_start": "555",
                 "engine_stderr_log": str(self.log), "engine_log": str(self.wrapper)}
        state.update(updates)
        self.state.write_text(json.dumps(state))

    def fdinfo(self, name, client="8", memory=True):
        text = f"drm-driver:\tamdgpu\ndrm-client-id:\t{client}\ndrm-pdev:\t0000:04:00.0\n"
        if memory:
            text += ("amd-requested-vram:\t16743428 KiB\namd-evicted-vram:\t1452032 KiB\n"
                     "drm-resident-vram:\t72 MiB\ndrm-resident-gtt:\t1454148 KiB\n")
        (self.proc / "4242" / "fdinfo" / name).write_text(text)
        return text

    def argv(self, out):
        return ["--out", str(out), "--duration", "0", "--state", str(self.state),
                "--proc-root", str(self.proc), "--sys-root", str(self.sys)]


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def wall(self):
        return 1795030000 + self.now


class ResidencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture(self)

    def test_units_missing_and_invalid(self):
        for raw, expected in (("1452032 KiB", 1452032 * 1024), ("72 MiB", 72 * 1024 ** 2),
                              ("1 GiB", 1024 ** 3), ("0", 0), ("10 B", 10)):
            self.assertEqual(watch_gpu_residency.parse_size(raw), expected)
        for raw in (None, "", "bad", "nan", "inf", "-1 KiB", "1 unknown"):
            self.assertIsNone(watch_gpu_residency.parse_size(raw))

    def test_selected_start_and_engine_stderr_not_wrapper(self):
        identity, valid, error = watch_gpu_residency.select_and_verify(self.fixture.state, self.fixture.proc)
        self.assertTrue(valid, error)
        self.assertEqual(identity["proc_start"], "555")
        self.assertEqual(identity["log_path"], str(self.fixture.log))
        self.assertNotEqual(identity["log_path"], str(self.fixture.wrapper))

    def test_invalid_state_and_recycled_pid(self):
        for update in ({"engine_start": "99"}, {"engine_start": None}, {"generation": None},
                       {"supervisor_pid": None}, {"engine_pid": True}, {"engine_stderr_log": None}):
            with self.subTest(update):
                self.fixture.write_state(**update)
                _, valid, error = watch_gpu_residency.select_and_verify(self.fixture.state, self.fixture.proc)
                self.assertFalse(valid)
                self.assertIsNotNone(error)
        self.fixture.state.write_text("[]")
        self.assertFalse(watch_gpu_residency.select_and_verify(self.fixture.state, self.fixture.proc)[1])
        self.fixture.state.unlink()
        self.assertFalse(watch_gpu_residency.select_and_verify(self.fixture.state, self.fixture.proc)[1])

    def test_full_identity_checked_around_sample(self):
        fixture = self.fixture

        class ChangingTailer(watch_gpu_residency.LogTailer):
            def read(self, path):
                fixture.write_state(generation=2)
                return super().read(path)

        tailer = ChangingTailer(fixture.root / "increment")
        sample = watch_gpu_residency.take_sample(lambda: 1795030000, 0, fixture.state,
                                                 fixture.proc, fixture.sys, tailer)
        self.assertFalse(sample["identity_valid"])
        self.assertIn("changed", sample["identity_error"])
        self.assertEqual(sample["identity"]["generation"], 1)
        self.assertEqual(sample["identity_after"]["generation"], 2)

    def test_dedup_retains_actual_raw_text(self):
        text = self.fixture.fdinfo("8")
        clients, raw, errors = watch_gpu_residency.read_fdinfo_clients(4242, self.fixture.proc)
        self.assertEqual(errors, [])
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0]["fds"], ["7", "8"])
        self.assertEqual(clients[0]["evicted_bytes"], 1452032 * 1024)
        self.assertEqual(clients[0]["resident_vram_bytes"], 72 * 1024 ** 2)
        self.assertEqual([record["text"] for record in raw], [text, text])

    def test_missing_memory_and_gpu_counters_are_not_zero(self):
        self.fixture.fdinfo("7", memory=False)
        clients, _, _ = watch_gpu_residency.read_fdinfo_clients(4242, self.fixture.proc)
        self.assertIsNone(clients[0]["evicted_bytes"])
        devices, errors = watch_gpu_residency.read_gpu_devices(self.fixture.sys)
        self.assertEqual(len(devices), 1, "connector linked to the GPU must not duplicate it")
        self.assertEqual(errors, [])
        self.assertEqual(devices[0]["pci"], "0000:04:00.0")
        self.assertEqual(devices[0]["busy_percent"], 0)
        (self.fixture.sys / "class/drm/card2/device/gpu_busy_percent").unlink()
        devices, errors = watch_gpu_residency.read_gpu_devices(self.fixture.sys)
        self.assertIsNone(devices[0]["busy_percent"])
        self.assertTrue(errors)


class LogTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture(self)
        self.increment = self.fixture.root / "increment"
        self.tailer = watch_gpu_residency.LogTailer(self.increment, max_read=10)

    def append(self, data):
        with self.fixture.log.open("ab") as stream:
            stream.write(data)

    def test_initial_exclusion_and_cap_preserves_backlog(self):
        initial = self.tailer.read(str(self.fixture.log))
        self.assertEqual(initial["new_bytes"], 0)
        self.append(b"x" * 25)
        first = self.tailer.read(str(self.fixture.log))
        second = self.tailer.read(str(self.fixture.log))
        third = self.tailer.read(str(self.fixture.log))
        self.assertEqual([first["new_bytes"], second["new_bytes"], third["new_bytes"]], [10, 10, 5])
        self.assertEqual([first["backlog_bytes"], second["backlog_bytes"], third["backlog_bytes"]], [15, 5, 0])
        self.assertEqual(self.increment.read_bytes(), b"x" * 25)

    def test_split_summary_and_nuls_preserved_in_raw(self):
        self.tailer.read(str(self.fixture.log))
        row = b"strata serve: prompt 1 tokens = 0 reused + 1 read, 1 generated (1 tok/s)\0\n"
        self.append(row)
        rows = []
        while True:
            record = self.tailer.read(str(self.fixture.log))
            rows.extend(record["prompt_lines"])
            if not record["backlog_bytes"]:
                break
        self.assertEqual(len(rows), 1)
        self.assertNotIn("\0", rows[0])
        self.assertEqual(self.increment.read_bytes(), row)

    def test_truncation_rotation_path_change_and_missing_log(self):
        self.tailer.read(str(self.fixture.log))
        self.fixture.log.write_bytes(b"short\n")
        record = self.tailer.read(str(self.fixture.log))
        self.assertEqual((record["reset"], record["from"]), ("truncate", 0))
        replacement = self.fixture.root / "replacement"
        replacement.write_bytes(b"new\n")
        os.replace(replacement, self.fixture.log)
        self.assertEqual(self.tailer.read(str(self.fixture.log))["reset"], "rotate")
        other = self.fixture.root / "other"
        other.write_bytes(b"path\n")
        self.assertEqual(self.tailer.read(str(other))["reset"], "path")
        self.fixture.log.unlink()
        self.assertIn("read", self.tailer.read(str(self.fixture.log))["error"])


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture(self)

    def test_duration_zero_actual_cli_smoke(self):
        out = self.fixture.root / "run"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(watch_gpu_residency.main(self.fixture.argv(out)), 0)
        samples = [json.loads(line) for line in (out / "samples.jsonl").read_text().splitlines()]
        self.assertEqual(len(samples), 1)
        self.assertTrue(samples[0]["identity_valid"])
        self.assertIsInstance(samples[0]["wall"], float)
        self.assertIn("T", samples[0]["observed_at"])
        self.assertEqual(samples[0]["log"]["source_path"], str(self.fixture.log))
        self.assertEqual((out / "engine-increment.log").read_bytes(), b"")
        self.assertEqual(json.loads((out / "summary.json").read_text())["outcome"], "completed")
        self.assertIn("done: samples=1", stdout.getvalue())

    def test_hour_schedule_and_nondivisible_final_tick_without_real_sleep(self):
        for duration, expected in ((3600, 121), (65, 4)):
            with self.subTest(duration):
                fake = FakeClock()
                out = self.fixture.root / str(duration)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(watch_gpu_residency.collect(
                        out, duration, 30, self.fixture.state, self.fixture.proc, self.fixture.sys,
                        monotonic=fake.monotonic, sleep=fake.sleep, wall=fake.wall), 0)
                rows = [json.loads(line) for line in (out / "samples.jsonl").read_text().splitlines()]
                self.assertEqual(len(rows), expected)
                self.assertEqual(rows[-1]["elapsed_s"], duration)
                self.assertEqual(fake.now, duration)

    def test_overslept_deadline_does_not_emit_catchup_samples(self):
        fake = FakeClock()

        def oversleep(seconds):
            fake.now += 200

        out = self.fixture.root / "overrun"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(watch_gpu_residency.collect(
                out, 65, 30, self.fixture.state, self.fixture.proc, self.fixture.sys,
                monotonic=fake.monotonic, sleep=oversleep, wall=fake.wall), 0)
        self.assertEqual(len((out / "samples.jsonl").read_text().splitlines()), 2)

    def test_existing_and_broken_symlink_output_refused_without_writes(self):
        out = self.fixture.root / "used"
        out.mkdir()
        (out / "keep").write_text("unchanged")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(watch_gpu_residency.main(self.fixture.argv(out)), 2)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["keep"])
        link = self.fixture.root / "broken"
        link.symlink_to(self.fixture.root / "absent")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(watch_gpu_residency.main(self.fixture.argv(link)), 2)
        self.assertFalse((self.fixture.root / "absent").exists())

    def test_invalid_or_nonfinite_timings_rejected_before_output(self):
        out = self.fixture.root / "never"
        for extra in (["--duration", "-1"], ["--duration", "nan"], ["--duration", "inf"],
                      ["--interval", "0"], ["--interval", "-1"], ["--interval", "nan"],
                      ["--interval", "inf"]):
            with self.subTest(extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    watch_gpu_residency.main(self.fixture.argv(out) + extra)
                self.assertEqual(caught.exception.code, 2)
                self.assertFalse(out.exists())

    def test_no_network_or_process_control_surface(self):
        tree = ast.parse(Path(watch_gpu_residency.__file__).read_text())
        allowed = {"__future__", "argparse", "datetime", "json", "math", "os", "sys", "time", "pathlib"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertTrue(all(alias.name in allowed for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertIn(node.module, allowed)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, {"system", "popen", "kill", "fork", "execv", "spawnv"})
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"eval", "exec", "__import__"})


if __name__ == "__main__":
    unittest.main(verbosity=2)

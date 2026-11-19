#!/usr/bin/env python3
"""CPU-only tests for the 2026-11-19 reserve-only panel. No GPU, no endpoint, no engine.

  python3 tests/test_reserve_panel_cpu.py         # stdlib only
  python3 -m unittest discover -s tests -v        # same thing

Everything mutable lives under out/tests-tmp (project-local, never the real
measurement tree). The frozen preparation artifacts of the real run are snapshotted at
import and re-checked at exit, so this suite can be proven not to have touched them.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

RPC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RPC))
sys.path.insert(0, str(RPC / "gpu-rounds"))
import ab9_driver as DRIVER  # noqa: E402
import champions_panel as PANEL  # noqa: E402
import champions_prepare as PREP  # noqa: E402
import reserve_panel as RP  # noqa: E402

SCRATCH = RPC / "out" / "tests-tmp"
CHAMPIONS_OUT = PREP.DEFAULT_OUT
# The real preparation artifacts this suite must read but never write.
PROTECTED = ("manifest.json", "manifest.sha256", "request-template.json",
             "configs/default_before.json", "configs/optimized.json",
             "configs/default_after.json",
             "bin/champs-default_before.sh", "bin/champs-optimized.sh",
             "bin/champs-default_after.sh")


def scratch_root(case: unittest.TestCase) -> Path:
    """Per-test scratch root, removed on teardown. Never the real benchmark tree."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=SCRATCH))
    case.addCleanup(shutil.rmtree, root, True)
    return root


def snapshot(out: Path) -> dict[str, str]:
    """stat + sha256 of the protected artifacts, read-only by construction."""
    marks: dict[str, str] = {}
    for relative in PROTECTED:
        path = out / relative
        if not path.is_file():
            marks[relative] = "<absent>"
            continue
        stat_ = path.stat()
        marks[relative] = (f"size={stat_.st_size} mtime={int(stat_.st_mtime)} "
                           f"sha256={hashlib.sha256(path.read_bytes()).hexdigest()}")
    return marks


@contextlib.contextmanager
def no_network():
    """No test here may open a socket. Import-time guard, not a fixture to forget."""
    import socket

    def blocked(*_a, **_k):
        raise AssertionError("a CPU test tried to open a socket")

    real_connect = socket.socket.connect
    socket.socket.connect = blocked
    try:
        yield
    finally:
        socket.socket.connect = real_connect


# --------------------------------------------------------------------- driver body


def _args(**overrides) -> argparse.Namespace:
    base = {"arm": "optimized", "leg": "short16k-p1", "kind": "tg", "ctx": 128,
            "max_tokens": 256, "out": "/tmp-unused", "elog": "/tmp-unused.e",
            "hlog": "/tmp-unused.h", "url": "http://127.0.0.1:8009/v1/chat/completions",
            "prompt_file": None, "prompt_sha256": None, "champs_shape": None,
            "champs_pair": None, "champs_measured": False, "champs_retry": 0,
            "request_template": None, "request_template_sha256": None}
    return argparse.Namespace(**{**base, **overrides})


class RequestBody(unittest.TestCase):
    LEGACY = {"messages", "max_tokens", "temperature", "enable_thinking",
              "reasoning_effort", "stream"}

    def test_legacy_body_is_unchanged_when_no_template_is_given(self):
        body = DRIVER.build_body("hello", 512, None)
        self.assertEqual(body, {
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 512, "temperature": 0, "enable_thinking": False,
            "reasoning_effort": "low", "stream": False,
        })
        self.assertEqual(set(body), self.LEGACY)

    def test_template_pins_survive_and_the_driver_owns_prompt_and_cap(self):
        template = dict(RP.REQUEST_TEMPLATE)
        body = DRIVER.build_body("hello", 256, template)
        for key, value in template.items():
            self.assertEqual(body[key], value)
        self.assertEqual(body["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(body["max_tokens"], 256)
        # The parity defaults the driver used to force are gone, not merged in.
        self.assertNotIn("enable_thinking", body)
        self.assertEqual(body["temperature"], 1.0)
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertNotIn("messages", template, "the driver must not write into the template")

    def test_template_file_that_pins_the_drivers_fields_is_refused(self):
        root = scratch_root(self)
        path = root / "template.json"
        path.write_text(json.dumps({"max_tokens": 5}))
        with self.assertRaises(SystemExit) as caught:
            DRIVER.load_template(_args(request_template=str(path)))
        self.assertIn("max_tokens", str(caught.exception))

    def test_template_digest_mismatch_is_refused(self):
        root = scratch_root(self)
        path = root / "template.json"
        path.write_text(json.dumps(RP.REQUEST_TEMPLATE))
        with self.assertRaises(SystemExit) as caught:
            DRIVER.load_template(_args(request_template=str(path),
                                       request_template_sha256="0" * 64))
        self.assertIn("sha256", str(caught.exception))

    def test_non_object_template_is_refused(self):
        root = scratch_root(self)
        path = root / "template.json"
        path.write_text("[1, 2]")
        with self.assertRaises(SystemExit):
            DRIVER.load_template(_args(request_template=str(path)))


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class DriverEndToEnd(unittest.TestCase):
    """Runs the real main() against a stub transport: prompt file in, record out."""

    def setUp(self):
        self.root = scratch_root(self)
        self.records = self.root / "records"
        self.prompt = self.root / "p.bin"
        self.prompt.write_bytes(b"x" * 128)
        self.sent: list[dict] = []
        real_urlopen, real_find = urllib.request.urlopen, DRIVER.find_engine_pid

        def fake_urlopen(request, timeout=None, **_k):
            # find_engine_pid would otherwise reach into the serving engine's /proc entry,
            # and MemSampler writes to clear_refs: no test may touch a live pair.
            self.sent.append(json.loads(request.data.decode()))
            return _FakeResponse({"choices": [{"message": {"content": "OK"}}],
                                  "model": "ud-iq3_xxs"})

        urllib.request.urlopen = fake_urlopen
        DRIVER.find_engine_pid = lambda: None
        self.addCleanup(setattr, urllib.request, "urlopen", real_urlopen)
        self.addCleanup(setattr, DRIVER, "find_engine_pid", real_find)

    def _run(self, argv: list[str]) -> dict:
        real_argv = sys.argv
        sys.argv = ["ab9_driver.py", *argv]
        try:
            self.assertEqual(DRIVER.main(), 0)
        finally:
            sys.argv = real_argv
        files = sorted(self.records.glob("*.json"))
        self.assertEqual(len(files), 1, files)
        return json.loads(files[0].read_text())

    def _base(self) -> list[str]:
        return ["--arm", "optimized", "--leg", "short16k-p1", "--kind", "tg",
                "--ctx", "128", "--max-tokens", "256", "--out", str(self.records),
                "--elog", str(self.root / "engine.log"), "--hlog", str(self.root / "helper.log"),
                "--prompt-file", str(self.prompt)]

    def test_legacy_record_carries_no_request_section_and_the_parity_body(self):
        record = self._run(self._base())
        self.assertNotIn("request", record)
        self.assertEqual(set(self.sent[0]), RequestBody.LEGACY)
        self.assertEqual(self.sent[0]["temperature"], 0)
        self.assertEqual(len(record["requests"]), 2, "kind=tg is fresh + cached")

    def test_template_leg_records_the_template_hash_and_the_effective_settings(self):
        template_path = self.root / "request-template.json"
        digest = hashlib.sha256(json.dumps(RP.REQUEST_TEMPLATE, indent=1,
                                          sort_keys=True).encode()).hexdigest()
        template_path.write_text(json.dumps(RP.REQUEST_TEMPLATE, indent=1, sort_keys=True))
        record = self._run(self._base() + ["--request-template", str(template_path),
                                           "--request-template-sha256", digest])
        request = record["request"]
        self.assertEqual(request["template_sha256"], digest)
        self.assertEqual(request["settings"]["temperature"], 1.0)
        self.assertEqual(request["settings"]["reasoning_effort"], "high")
        self.assertEqual(request["settings"]["max_tokens"], 256)
        self.assertNotIn("enable_thinking", request["settings"])
        self.assertEqual(request["settings"]["messages"],
                         [{"content_sha256": record["prompt_sha256"], "role": "user"}])
        self.assertEqual(hashlib.sha256(json.dumps(request["settings"], sort_keys=True)
                                       .encode()).hexdigest(), request["settings_sha256"])
        for entry in record["requests"]:
            self.assertEqual(entry["model"], "ud-iq3_xxs")
        # Both requests of the pair were sent with the same pinned settings.
        self.assertEqual([body["temperature"] for body in self.sent], [1.0, 1.0])


# ---------------------------------------------------------------- preparation


class ReservePreparation(unittest.TestCase):
    """The real --prepare, run for real into scratch: hashes, git blobs, corpus."""

    @classmethod
    def setUpClass(cls):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        cls.out = Path(tempfile.mkdtemp(dir=SCRATCH)) / "reserve"
        cls.real_out = RP.DEFAULT_OUT
        cls.protected_before = snapshot(cls.real_out)
        RP.prepare(cls.out)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out.parent, ignore_errors=True)

    def setUp(self):
        self.assertEqual(snapshot(self.real_out), self.protected_before,
                         "the suite must not write the real preparation artifacts")
        self.manifest = json.loads((self.out / "manifest.json").read_text())

    def test_every_arm_is_the_same_bytes(self):
        for name, spec in self.manifest["arms"].items():
            self.assertEqual(spec["engine"]["sha256"], PREP.CANONICAL_ENGINE_SHA, name)
            self.assertEqual(spec["helper"]["sha256"], PREP.HELPER_SHA, name)
            self.assertEqual(spec["server"]["sha256"], PREP.SERVER_SHA, name)
            link = os.path.join(spec["slot"], "rpcw-hip", "strata")
            self.assertEqual(Path(os.readlink(link)), PREP.CANONICAL_ENGINE, name)
        self.assertTrue(self.manifest["effective_deltas"]["engine_binary_identical"])

    def test_only_the_reserves_move_once_the_harness_paths_are_blanked(self):
        settings = {name: PREP.settings_only(json.loads(Path(spec["frozen_config"]["path"]).read_text()))
                    for name, spec in self.manifest["arms"].items()}
        diffs = PREP.cross_arm_setting_diffs(settings)
        self.assertEqual({RP._reserve_flag(field) for field in diffs}, set(PREP.RESERVE_FLAGS),
                         f"unexpected difference: {diffs}")
        for flag, by_arm in self.manifest["profile_delta"].items():
            self.assertEqual(by_arm, {"default_before": "700", "optimized": "256"
                                      if flag == "--vram-reserve-mib" else "448",
                                      "default_after": "700"})

    def test_quality_bearing_keys_and_capacity_are_frozen(self):
        for name, spec in self.manifest["arms"].items():
            keys = spec["quality_identity"]
            self.assertEqual(keys["--max-context"], "262144", name)
            self.assertEqual(keys["--kv"], "int8", name)
            self.assertEqual(keys["--conversation-cache-disk-mib"], "24576", name)
            self.assertEqual(keys["--expert-helper-slots"], "4600", name)
        self.assertEqual(self.manifest["quality_key_deltas"], {})

    def test_plan_text_never_claims_an_engine_difference(self):
        text = PREP.plan_text(self.out)
        self.assertIn("engine binary: identical in all 3 arms", text)
        self.assertNotIn("only the engine binary differs", text)
        self.assertIn("LEVER --vram-reserve-mib", text)
        self.assertIn("request template:", text)
        self.assertIn(PREP.CANONICAL_ENGINE_SHA[:16], text)

    def test_sample_plan_is_one_shape_and_twenty_four_requests(self):
        shapes = self.manifest["shapes"]
        self.assertEqual([s["name"] for s in shapes], ["short16k"])
        shape = shapes[0]
        self.assertEqual((shape["ctx_bytes"], shape["max_tokens"], shape["warmup_pairs"],
                          shape["measured_pairs"], shape["request_timeout_s"]),
                         (88400, 256, 1, 3, 2400))
        corpus = self.manifest["corpus"]
        self.assertEqual(len(corpus), 4)
        self.assertEqual(len({entry["head_tag"] for entry in corpus}), 4)
        self.assertEqual(sum(1 for entry in corpus if entry["measured"]), 3)
        for entry in corpus:
            blob = Path(entry["path"]).read_bytes()
            self.assertEqual(len(blob), 88400)
            self.assertEqual(hashlib.sha256(blob).hexdigest(), entry["sha256"])
            self.assertEqual(blob, PREP.build_paired_prompt(entry["shape"], entry["pair"], 88400))

    def test_verify_reports_a_reserve_that_never_reached_the_config(self):
        """The known defect class: the setting is declared but the engine never sees it."""
        name, spec = "optimized", self.manifest["arms"]["optimized"]
        path = Path(spec["frozen_config"]["path"])
        original = path.read_bytes()
        self.addCleanup(path.write_bytes, original)
        broken = json.loads(original)
        broken["args"][broken["args"].index("--vram-reserve-mib") + 1] = "512"
        path.write_text(json.dumps(broken, indent=1, sort_keys=True) + "\n")
        problems = [problem for problem in PREP.verify(self.out) if "lever" in problem]
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("--vram-reserve-mib", problems[0])

    def test_prepare_refuses_an_override_the_frozen_config_does_not_carry(self):
        arm = dataclasses.replace(RP.ARMS[0],
                                  arg_overrides=(("--vram-reserve-not-a-flag", "7"),))
        with self.assertRaises(SystemExit) as caught:
            PREP.retarget_config(PREP.git_blob(RP.BASE_ARM.config_ref), arm, self.out)
        self.assertIn("not an argument", str(caught.exception))

    def test_prepare_refuses_a_template_that_steals_the_corpus_or_the_cap(self):
        with self.assertRaises(SystemExit):
            PREP.write_request_template(self.out, {"max_tokens": 5})

    def test_request_template_is_frozen_pinned_and_matches_production(self):
        entry = self.manifest["request_template"]
        path = Path(entry["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(PREP.sha256_path(path), entry["sha256"])
        self.assertEqual(entry["settings"], RP.REQUEST_TEMPLATE)
        live = json.loads(PREP.PROD_CONFIG.read_text())
        for key, value in live["sampling"].items():
            self.assertEqual(entry["settings"][key], value, key)
        self.assertEqual(entry["settings"]["reasoning_effort"], live["reasoning_effort"])
        self.assertEqual(entry["settings"]["model"], live["model_name"])
        self.assertNotIn("enable_thinking", entry["settings"])

    def test_restore_pin_is_the_active_production_stack(self):
        restore = self.manifest["restore"]
        self.assertEqual(restore["config_sha256"], PREP.sha256_path(PREP.PROD_CONFIG))
        self.assertEqual(restore["config_sha256"], RP.ACTIVE_CONFIG_SHA256)
        self.assertEqual(restore["launcher_sha256"], RP.ACTIVE_LAUNCHER_SHA256)
        self.assertEqual(restore["engine_sha"], PREP.CANONICAL_ENGINE_SHA)
        self.assertEqual(restore["supervisor_state"], str(PREP.ROOT / PREP.SUPERVISOR_STATE))
        self.assertEqual((restore["timeout_s"], restore["poll_s"]), (1800, 5))
        for name, spec in self.manifest["arms"].items():
            self.assertEqual(spec["config_snapshot"]["blob"], RP.CONFIG_BLOB, name)
            self.assertEqual(spec["launcher_snapshot"]["blob"], RP.LAUNCHER_BLOB, name)

    def test_launchers_avoid_the_stop_all_sweep_and_keep_the_pair_state(self):
        for name in RP.RESERVE_ORDER:
            path = self.out / "bin" / f"champs-{name}.sh"
            text = path.read_text()
            for part in PREP.FORBIDDEN_NAME_PARTS:
                self.assertNotIn(part, path.name)
            self.assertIn(f'BIN="{self.out / "slots" / name}"', text)
            self.assertIn('--state "$ROOT/run/prod-swift15-f16kv-mixed-supervisor.json"', text)
            self.assertIn('--lock "$ROOT/run/mixed-supervisor.lock"', text)

    def test_launchers_are_identical_once_the_arm_name_is_blanked(self):
        """Same launcher text per arm: the lever lives in the config, never in a flag.

        A reserve that reached a launcher would move it out of the frozen snapshot and
        make the arms differ in two places at once.
        """
        def normalised(name: str) -> str:
            return (self.out / "bin" / f"champs-{name}.sh").read_text().replace(name, "<arm>")

        texts = {name: normalised(name) for name in RP.RESERVE_ORDER}
        self.assertEqual(len(set(texts.values())), 1,
                         "the three arms must boot the same launcher apart from paths")
        for name, text in texts.items():
            self.assertNotIn("--vram-reserve", text, name)
            self.assertIn('"$PY" "$WORK/serve/server.py" --engine strata', text, name)


class ProfileChecks(unittest.TestCase):
    """profile_problems is the gate in front of --run: it has to be able to fail."""

    def setUp(self):
        self.root = scratch_root(self)
        self.out = self.root / "reserve"
        RP.prepare(self.out)
        # profile_problems resolves everything from absolute paths in the manifest, so a
        # tampered manifest alone is enough to exercise it without copying evidence.
        self.tampered = self.root / "tampered"
        self.tampered.mkdir()
        shutil.copy(self.out / "manifest.json", self.tampered / "manifest.json")

    def test_a_clean_profile_passes(self):
        self.assertEqual(RP.profile_problems(self.out), [])

    def test_swapping_the_drift_bracket_out_of_last_position_is_refused(self):
        manifest = json.loads((self.tampered / "manifest.json").read_text())
        manifest["arm_order"] = ["default_before", "default_after", "optimized"]
        (self.tampered / "manifest.json").write_text(json.dumps(manifest))
        problems = RP.profile_problems(self.tampered)
        self.assertTrue(any("drift bracket" in problem for problem in problems), problems)

    def test_an_arm_that_differs_outside_the_lever_is_refused(self):
        manifest = json.loads((self.tampered / "manifest.json").read_text())
        spec = manifest["arms"]["optimized"]
        path = Path(spec["frozen_config"]["path"])
        original = path.read_bytes()
        self.addCleanup(path.write_bytes, original)
        broken = json.loads(original)
        broken["args"][broken["args"].index("--kv-resident") + 1] = "131072"
        path.write_text(json.dumps(broken, indent=1, sort_keys=True) + "\n")
        problems = RP.profile_problems(self.tampered)
        self.assertTrue(any("outside the lever" in problem for problem in problems), problems)

    def test_a_lever_that_does_not_actually_vary_is_refused(self):
        manifest = json.loads((self.tampered / "manifest.json").read_text())
        for spec in manifest["arms"].values():
            spec["profile_arg_overrides"]["--vram-reserve-mib"] = "700"
        (self.tampered / "manifest.json").write_text(json.dumps(manifest))
        problems = RP.profile_problems(self.tampered)
        self.assertTrue(any("does not vary" in problem for problem in problems), problems)

    def test_a_template_drift_is_refused(self):
        entry = json.loads((self.tampered / "manifest.json").read_text())["request_template"]
        Path(entry["path"]).write_text(json.dumps({"temperature": 0.7}))
        problems = RP.profile_problems(self.tampered)
        self.assertTrue(any("request template" in problem for problem in problems), problems)

    def test_production_moving_since_the_freeze_is_refused(self):
        """The baseline the comparison is read against must still be the baseline."""
        real_config = PREP.PROD_CONFIG
        try:
            moved = self.root / "prod-config-moved.json"
            moved.write_text(json.dumps({"sampling": {}, "args": []}))
            PREP.PROD_CONFIG = moved
            problems = RP.profile_problems(self.out)
        finally:
            PREP.PROD_CONFIG = real_config
        self.assertTrue(any("not the baseline" in problem for problem in problems), problems)

    def test_sampling_that_is_no_longer_production_s_is_refused(self):
        real_config = PREP.PROD_CONFIG
        try:
            moved = self.root / "prod-config-sampling.json"
            moved.write_text(json.dumps({"sampling": {"temperature": 0.2},
                                         "reasoning_effort": "low", "args": []}))
            PREP.PROD_CONFIG = moved
            problems = RP.profile_problems(self.out)
        finally:
            PREP.PROD_CONFIG = real_config
        self.assertTrue(any("sampling" in problem for problem in problems), problems)
        self.assertTrue(any("reasoning_effort" in problem for problem in problems), problems)


# ------------------------------------------------------------------ runner wiring


def restore_stub(scratch: Path, exit_code: int = 0) -> Path:
    """A stand-in for champions_restore.sh that records the arguments it was given."""
    script = scratch / "restore-stub.sh"
    log = scratch / "restore-calls.log"
    script.write_text(f"""#!/usr/bin/env bash
printf '%s\\n' "$0 $*" >> "{log}"
echo "restore-stub: $*"
exit {exit_code}
""")
    script.chmod(0o755)
    return script


class RestoreArguments(unittest.TestCase):
    def setUp(self):
        self.root = scratch_root(self)
        self.out = self.root / "reserve"
        RP.prepare(self.out)
        self.real_restore = PANEL.RESTORE
        self.stub = restore_stub(self.root)
        PANEL.RESTORE = self.stub
        self.addCleanup(setattr, PANEL, "RESTORE", self.real_restore)
        self.log = self.root / "restore-calls.log"

    def _calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.is_file() else []

    def test_restore_gets_a_fresh_out_dir_and_the_pinned_engine(self):
        panel = PANEL.Panel(self.out, health=True)
        self.assertTrue(panel.restore())
        self.assertTrue(panel.restore(), "restore is exactly once per session")
        calls = self._calls()
        self.assertEqual(len(calls), 1, calls)
        argv = calls[0].split()
        self.assertEqual(argv[0], str(self.stub))
        self.assertIn("--engine-sha", argv)
        self.assertEqual(argv[argv.index("--engine-sha") + 1], PREP.CANONICAL_ENGINE_SHA)
        self.assertEqual(argv[argv.index("--timeout") + 1], "1800")
        self.assertEqual(argv[argv.index("--poll") + 1], "5")
        self.assertIn("--health", argv)
        first_out = Path(argv[argv.index("--out") + 1])
        self.assertTrue(first_out.is_dir())
        self.assertEqual(first_out.parent, self.out / "restore")
        # A second session must not reuse it: a restore's logs are not measurement data.
        PANEL.Panel(self.out, health=False).restore()
        argv2 = self._calls()[1].split()
        self.assertNotEqual(argv2[argv2.index("--out") + 1], str(first_out))
        self.assertNotIn("--health", argv2)

    def test_a_manifest_without_a_restore_pin_still_gets_a_fresh_out_dir(self):
        """The champions manifest predates the pin; the fix still has to apply there."""
        legacy = self.root / "champions"
        legacy.mkdir()
        manifest = json.loads((CHAMPIONS_OUT / "manifest.json").read_text())
        manifest.pop("restore", None)
        for spec in manifest["arms"].values():
            spec["engine_log"] = str(self.out / "logs" / Path(spec["engine_log"]).name)
        (legacy / "manifest.json").write_text(json.dumps(manifest))
        panel = PANEL.Panel(legacy, health=False)
        panel.restore()
        argv = self._calls()[0].split()
        self.assertIn("--out", argv)
        self.assertEqual(Path(argv[argv.index("--out") + 1]).parent, legacy / "restore")
        self.assertNotIn("--engine-sha", argv)
        self.assertNotIn("--timeout", argv)

    def test_a_failing_restore_is_reported_as_a_failure(self):
        PANEL.RESTORE = restore_stub(self.root, exit_code=1)
        self.assertFalse(PANEL.Panel(self.out, health=False).restore())


class RunnerBehaviour(unittest.TestCase):
    """run() is the parent's entry point: it gates, restores always, and never says ok on
    a failed restore. Boot is stubbed; no test here starts anything."""

    def setUp(self):
        self.root = scratch_root(self)
        self.out = self.root / "reserve"
        RP.prepare(self.out)
        self.real = {name: getattr(RP.ReservePanel, name)
                     for name in ("identity", "snapshot_residency", "run_arm", "capture_runtime",
                                  "capture_capacity", "boot")}
        self.addCleanup(lambda: setattr_chain(RP.ReservePanel, self.real))
        RP.ReservePanel.identity = lambda self, arm, phase: "token"
        RP.ReservePanel.snapshot_residency = lambda self, arm, phase: None
        RP.ReservePanel.capture_runtime = lambda self, arm, state, phase: {}
        RP.ReservePanel.capture_capacity = lambda self, arm, record: 0
        self.calls: list[str] = []

    def test_run_refuses_a_frozen_input_that_drifted(self):
        real_verify = PREP.verify
        try:
            PREP.verify = lambda out: ["engine sha drift"]
            code = RP.run(self.out, list(RP.RESERVE_ORDER), health=False)
        finally:
            PREP.verify = real_verify
        self.assertEqual(code, 2)

    def test_run_refuses_when_the_profile_is_not_the_narrow_one(self):
        real = RP.profile_problems
        try:
            RP.profile_problems = lambda out: ["arms differ outside the lever"]
            code = RP.run(self.out, list(RP.RESERVE_ORDER), health=False)
        finally:
            RP.profile_problems = real
        self.assertEqual(code, 2)

    def test_a_failed_restore_never_reports_success(self):
        seen: list[str] = []
        RP.ReservePanel.run_arm = lambda self, arm: seen.append(arm) or True
        real_restore = RP.PANEL.Panel.restore
        try:
            RP.PANEL.Panel.restore = lambda self: False
            code, printed = capture_stdout(RP.run, self.out, list(RP.RESERVE_ORDER), False)
        finally:
            RP.PANEL.Panel.restore = real_restore
        self.assertEqual(code, 1)
        self.assertIn("RESTORE FAILED", printed)
        self.assertEqual(seen, list(RP.RESERVE_ORDER))

    def test_restore_runs_even_when_the_panel_is_interrupted(self):
        def explode(self, arm):
            raise KeyboardInterrupt()

        RP.ReservePanel.run_arm = explode
        restores: list[bool] = []
        real_restore = RP.PANEL.Panel.restore
        try:
            def fake_restore(self):
                restores.append(True)
                return True
            RP.PANEL.Panel.restore = fake_restore
            code, printed = capture_stdout(RP.run, self.out, list(RP.RESERVE_ORDER), False)
        finally:
            RP.PANEL.Panel.restore = real_restore
        self.assertEqual(restores, [True])
        self.assertEqual(code, 1)
        self.assertIn("aborted", printed)

    def test_run_order_comes_from_the_manifest_not_the_champion_order(self):
        seen: list[str] = []
        RP.ReservePanel.run_arm = lambda self, arm: seen.append(arm) or True
        real_restore = RP.PANEL.Panel.restore
        real_verify, real_profile = PREP.verify, RP.profile_problems
        try:
            PREP.verify = lambda out: []
            RP.profile_problems = lambda out: []
            RP.PANEL.Panel.restore = lambda self: True
            _, printed = capture_stdout(RP.run, self.out, list(RP.RESERVE_ORDER), False)
        finally:
            PREP.verify, RP.profile_problems, RP.PANEL.Panel.restore = (
                real_verify, real_profile, real_restore)
        self.assertEqual(seen, ["default_before", "optimized", "default_after"])
        self.assertNotIn("current", printed)
        self.assertNotIn("nov16", printed)


def setattr_chain(owner: type, values: dict) -> None:
    for name, value in values.items():
        setattr(owner, name, value)


def capture_stdout(function, *args, **kwargs):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = function(*args, **kwargs)
    return code, buffer.getvalue()


class ArmEvidence(unittest.TestCase):
    """Identity, residency and runtime capture: wiring only, no engine, no endpoint."""

    def setUp(self):
        self.root = scratch_root(self)
        self.out = self.root / "reserve"
        RP.prepare(self.out)
        self.panel = RP.ReservePanel(self.out, health=False)
        self.arm = "optimized"
        self.panel.arm_dir(self.arm).mkdir(parents=True, exist_ok=True)

    def test_identity_asks_the_restore_verifier_about_the_canonical_engine(self):
        real_run = subprocess.run
        seen: list[list[str]] = []

        def fake_run(command, capture_output=False, text=False):
            seen.append([str(part) for part in command])
            return subprocess.CompletedProcess(command, 0,
                                               "IDENTITY-OK\ntoken=1:111:222:/log\npid=111\n"
                                               "generation=1\nlog=/log\n", "")

        subprocess.run = fake_run
        self.addCleanup(setattr, subprocess, "run", real_run)
        (self.panel.arm_dir(self.arm) / "supervisor.out").write_text("announced\n")
        first = self.panel.identity(self.arm, "boot")
        second = self.panel.identity(self.arm, "short16k-p1-before")
        self.assertEqual((first, second), ("1:111:222:/log", "1:111:222:/log"))
        argv = seen[0]
        self.assertEqual(argv[0:2], [sys.executable, str(RP.VERIFY)])
        self.assertEqual(argv[2], "identity")
        self.assertEqual(argv[argv.index("--state") + 1],
                         str(PREP.ROOT / PREP.SUPERVISOR_STATE))
        self.assertEqual(argv[argv.index("--engine") + 1], str(PREP.CANONICAL_ENGINE))
        self.assertEqual(argv[argv.index("--expected-sha") + 1], PREP.CANONICAL_ENGINE_SHA)
        self.assertEqual(argv[argv.index("--log") + 1],
                         str(self.panel.arm_dir(self.arm) / "supervisor.out"))
        self.assertNotIn("--expect", argv, "the first check has no token to hold to")
        self.assertIn("--expect", seen[1])
        self.assertEqual(seen[1][seen[1].index("--expect") + 1], "1:111:222:/log")
        records = [json.loads(line) for line in
                   (self.panel.arm_dir(self.arm) / "identity.jsonl").read_text().splitlines()]
        self.assertEqual([record["phase"] for record in records],
                         ["boot", "short16k-p1-before"])

    def test_a_rejected_identity_is_not_silently_a_token(self):
        real_run = subprocess.run
        subprocess.run = lambda command, **_k: subprocess.CompletedProcess(command, 3,
                                                                          "IDENTITY-FAIL nope\n", "")
        self.addCleanup(setattr, subprocess, "run", real_run)
        capture_stdout(self.panel.identity, self.arm, "boot")
        self.assertEqual(self.panel.tokens.get(self.arm, ""), "")

    def test_a_generation_change_discards_the_pair_around_it(self):
        tokens = iter(["t1", "t1", ""])
        self.panel.identity = lambda arm, phase: next(tokens)  # type: ignore[assignment]
        self.panel.snapshot_residency = lambda arm, phase: None  # type: ignore[assignment]
        real_pair = PANEL.Panel.run_pair
        PANEL.Panel.run_pair = lambda self, arm, shape, pair: "pair-ok"
        self.addCleanup(setattr, PANEL.Panel, "run_pair", real_pair)
        shape = self.panel.manifest["shapes"][0]
        self.assertEqual(self.panel.run_pair(self.arm, shape, 1), "pair-ok")
        self.assertIsNone(self.panel.run_pair(self.arm, shape, 2))

    def test_runtime_capture_notices_a_reserve_the_engine_never_got(self):
        """Reads /proc of THIS process as a harmless real target: the guard is the point."""
        spec = self.panel.manifest["arms"][self.arm]
        spec["profile_arg_overrides"] = {"--vram-reserve-mib": "256",
                                         "--vram-reserve-later-mib": "448"}
        state = {"engine_pid": os.getpid(), "engine_start": "not-the-start",
                 "helper_pid": None, "server_pid": None, "generation": 1,
                 "engine_log": "wrapper.log", "engine_stderr_log": spec["engine_log"]}
        _, printed = capture_stdout(self.panel.capture_runtime, self.arm, state, "test")
        runtime = json.loads((self.panel.arm_dir(self.arm) / "runtime.json").read_text())
        self.assertTrue(runtime["reserve_mismatch"],
                        "this process was not started with those reserves")
        self.assertIn("argv", printed)
        self.assertIsNone(runtime["reserve_args_seen"]["--vram-reserve-mib"])
        self.assertEqual(runtime["supervisor_state"]["generation"], 1)

    def test_a_supervisor_stderr_log_that_is_not_this_arms_log_fails_the_boot(self):
        arm = self.arm
        spec = self.panel.manifest["arms"][arm]
        real_boot = PANEL.Panel.boot
        real_state = RP.ReservePanel.supervisor_state
        PANEL.Panel.boot = lambda self, name: {"arm": name, "boot_log": None,
                                               "helper_log": None}
        RP.ReservePanel.supervisor_state = lambda self: {
            "engine_stderr_log": "/mnt/srcds/Strata/logs/somebody-elses.log",
            "engine_log": "wrapper.log", "generation": 1}
        self.addCleanup(setattr, PANEL.Panel, "boot", real_boot)
        self.addCleanup(setattr, RP.ReservePanel, "supervisor_state", real_state)
        self.panel.identity = lambda a, phase: "token"  # type: ignore[assignment]
        self.panel.capture_runtime = lambda a, state, phase: {}  # type: ignore[assignment]
        self.panel.capture_capacity = lambda a, record: 0  # type: ignore[assignment]
        self.panel.snapshot_residency = lambda a, phase: None  # type: ignore[assignment]
        with self.assertRaises(PANEL.BootFailed):
            self.panel.boot(arm)

    def test_capacity_lines_are_saved_nul_stripped_and_counted(self):
        spec = self.panel.manifest["arms"][self.arm]
        boot_log = self.root / "boot.log"
        boot_log.write_bytes(b"expert cache: 4318 slots\0\nvram reserve 256/448 MiB\n"
                             b"serve: prompt 16000 tokens = 0 reused\n")
        self.panel.capture_capacity(self.arm, {"boot_log": str(boot_log), "helper_log": None})
        text = (self.panel.arm_dir(self.arm) / "capacity.txt").read_text()
        self.assertNotIn("\x00", text)
        self.assertIn("expert cache: 4318 slots", text)
        counts = json.loads((self.panel.arm_dir(self.arm) / "capacity.json").read_text())
        self.assertGreaterEqual(counts["matches"], 2)


class DryRunIsSafe(unittest.TestCase):
    """The default path: no endpoint, no process control, no engine."""

    def setUp(self):
        self.root = scratch_root(self)
        self.out = self.root / "reserve"
        RP.prepare(self.out)

    def test_dry_run_opens_no_socket_and_spawns_nothing_it_needs(self):
        with no_network():
            real_popen = subprocess.Popen
            subprocess.Popen = lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("dry-run must not spawn processes"))
            try:
                code, printed = capture_stdout(RP.dry_run, self.out)
            finally:
                subprocess.Popen = real_popen
        self.assertEqual(code, 0, printed)
        self.assertIn("zero endpoint requests", printed)
        self.assertIn("reserve-only-20261119", printed)

    def test_dry_run_reports_a_drifted_pin_instead_of_passing(self):
        path = self.out / "request-template.json"
        original = path.read_bytes()
        self.addCleanup(path.write_bytes, original)
        path.write_text(json.dumps({"temperature": 0.1}))
        code, printed = capture_stdout(RP.dry_run, self.out)
        self.assertEqual(code, 1)
        self.assertIn("PROBLEM", printed)

    def test_run_without_preparation_is_refused(self):
        empty = self.root / "unprepared"
        empty.mkdir()
        code, printed = capture_stdout(RP.run, empty, list(RP.RESERVE_ORDER), False)
        self.assertEqual(code, 2)
        self.assertIn("missing", printed)


class HistoricalProfileStillWorks(unittest.TestCase):
    """The champions path is the reason this preparer exists; it must not move."""

    def test_defaults_are_the_historical_profile(self):
        self.assertIs(PREP.CHAMPIONS.arms, PREP.ARMS)
        self.assertIs(PREP.CHAMPIONS.shapes, PREP.SHAPES)
        self.assertEqual(PREP.CHAMPIONS.arm_order, PREP.ARM_ORDER)
        self.assertEqual(PREP.CHAMPIONS.request_template, {})

    def test_plan_text_prints_a_manifest_written_before_the_new_keys(self):
        root = scratch_root(self)
        shutil.copy(CHAMPIONS_OUT / "manifest.json", root / "manifest.json")
        text = PREP.plan_text(root)
        self.assertIn("engine current: 8f19baaf150237e5", text)
        self.assertIn("restore target:", text)

    def test_settings_only_blanks_exactly_the_harness_paths(self):
        cfg = json.loads(PREP.PROD_CONFIG.read_text())
        blanked = PREP.settings_only(cfg)
        for field in PREP.HARNESS_CONFIG_FIELDS:
            self.assertEqual(blanked[field], PREP.PER_ARM_PLACEHOLDER, field)
        args = blanked["args"]
        self.assertEqual(args[args.index("--conversation-cache-dir") + 1],
                         PREP.PER_ARM_PLACEHOLDER)
        self.assertEqual(len(args), len(cfg["args"]))
        self.assertNotIn("--vram-reserve-mib",
                         [flag for flag, value in zip(args, args[1:])
                          if flag == "--vram-reserve-mib" and value == PREP.PER_ARM_PLACEHOLDER])

    def test_a_single_config_reports_no_difference(self):
        cfg = PREP.settings_only(json.loads(PREP.PROD_CONFIG.read_text()))
        self.assertEqual(PREP.cross_arm_setting_diffs({"a": cfg, "b": dict(cfg)}), {})


class SuiteLeavesRealArtifactsUntouched(unittest.TestCase):
    def test_the_real_reserve_artifacts_were_not_written_renamed_or_deleted(self):
        if not RP.DEFAULT_OUT.is_dir():
            self.skipTest(f"{RP.DEFAULT_OUT} not prepared yet")
        before = _FIRST_SNAPSHOT
        if before is None:
            self.skipTest("no snapshot taken at import")
        self.assertEqual(snapshot(RP.DEFAULT_OUT), before)


_FIRST_SNAPSHOT = snapshot(RP.DEFAULT_OUT) if RP.DEFAULT_OUT.is_dir() else None

if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""CPU-only fixtures for the rewind phase's EXPECTED PHYSICAL expert slot counts - PARENT executes.

ZERO GPU, ZERO process, ZERO /proc, ZERO HTTP, ZERO snapshot, ZERO stop, ZERO signal, ZERO write
outside the fixture temp dir.  The driver's REAL parser, argv builder and plan are used; every call
that would read a production config, scan /proc, snapshot, stop, restore, start a server or signal a
process is MOCKED and its call order is pinned.

Pinned behaviors:
 1. WITHOUT an expectation, the rewind and cancel-park commands are byte-identical to the legacy
    commands (the same literals the L22/L23 fixtures pin).
 2. A PAIR of expected physical counts is appended to the rewind command ONLY, as two integer flags,
    and a pair on cancel-park is refused.
 3. The parser refuses, BEFORE any plan/snapshot/stop, a partial pair, a non-rewind phase, a zero, a
    negative, a reversed pair and an expected primary below the tool's requested --expert-cache budget
    (2000) - so no invalid fixture can ever reach the production stop path.
 4. The plan records the requested BUDGET, the EXPECTED counts and the forwarded tool as separate
    fields, and says plainly when no expectation was supplied.  It records no observation and promises
    nothing about the resident MEMBERSHIP: the tool compares the pair fail-closed and writes its own
    observed counts separately (tools/dflash_sampling.py:validate_residency).
"""
from __future__ import annotations

import contextlib
import io
import pathlib
import shutil
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "window_harness"))
import model_window as mw                                        # reviewed currency/start/evidence/stop
import regression_window as R                                    # the L22/L23 window controller

PAIR = (2904, 7849)          # the parent's saved observation at the 2000-byte pin (engine-info.json)
BUDGET = 2000               # the rewind tool's requested --expert-cache BYTE budget (its :40-42 + :58)
LEGACY_REWIND = [mw.VENV_PY, f"{mw.CLONE}/tools/dflash_cache.py", "--config", "/cfg.json",
                 "--engine", mw.PROTO_BINARY, "--output", "/out"]
LEGACY_CANCEL_PARK = [mw.VENV_PY, f"{mw.CLONE}/tools/cancel_park_test.py", "/cfg.json"]
FORWARDED = ["--expected-primary-slots", "2904", "--expected-total-slots", "7849"]


class DriverSandbox(unittest.TestCase):
    """Saves and restores every driver attribute a test patches, so no fixture leaks state."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(dir=str(R.W.OPS), prefix="tmp-slotexpect-"))
        self.saved: dict = {}

    def tearDown(self):
        for target, name, value in self.saved.values():
            setattr(target, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def patch(self, target, name: str, value) -> None:
        key = (id(target), name)
        if key not in self.saved:
            self.saved[key] = (target, name, getattr(target, name))
        setattr(target, name, value)


class CommandForwarding(DriverSandbox):
    def test_default_commands_are_byte_identical(self):
        self.assertEqual(R.phase_argv("rewind", pathlib.Path("/cfg.json"), pathlib.Path("/out")),
                         LEGACY_REWIND, "the default rewind command keeps every legacy token in place")
        self.assertEqual(R.phase_argv("rewind", pathlib.Path("/cfg.json"), pathlib.Path("/out"), None),
                         LEGACY_REWIND, "an explicit no-expectation is the same command")
        self.assertEqual(R.phase_argv("cancel-park", pathlib.Path("/cfg.json"), pathlib.Path("/cfg.json")),
                         LEGACY_CANCEL_PARK, "cancel-park keeps its single positional config argument")

    def test_pair_is_forwarded_for_rewind(self):
        argv = R.phase_argv("rewind", pathlib.Path("/cfg.json"), pathlib.Path("/out"), PAIR)
        self.assertEqual(argv, LEGACY_REWIND + FORWARDED,
                         "the pair is APPENDED after the legacy tokens, nothing else moves")
        self.assertEqual(argv[:len(LEGACY_REWIND)], LEGACY_REWIND)

    def test_cancel_park_never_forwards_an_expectation(self):
        with self.assertRaises(SystemExit):
            R.phase_argv("cancel-park", pathlib.Path("/cfg.json"), pathlib.Path("/out"), PAIR)
        self.assertEqual(R.phase_argv("cancel-park", pathlib.Path("/cfg.json"), pathlib.Path("/out")),
                         LEGACY_CANCEL_PARK, "the refusal leaves the legacy command untouched")

    def test_forwarded_values_are_plain_integers(self):
        argv = R.phase_argv("rewind", pathlib.Path("/cfg.json"), pathlib.Path("/out"), (2904, 100000))
        self.assertEqual(argv[-4:], ["--expected-primary-slots", "2904",
                                     "--expected-total-slots", "100000"],
                         "counts are forwarded as plain decimal integers, never as floats")


class ParserRules(DriverSandbox):
    def test_valid_pairs(self):
        self.assertEqual(R.check_phase_expectations("rewind", 2904, 7849), ((2904, 7849), []))
        self.assertEqual(R.check_phase_expectations("rewind", 2000, 2000), ((2000, 2000), []))
        self.assertEqual(R.check_phase_expectations("rewind", None, None), (None, []))

    def test_partial_pairs_are_refused(self):
        for primary, total in ((2904, None), (None, 7849)):
            pair, problems = R.check_phase_expectations("rewind", primary, total)
            self.assertEqual(pair, None, "a partial pair never yields a forwarded pair")
            self.assertTrue(any("required together" in p for p in problems), problems)

    def test_non_rewind_phases_are_refused(self):
        for phase in ("cancel-park", "integration"):
            pair, problems = R.check_phase_expectations(phase, 2904, 7849)
            self.assertEqual(pair, None)
            self.assertTrue(any("rewind" in p for p in problems), problems)

    def test_zero_negative_and_reversed_are_refused(self):
        for primary, total in ((0, 7849), (2904, 0), (0, 0), (-2904, -7849), (7849, 2904)):
            pair, problems = R.check_phase_expectations("rewind", primary, total)
            self.assertEqual(pair, None, f"{primary}/{total} must not be forwarded")
            self.assertTrue(problems, f"{primary}/{total} must report a problem")

    def test_clamped_expected_primary_below_the_budget_is_refused(self):
        for primary, total in ((1999, 7849), (1999, 2000), (1999, 1999)):
            pair, problems = R.check_phase_expectations("rewind", primary, total)
            self.assertEqual(pair, None, f"expected primary {primary} < budget {BUDGET} is a fixture bug")
            self.assertTrue(any(str(BUDGET) in p for p in problems), problems)

    def test_budget_constant_is_the_tools_forced_pin(self):
        self.assertEqual(R.REWIND_EXPERT_CACHE_BUDGET, BUDGET)
        self.assertIn("--expert-cache", R.PHASES["rewind"]["forced"])


class PlanRecording(DriverSandbox):
    def setUp(self):
        super().setUp()
        cfg = {"args": ["--native", "model.gguf", "--dflash", "drafter.gguf", "--layer-split", "32"],
               "model_name": "swift15", "gpu": "0,1", "layer_split": "32",
               "log": f"{R.W.LOGS}/engine-regwindow-W0-dflash_w3.log"}
        self.patch(R, "load_prod_config", lambda *a, **k: {})
        self.patch(R, "source_support", lambda: ({}, []))
        self.patch(R, "derive", lambda window, base: (dict(cfg), [], []))
        self.patch(R, "identity_note", lambda c: {"synthetic": True})
        self.patch(R.mw, "currency", lambda *a, **k: ({"config": str(self.tmp / "clone.json")}, []))
        self.patch(R.W, "config_sha", lambda: "fixture-sha")

    def plan(self, phase: str, expectations=None):
        out, problems = R.plan("W-slotexpect", phase, expectations)
        self.assertEqual(problems, [], "the mocked plan has no derivation problems")
        return out

    def test_default_rewind_plan_is_the_legacy_command(self):
        out = self.plan("rewind")
        want = " ".join([mw.VENV_PY, f"{mw.CLONE}/tools/dflash_cache.py", "--config",
                         str(mw.CONFIG_DIR / "proto-server-swift15-dflash_w3-reg-W-slotexpect.json"),
                         "--engine", mw.PROTO_BINARY,
                         "--output", str(R.W.OPS / "reg-window-W-slotexpect-rewind")])
        self.assertEqual(out["phase_command"], want)
        rec = out["phase_expected_slots"]
        self.assertEqual((rec["expected_primary_slots"], rec["expected_total_slots"],
                          rec["requested_expert_cache_budget"], rec["forwarded_to"]),
                         (None, None, BUDGET, None))

    def test_pair_is_recorded_and_forwarded_for_rewind(self):
        out = self.plan("rewind", PAIR)
        self.assertTrue(out["phase_command"].endswith(" ".join(FORWARDED)), out["phase_command"])
        self.assertTrue(out["phase_command"].startswith(mw.VENV_PY))
        rec = out["phase_expected_slots"]
        self.assertEqual((rec["expected_primary_slots"], rec["expected_total_slots"],
                          rec["requested_expert_cache_budget"]), (2904, 7849, BUDGET))
        self.assertEqual(rec["forwarded_to"], f"{mw.CLONE}/tools/dflash_cache.py")
        self.assertNotIn("observed", rec, "a plan records an EXPECTATION, never a claimed observation")
        self.assertIn("fail-closed", rec["note"])

    def test_no_slot_expectation_for_cancel_park(self):
        out = self.plan("cancel-park")
        rec = out["phase_expected_slots"]
        self.assertNotIn("--expected-primary-slots", out["phase_command"])
        self.assertNotIn("--expected-total-slots", out["phase_command"])
        self.assertEqual((rec["expected_primary_slots"], rec["expected_total_slots"],
                          rec["requested_expert_cache_budget"], rec["forwarded_to"]),
                         (None, None, BUDGET, None))

    def test_plan_writes_nothing(self):
        before = sorted(p.name for p in self.tmp.iterdir())
        self.plan("rewind", PAIR)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), before,
                         "a plan creates no config, no output dir and no file")


class DriverRefusal(DriverSandbox):
    """The driver must refuse an invalid fixture BEFORE a plan, a snapshot, a stop or any signal."""

    def setUp(self):
        super().setUp()
        self.calls: list[str] = []

        def plan(window, phase, expectations=None):
            self.calls.append("plan")
            return {"window": window, "phase": phase, "base_config": str(self.tmp / "b.json")}, []

        self.patch(R, "plan", plan)
        self.patch(R, "write_derived_config",
                   lambda p, c: self.calls.append("write_derived_config"))
        self.patch(R, "apply_phase_controls", lambda p, ph, rec: self.calls.append("apply_controls"))
        self.patch(R, "run_phase_window", lambda *a, **k: self.calls.append("run_phase_window"))
        self.patch(R, "run_regression", lambda *a, **k: self.calls.append("run_regression"))
        self.patch(R.mw, "install_signal_handlers", lambda: self.calls.append("install_handlers"))
        self.patch(R, "restore_gate", lambda *a, **k: self.calls.append("restore_gate"))
        self.patch(R.W, "snapshot", lambda *a, **k: self.calls.append("snapshot")
                   or {"verified": True, "display": {}})
        self.patch(R.W, "display", lambda snap: {})
        self.patch(R.W, "config_sha", lambda: "fixture-sha")
        self.patch(R.W, "note_to", lambda path, msg: self.calls.append("note"))

    def drive(self, argv: list[str]) -> int | None:
        code: list[int] = []
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            old = sys.argv
            sys.argv = ["regression_window.py"] + argv
            try:
                code.append(R.main())
            except SystemExit as e:
                code.append(int(e.code))
            finally:
                sys.argv = old
        return code[0]

    def test_invalid_expectations_exit_before_any_plan_or_snapshot(self):
        for argv in (["--phase", "rewind", "--expected-primary-slots", "2904"],
                     ["--phase", "rewind", "--expected-total-slots", "7849"],
                     ["--phase", "cancel-park", "--expected-primary-slots", "2904",
                      "--expected-total-slots", "7849"],
                     ["--expected-primary-slots", "2904", "--expected-total-slots", "7849"],
                     ["--phase", "rewind", "--expected-primary-slots", "0",
                      "--expected-total-slots", "7849"],
                     ["--phase", "rewind", "--expected-primary-slots", "7849",
                      "--expected-total-slots", "2904"],
                     ["--phase", "rewind", "--expected-primary-slots", "1999",
                      "--expected-total-slots", "7849"],
                     ["--phase", "rewind", "--expected-primary-slots", "abc",
                      "--expected-total-slots", "7849"],
                     ["--execute", "--phase", "rewind", "--expected-primary-slots", "1999",
                      "--expected-total-slots", "7849"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.drive(argv), 2, argv)
                self.assertEqual(self.calls, [], f"nothing may run for {argv}: {self.calls}")

    def test_valid_rewind_pair_reaches_the_plan_and_records_it(self):
        code = self.drive(["--phase", "rewind", "--expected-primary-slots", "2904",
                           "--expected-total-slots", "7849"])
        self.assertEqual(code, 0, "a valid pair in a DRY RUN completes the plan")
        self.assertEqual(self.calls, ["plan", "snapshot"],
                         f"a dry run plans and snapshots only: {self.calls}")

    def test_default_run_is_unchanged(self):
        code = self.drive(["--phase", "rewind"])
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["plan", "snapshot"])


if __name__ == "__main__":
    unittest.main()

"""The GPU cache gate must reject changed expert placement before comparing IDs."""
import contextlib
import io
import queue
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dflash_cache import validate_residency
from dflash_sampling import Engine, residency_evidence_row, resolve_expected_slots


class CacheGate(unittest.TestCase):
    def info(self):
        return dict(expert_slots='2000', expert_slots_primary='2000', context='4096',
                    kv='int8', kv_resident='0', spec='2', pcie_frac='0.00', pool_workers='23')

    def test_fixed_and_matching_placement(self):
        validate_residency(self.info(), 2000, self.info())

    def test_clamped_equal_budgets_are_still_rejected(self):
        info = self.info()
        info.update(expert_slots='1999', expert_slots_primary='1999')
        with self.assertRaisesRegex(AssertionError, 'Reduce --expert-cache'):
            validate_residency(info, 2000, info)

    def test_missing_or_different_configuration_rejected(self):
        for key in self.info():
            with self.subTest(key=key):
                info = self.info()
                info.pop(key)
                with self.assertRaises(AssertionError):
                    validate_residency(info, 2000, self.info())

    def test_native_info_is_retained(self):
        engine = Engine.__new__(Engine)
        engine.info = {}
        engine.lines = queue.Queue()
        engine.lines.put('INFO expert_slots=2000 kv=int8')
        engine.line()
        self.assertEqual(engine.info, dict(expert_slots='2000', kv='int8'))

    def test_dead_child_does_not_mask_original_error_during_close(self):
        engine = Engine.__new__(Engine)
        engine.p = Mock()
        engine.p.poll.return_value = 1
        engine.p.stdin.close.side_effect = BrokenPipeError()
        engine.reader = Mock()
        engine.log = Mock()
        engine.close()
        engine.p.stdout.close.assert_called_once()
        engine.log.close.assert_called_once()


class SlotExpectations(unittest.TestCase):
    """EXPECTED PHYSICAL slot counts are a PAIRED fixture input, never read back from the engine.
    The 2904 primary / 7849 total split is the parent's saved observation at the 2000-byte pin
    (ops/reg-window-20261024-043016-rewind-rewind/engine-info.json: expert_slots_primary=2904,
    expert_slots=7849).  --expert-cache is a byte budget; these are slot counts."""

    def split(self):
        return dict(expert_slots='7849', expert_slots_primary='2904', context='4096',
                    kv='int8', kv_resident='0', spec='2', pcie_frac='0.00', pool_workers='23')

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=str(Path(__file__).resolve().parents[1] / "ops"),
                                    prefix="tmp-slotfixtures-")     # project ops dir, never /tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def check(self, info, reference, primary, total, requested=2000):
        return validate_residency(info, requested, reference,
                                  expected_primary_slots=primary, expected_total_slots=total)

    def test_exact_split_passes_with_matching_reference(self):
        self.check(self.split(), self.split(), 2904, 7849)

    def test_budget_equal_counts_still_pass_without_expectations(self):
        info = self.split()
        info.update(expert_slots='2000', expert_slots_primary='2000')
        validate_residency(info, 2000, info)

    def test_split_still_fails_without_expectations(self):
        with self.assertRaisesRegex(AssertionError, 'Reduce --expert-cache'):
            validate_residency(self.split(), 2000, self.split())

    def test_partial_expectations_fail(self):
        for primary, total in ((2904, None), (None, 7849)):
            with self.subTest(primary=primary, total=total):
                with self.assertRaisesRegex(AssertionError, 'required together'):
                    self.check(self.split(), self.split(), primary, total)

    def test_zero_negative_reversed_and_clamped_expectations_fail(self):
        for primary, total in ((0, 7849), (2904, 0), (-1, -1), (7849, 2904), (1999, 7849), (1999, 2000)):
            with self.subTest(pair=(primary, total)):
                with self.assertRaises(AssertionError):
                    self.check(self.split(), self.split(), primary, total)

    def test_unexpected_primary_fails_even_when_every_arm_shares_it(self):
        info = self.split()
        info['expert_slots_primary'] = '2905'
        with self.assertRaisesRegex(AssertionError, 'expert_slots_primary=2905'):
            self.check(info, info, 2904, 7849)

    def test_unexpected_total_fails_even_when_every_arm_shares_it(self):
        info = self.split()
        info['expert_slots'] = '7850'
        with self.assertRaisesRegex(AssertionError, 'expert_slots=7850'):
            self.check(info, info, 2904, 7849)

    def test_reference_configuration_mismatch_still_fails(self):
        ref = self.split()
        ref['context'] = '8192'
        with self.assertRaisesRegex(AssertionError, 'incomparable engine setting context'):
            self.check(self.split(), ref, 2904, 7849)

    def test_reference_counts_that_differ_still_fail(self):
        ref = self.split()
        ref.update(expert_slots='2000', expert_slots_primary='2000')
        with self.assertRaisesRegex(AssertionError, 'incomparable engine setting expert_slots'):
            self.check(self.split(), ref, 2904, 7849)

    def test_missing_or_malformed_observed_count_fails_closed(self):
        for key, value in (('expert_slots', None), ('expert_slots_primary', None),
                           ('expert_slots', 'n/a'), ('expert_slots_primary', '')):
            info = self.split()
            info[key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(AssertionError, 'is not a physical expert slot count'):
                    self.check(info, None, 2904, 7849)

    def test_resolver_is_the_shared_pair_rule(self):
        self.assertEqual(resolve_expected_slots(2904, 7849, 2000), (2904, 7849))
        self.assertEqual(resolve_expected_slots(2000, 2000, 2000), (2000, 2000))
        self.assertIsNone(resolve_expected_slots(None, None, 2000))
        for primary, total in ((2904, None), (None, 7849), (0, 1), (-5, -5), (7849, 2904), (1999, 2000)):
            with self.subTest(pair=(primary, total)):
                with self.assertRaises(ValueError):
                    resolve_expected_slots(primary, total, 2000)

    def test_evidence_keeps_budget_expectation_and_observation_distinct(self):
        row = residency_evidence_row(self.split(), 2000, (2904, 7849))
        self.assertEqual((row["requested_expert_cache_budget"], row["expected_primary_slots"],
                          row["expected_total_slots"], row["observed_expert_slots_primary"],
                          row["observed_expert_slots"]), (2000, 2904, 7849, "2904", "7849"))
        legacy = residency_evidence_row(CacheGate().info(), 2000, None)
        self.assertEqual((legacy["requested_expert_cache_budget"], legacy["expected_primary_slots"],
                          legacy["expected_total_slots"], legacy["observed_expert_slots"]),
                         (2000, None, None, "2000"))

    def test_cli_refuses_a_bad_pair_before_any_engine_or_write(self):
        """The CLI fails closed at parse time: no engine, no config read, no output directory."""
        import dflash_cache
        base = ["dflash_cache.py", "--config", "/nonexistent-config.json", "--engine",
                "/nonexistent-engine", "--output", str(Path(self.tmp) / "out")]
        for pair in (["--expected-primary-slots", "2904"], ["--expected-total-slots", "7849"],
                     ["--expected-primary-slots", "1999", "--expected-total-slots", "7849"],
                     ["--expected-primary-slots", "7849", "--expected-total-slots", "2904"],
                     ["--expected-primary-slots", "0", "--expected-total-slots", "7849"],
                     ["--expected-primary-slots", "not-a-number", "--expected-total-slots", "7849"]):
            with self.subTest(pair=pair):
                with self.assertRaises(SystemExit) as got:
                    with contextlib.redirect_stderr(io.StringIO()):
                        run_main(dflash_cache, base + pair)
                self.assertEqual(got.exception.code, 2)
                self.assertFalse((Path(self.tmp) / "out").exists(),
                                 "a refused expectation creates no output directory")


def run_main(module, argv: list[str]) -> None:
    """Call a tool's main() with a stub argv (a module method, so self is bound)."""
    old = sys.argv
    sys.argv = argv
    try:
        module.main()
    finally:
        sys.argv = old


if __name__ == '__main__':
    unittest.main()

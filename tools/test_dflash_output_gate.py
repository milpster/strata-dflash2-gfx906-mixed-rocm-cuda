import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
"""Fail-closed checks for the real-weight output/cache gate; no GPU required."""
import copy
import json
import unittest
from dflash_output_gate import evaluate, first_difference


def fixture():
    record = dict(tokens=[1, 2], requested=2, reused=10)
    records = {key: copy.deepcopy(record) for key in
               ('code-greedy','code-sampled','cache-live','cache-restored','cache-checkpoint','cache-cold')}
    records['cache-cold']['reused']=0
    return dict(info={'conversation_cache_mib':'2048', 'expert_slots':'2000',
                      'expert_slots_primary':'2000', 'context':'8192', 'kv':'int8', 'spec':'8'},records=records)


def split_fixture(primary='2904', total='7849'):
    """The same records at the parent's OBSERVED physical split (2904 primary / 7849 total at the
    2000-byte pin, ops/reg-window-20261024-043016-rewind-rewind/engine-info.json)."""
    arm = fixture()
    arm['info']['expert_slots'], arm['info']['expert_slots_primary'] = total, primary
    return arm


def all_ok(checks):
    return all(v['equal'] if isinstance(v, dict) else v for arm in checks.values() for v in arm.values())


def gate(**expectations):
    r = {'fixed-ar': split_fixture(), 'fixed-dflash2': split_fixture()}
    return r, evaluate(r, ['code'], **expectations)


class Gate(unittest.TestCase):
    def test_changed_expert_placement_invalidates_comparison(self):
        r={'fixed-ar':fixture(),'fixed-dflash2':fixture()}
        r['fixed-dflash2']['info']['expert_slots']='1999'
        c=evaluate(r,['code'])['fixed-dflash2']
        self.assertFalse(c['fixed_expert_residency'])
        self.assertFalse(c['target_configuration_matches'])

    def test_prompt_read_field_is_not_offloaded_expert_count(self):
        from dflash_output_gate import ObservedEngine
        from unittest.mock import patch
        done = 'DONE 2 12 1.0 2.0 length 0 0 10 0 0 0 0 0.0 2 123'
        with patch('dflash_sampling.Engine.request', return_value={'tokens':[1,2], 'done':done}):
            r=ObservedEngine.__new__(ObservedEngine).request([0]*12,'temperature=0',2)
        self.assertEqual(r['reused'],10)
        self.assertEqual(r['read'],2)

    def test_exact_output_and_cache(self):
        r={'fixed-ar':fixture(),'fixed-dflash2':fixture()}
        c=evaluate(r,['code'])
        self.assertTrue(all(v['equal'] if isinstance(v,dict) else v for a in c.values() for v in a.values()))

    def test_disabled_cache_and_no_hit_are_failures(self):
        r={'fixed-ar':fixture(),'fixed-dflash2':fixture()}
        r['fixed-dflash2']['info']['conversation_cache_mib']='0'
        r['fixed-dflash2']['records']['cache-restored']['reused']=0
        c=evaluate(r,['code'])['fixed-dflash2']
        self.assertFalse(c['cache_enabled'])
        self.assertFalse(c['cache-restored-hit'])
        r['fixed-ar']['records']['cache-cold']['reused']=10
        self.assertFalse(evaluate(r,['code'])['fixed-ar']['cold_control_missed'])

    def test_missing_target_and_truncation_are_failures(self):
        r={'fixed-dflash2':fixture()}
        r['fixed-dflash2']['records']['code-greedy']['tokens']=[]
        c=evaluate(r,['code'])['fixed-dflash2']
        self.assertFalse(c['target_reference_present'])
        self.assertFalse(c['complete_requests'])

    def test_changed_output_is_not_a_near_tie_without_evidence(self):
        r={'fixed-ar':fixture(),'fixed-dflash2':fixture()}
        r['fixed-dflash2']['records']['code-sampled']['tokens']=[1,3]
        c=evaluate(r,['code'])['fixed-dflash2']
        self.assertFalse(c['code-sampled']['equal'])
        self.assertEqual(c['code-sampled']['first_difference'],1)
        self.assertEqual(first_difference([1],[1,2]),1)
        self.assertIsNone(first_difference([1,2],[1,2]))


class SlotExpectations(unittest.TestCase):
    """--expected-primary-slots / --expected-total-slots: a PAIRED harness fixture for the PHYSICAL
    counts. The gate's forced --expert-cache 2000 stays a byte budget; the target reference, the
    cross-arm count equality and every cache/activation/parity requirement stay required in both modes."""

    def test_exact_split_pair_passes(self):
        _r, c = gate(expected_primary_slots=2904, expected_total_slots=7849)
        self.assertTrue(c['fixed-dflash2']['fixed_expert_residency'])
        self.assertTrue(c['fixed-dflash2']['target_configuration_matches'])
        self.assertTrue(all_ok(c), json.dumps(c))

    def test_split_pair_still_fails_without_expectations(self):
        _r, c = gate()
        self.assertFalse(c['fixed-dflash2']['fixed_expert_residency'])

    def test_legacy_budget_counts_still_pass_without_expectations(self):
        r = {'fixed-ar': fixture(), 'fixed-dflash2': fixture()}
        self.assertTrue(all_ok(evaluate(r, ['code'])))

    def test_unexpected_primary_fails_even_when_both_arms_share_it(self):
        r = {'fixed-ar': split_fixture(primary='2905'), 'fixed-dflash2': split_fixture(primary='2905')}
        c = evaluate(r, ['code'], expected_primary_slots=2904, expected_total_slots=7849)
        self.assertFalse(c['fixed-ar']['fixed_expert_residency'])
        self.assertFalse(c['fixed-dflash2']['fixed_expert_residency'])
        self.assertFalse(all_ok(c))

    def test_unexpected_total_fails_even_when_both_arms_share_it(self):
        r = {'fixed-ar': split_fixture(total='7850'), 'fixed-dflash2': split_fixture(total='7850')}
        c = evaluate(r, ['code'], expected_primary_slots=2904, expected_total_slots=7849)
        self.assertFalse(c['fixed-ar']['fixed_expert_residency'])
        self.assertFalse(c['fixed-dflash2']['fixed_expert_residency'])

    def test_partial_reversed_zero_and_clamped_pairs_fail_every_arm(self):
        for kw in ({'expected_primary_slots': 2904}, {'expected_total_slots': 7849},
                   {'expected_primary_slots': 7849, 'expected_total_slots': 2904},
                   {'expected_primary_slots': 0, 'expected_total_slots': 7849},
                   {'expected_primary_slots': 1999, 'expected_total_slots': 2000}):
            with self.subTest(pair=kw):
                _r, c = gate(**kw)
                self.assertFalse(c['fixed-ar']['fixed_expert_residency'])
                self.assertFalse(c['fixed-dflash2']['fixed_expert_residency'])

    def test_reference_configuration_mismatch_fails_with_satisfied_counts(self):
        r = {'fixed-ar': fixture(), 'fixed-dflash2': split_fixture()}      # the reference keeps 2000/2000
        c = evaluate(r, ['code'], expected_primary_slots=2904, expected_total_slots=7849)
        self.assertFalse(c['fixed-dflash2']['target_configuration_matches'])
        self.assertFalse(c['fixed-ar']['fixed_expert_residency'])          # every arm is compared

    def test_missing_target_reference_still_fails_with_satisfied_counts(self):
        c = evaluate({'fixed-dflash2': split_fixture()}, ['code'],
                     expected_primary_slots=2904, expected_total_slots=7849)['fixed-dflash2']
        self.assertTrue(c['fixed_expert_residency'])
        self.assertFalse(c['target_reference_present'])
        self.assertFalse(all_ok({'fixed-dflash2': c}))

    def test_cache_and_parity_failures_stay_required_with_expectations(self):
        r = {'fixed-ar': split_fixture(), 'fixed-dflash2': split_fixture()}
        r['fixed-dflash2']['info']['conversation_cache_mib'] = '0'
        r['fixed-dflash2']['records']['cache-restored']['reused'] = 0
        r['fixed-ar']['records']['cache-cold']['reused'] = 10
        r['fixed-ar']['records']['code-greedy']['tokens'] = [1, 3]
        c = evaluate(r, ['code'], expected_primary_slots=2904, expected_total_slots=7849)
        self.assertFalse(c['fixed-dflash2']['cache_enabled'])
        self.assertFalse(c['fixed-dflash2']['cache-restored-hit'])
        self.assertFalse(c['fixed-ar']['cold_control_missed'])
        self.assertFalse(c['fixed-dflash2']['code-greedy']['equal'])
        self.assertFalse(all_ok(c))

    def test_malformed_observed_count_fails_closed_with_expectations(self):
        for key, value in (('expert_slots', 'n/a'), ('expert_slots_primary', None), ('expert_slots', '')):
            r = {'fixed-ar': split_fixture(), 'fixed-dflash2': split_fixture()}
            r['fixed-dflash2']['info'][key] = value
            with self.subTest(key=key, value=value):
                c = evaluate(r, ['code'], expected_primary_slots=2904, expected_total_slots=7849)
                self.assertFalse(c['fixed-dflash2']['fixed_expert_residency'])


if __name__=='__main__': unittest.main()

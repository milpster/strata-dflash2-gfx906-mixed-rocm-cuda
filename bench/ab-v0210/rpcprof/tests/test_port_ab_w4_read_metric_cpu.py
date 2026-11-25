#!/usr/bin/env python3
"""CPU-only tests for W4's re-scoped gate metric. No GPU, no endpoint, no boot.

What this fixes: W4 is the contract's 2x2 attribution window, and its gate metric was gen_tps on
dec144k (kind tg, 1,024-token cap) - a metric the harness's TG floor (>= 1,000 tokens actually
generated) makes unmeasurable here, because the answer-room guard closes the thinking at 512 of
a 1,024-token cap and the reply ends at EOS after it. All four W2 decode boots generated 512-513
tokens, so all four W4 boots would have failed the floor. W4 now gates on request 1's read_tps
(the 143,893-token cold prefill the same leg already produces) and makes no TG claim.

Everything here is static, or runs check() over synthetic records in a temp dir: nothing boots
and nothing is written under out/port-ab-20261123.
"""
from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

RPC = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RPC))
import port_ab_configs as PC  # noqa: E402
import port_ab_contract as C  # noqa: E402
import port_ab_legs as LEGS  # noqa: E402
import port_ab_prepare as PREP  # noqa: E402

TMPROOT = "/tmp/opencode" if pathlib.Path("/tmp/opencode").is_dir() else None


def tmpdir():
    return tempfile.TemporaryDirectory(dir=TMPROOT)


class W4GateMetric(unittest.TestCase):
    """The window's gate decision, and the two keys that state it."""

    def test_gates_on_read_tps_with_the_preregistered_prefill_delta(self):
        w4 = C.WINDOWS["W4"]
        self.assertEqual(w4["gate_metric"], "read_tps")
        # the same pre-registered prefill delta W1/W1d/W1f carry - copied, not invented
        self.assertEqual(w4["delta"], C.CLAIM["delta_pre_registered"]["prefill_read_tps"])
        self.assertEqual(w4["delta"], C.WINDOWS["W1"]["delta"])
        self.assertEqual(w4["delta"], 0.05)
        self.assertIn("read_tps", w4["metric_note"])

    def test_the_decode_delta_is_kept_as_history_not_deleted(self):
        note = C.WINDOWS["W4"]["delta_note"]
        # the 8 % decode delta W4 used while it gated on gen_tps is still recorded, named as
        # the CLAIM block's decode key, and explicitly marked history-only
        self.assertIn("decode_gen_tps", note)
        self.assertIn("HISTORY", note)
        self.assertEqual(C.CLAIM["delta_pre_registered"]["decode_gen_tps"], 0.08)
        self.assertIsNone(C.WINDOWS["W4"].get("delta_was"))  # written into the note, not a field

    def test_prepare_delta_key_follows_the_gate_metric(self):
        self.assertEqual(PREP.DELTA_KEY["dec144k"]["W4"], "prefill_read_tps")
        self.assertIn(PREP.DELTA_KEY["dec144k"]["W4"], C.CLAIM["delta_pre_registered"])
        # W2 keeps the decode key: that window really does measure and claim a decode rate
        self.assertEqual(PREP.DELTA_KEY["dec144k"]["W2"], "decode_gen_tps")

    def test_manifest_slots_carry_it(self):
        manifest = json.loads(PREP.MANIFEST.read_text())
        w4 = manifest["windows"]["W4"]
        self.assertEqual(w4["gate_metric"], "read_tps")
        self.assertEqual(w4["delta"], 0.05)
        self.assertEqual(w4["legs"]["dec144k"]["delta_key"], "prefill_read_tps")
        self.assertIn("GATES ON read_tps", w4["legs"]["dec144k"]["metric"])


class W4NoTGClaim(unittest.TestCase):
    """W4 runs the leg but makes no TG claim on it; every other window is untouched."""

    def test_w4_makes_no_tg_claim_and_w2_does(self):
        self.assertFalse(C.claims_tg("dec144k", "W4"))
        self.assertTrue(C.claims_tg("dec144k", "W2"))

    def test_no_other_window_lost_its_tg_claim(self):
        # the +1024tg directive's TG rates stay claim-grade everywhere else
        for shape, window in (("pp239k", "W1"), ("pp239k", "W3"),
                              ("pp239k-prodshape", "W3p")):
            self.assertTrue(C.claims_tg(shape, window), window)

    def test_the_leg_shape_and_cap_did_not_move(self):
        # only the JUDGEMENT changed: same two-request tg shape, same corpus, same 1,024 cap
        self.assertEqual(C.kind_for("dec144k", "W4"), "tg")
        self.assertEqual(C.gen_tokens("dec144k", "W4"), 1024)
        self.assertEqual(C.corpus_shape("dec144k"), "dec144k")
        self.assertEqual(C.SHAPES["dec144k"]["bytes"], 796618)

    def test_the_2x2_schedule_is_unchanged(self):
        rows = LEGS.schedule("W4")
        self.assertEqual([(r["arm"], r["rep"], r["server_arm"]) for r in rows],
                         [("A", 1, "B"), ("B", 1, "A"), ("B", 2, "A"), ("A", 2, "B")])
        self.assertEqual([r["cell"] for r in rows],
                         ["A-engine+B-server", "B-engine+A-server",
                          "B-engine+A-server", "A-engine+B-server"])
        self.assertEqual([PC.boot_key("W4", r["arm"], r["rep"], r["boot"]) for r in rows],
                         ["W4-A-dec-r1", "W4-B-dec-r1", "W4-B-dec-r2", "W4-A-dec-r2"])

    def test_the_runbook_prints_the_same_four_boots(self):
        text = LEGS.runbook(PC.OUT_DEFAULT, "W4")
        boots = [ln for ln in text.splitlines() if ln.startswith("## ")]
        self.assertEqual(boots, ["## W4-A-dec-r1 (cell)", "## W4-B-dec-r1 (cell)",
                                 "## W4-B-dec-r2 (cell)", "## W4-A-dec-r2 (cell)"])
        self.assertEqual(text.count("port_ab_boot.sh W4"), 4)
        self.assertEqual(text.count("port_ab_stop.sh W4"), 4)
        self.assertIn("--server-arm B", text)
        self.assertIn("--server-arm A", text)


class W4CheckPassesOnReadTPS(unittest.TestCase):
    """The regression itself: 513 generated FAILS as a TG leg, PASSES as W4's read_tps leg."""

    def run_check(self, key_arm, generated, second_reused, window):
        with tmpdir() as root:
            for key, arm in key_arm:
                LEGS._fixture(root, key, arm, "dec144k", generated=generated,
                              second_reused=second_reused)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = LEGS.check(pathlib.Path(root), window)
            return rc, buf.getvalue()

    def test_513_generated_fails_w2_and_passes_w4(self):
        pairs = (("W2-A-dec-r1", "A"), ("W2-B-dec-r1", "B"))
        rc, text = self.run_check(pairs, 513, 144405, "W2")
        self.assertEqual(rc, 1)
        self.assertIn("TG leg generated 513", text)
        w4_pairs = (("W4-A-dec-r1", "A"), ("W4-B-dec-r1", "B"))
        rc, text = self.run_check(w4_pairs, 513, 144405, "W4")
        self.assertEqual(rc, 0, text)
        self.assertNotIn("TG leg generated", text)
        # the leg is reported, and its TG column says context only - never a silent TG sample
        rows = [ln.split() for ln in text.splitlines()
                if ln.startswith("dec144k") and " W4-" in ln]
        self.assertEqual(len(rows), 2)
        self.assertEqual([r[7] for r in rows], ["ctx", "ctx"])
        # and read_tps is the number that was judged, with the prefill rate present
        self.assertTrue(all(float(r[5]) > 0 for r in rows))

    def test_no_boot_record_is_a_warning_not_a_failure(self):
        # A check over re-keyed records has no boots/<key>.json for them (a boot record carries
        # W2's own kernel-journal window and says nothing about W4). That must stay a WARN with
        # the tuple still read from the log - never a failure - or the re-key check could not
        # judge the leg at all.
        with tmpdir() as root:
            for key, arm in (("W4-A-dec-r1", "A"), ("W4-B-dec-r1", "B")):
                LEGS._fixture(root, key, arm, "dec144k", generated=513, second_reused=144405)
                (pathlib.Path(root) / "boots" / (key + ".json")).unlink()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = LEGS.check(pathlib.Path(root), "W4")
        text = buf.getvalue()
        self.assertEqual(rc, 0, text)
        self.assertIn("no boot record under boots/", text)
        self.assertIn("tuple checked from the log", text)


if __name__ == "__main__":
    unittest.main()

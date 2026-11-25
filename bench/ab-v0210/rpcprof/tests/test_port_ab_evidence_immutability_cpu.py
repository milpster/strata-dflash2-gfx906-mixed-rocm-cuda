#!/usr/bin/env python3
"""CPU-only tests for the boot-evidence immutability rule in port_ab_legs.py.

No GPU, no endpoint, no engine. Every fixture lives under out/tests-tmp (project-local, removed
when the test ends); the measurement tree out/port-ab-20261123 holds completed evidence and is
never written here.

Why this exists: on 2026-11-25T00:51 a single `port_ab_legs.py --check W2` replaced all twelve
W2 evidence files. Two of the fields it writes are CURRENT box state (page_cache_now() and the
kernel-journal window), so recomputing a finished window rewrote its history: cached_delta_mib
6138 -> 3333, majfault_delta 3452753 -> 9787947, written_at forward five hours. write_boot_evidence
now leaves an existing file alone unless refresh=True. These tests pin that behaviour so it
cannot silently come back.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import unittest

RPC = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RPC))
import port_ab_legs as LEGS  # noqa: E402

SCRATCH = RPC / "out" / "tests-tmp"


def fake_checks(cached_now=14007, majfault_now=9787947):
    """The minimum shape write_boot_evidence reads: it only formats these into the file."""
    return {"fingerprint": {"count": 0, "matches": [], "window": {"since": 1, "until": 2}},
            "page_cache": {"cached_mib_at_boot": 10674, "cached_mib_now": cached_now,
                           "cached_delta_mib": cached_now - 10674, "majfault_delta": majfault_now},
            "low": 1, "fs_slots": ["3,2"], "cache_slots": 4951,
            "restart": {"generation": 1, "events": 0}, "fill": {"rows": 3655},
            "tuple": {"--vram-reserve-mib": "256"}, "problems": [], "warnings": []}


class EvidenceImmutability(unittest.TestCase):
    def setUp(self):
        self.out = SCRATCH / ("evidence-" + self._testMethodName)
        if self.out.exists():
            shutil.rmtree(self.out)
        (self.out / "boots").mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.out, ignore_errors=True))
        self.path = self.out / "boots" / "W2-A-dec-r1.evidence.json"

    def write(self, refresh=False, **checks):
        return LEGS.write_boot_evidence(self.out, "W2-A-dec-r1", fake_checks(**checks),
                                        refresh=refresh)

    def test_first_write_is_written_and_reports_so(self):
        state, path = self.write()
        self.assertEqual(state, "wrote")
        self.assertEqual(path, self.path)
        self.assertTrue(self.path.is_file())
        self.assertEqual(json.loads(self.path.read_text())["page_cache"]["cached_mib_now"], 14007)

    def test_second_write_keeps_the_recorded_file_byte_for_byte(self):
        self.write(cached_now=14007, majfault_now=9787947)
        before = self.path.read_bytes()
        state, path = self.write(cached_now=16812, majfault_now=3452753)
        self.assertEqual(state, "kept")
        self.assertEqual(path, self.path)
        self.assertEqual(self.path.read_bytes(), before,
                         "a --check re-run must not overwrite a finished window's evidence")

    def test_the_kept_file_still_carries_the_original_numbers(self):
        # This is the 2026-11-25 incident in miniature: a second read reports different
        # box state, and the file must keep the first reading.
        self.write(cached_now=14007, majfault_now=9787947)
        self.write(cached_now=16812, majfault_now=3452753)
        page = json.loads(self.path.read_text())["page_cache"]
        self.assertEqual(page["cached_mib_now"], 14007)
        self.assertEqual(page["majfault_delta"], 9787947)
        self.assertNotEqual(page["majfault_delta"], 3452753)

    def test_refresh_recomputes_on_request(self):
        self.write(cached_now=14007)
        state, _ = self.write(refresh=True, cached_now=16812)
        self.assertEqual(state, "wrote")
        self.assertEqual(json.loads(self.path.read_text())["page_cache"]["cached_mib_now"], 16812)

    def test_the_module_default_is_never_refresh(self):
        # Anything importing port_ab_legs without touching the flag must get the safe default.
        self.assertFalse(LEGS.EVIDENCE_REFRESH)


class CheckKeepsEvidence(unittest.TestCase):
    """End to end through check(): the command that actually rewrote the W2 window."""

    def setUp(self):
        self.out = SCRATCH / ("check-" + self._testMethodName)
        if self.out.exists():
            shutil.rmtree(self.out)
        for name in ("boots", "records", "logs", "cache", "run", "corpus"):
            (self.out / name).mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.out, ignore_errors=True))
        self.key = "W2-A-dec-r1"
        # A boot record is enough for boot_checks; the leg record below keeps check() running.
        (self.out / "boots" / (self.key + ".json")).write_text(json.dumps({
            "key": self.key, "arm": "A", "rep": 1, "boot": "dec", "started_epoch": 1795543000,
            "tuple": {"--vram-reserve-mib": "256"}, "page_cache": {"cached_mib": 10674,
                                                                   "pgmajfault": 10}}))

    def test_a_finished_windows_evidence_survives_a_rerun(self):
        path = self.out / "boots" / (self.key + ".evidence.json")
        path.write_text(json.dumps({"key": self.key, "written_at": "2026-11-24T19:45:31",
                                    "written_by": "port_ab_legs.py --check",
                                    "page_cache": {"cached_delta_mib": 6138,
                                                   "majfault_delta": 3452753},
                                    "problems": [], "warnings": []}, indent=1) + "\n")
        before = path.read_bytes()
        checks = LEGS.boot_checks(self.out, self.key, {"arm": "A", "leg": "dec144k"})
        state, _ = LEGS.write_boot_evidence(self.out, self.key, checks)
        self.assertEqual(state, "kept")
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

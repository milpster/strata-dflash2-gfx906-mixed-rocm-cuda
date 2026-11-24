#!/usr/bin/env python3
"""CPU-only tests for port_ab_prune.py and the cache purge at stop. No GPU, no endpoint.

Every fixture lives under out/tests-tmp (project-local, removed when the test ends): the
measurement tree out/port-ab-20261123 holds completed evidence and is never written here.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import unittest

RPC = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RPC))
import port_ab_prune as PRUNE  # noqa: E402

SCRATCH = RPC / "out" / "tests-tmp"


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.out = SCRATCH / ("prune-" + self._testMethodName)
        if self.out.exists():
            shutil.rmtree(self.out)
        for name in ("boots", "records", "logs", "cache", "run"):
            (self.out / name).mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.out, ignore_errors=True))

    def boot(self, key, *, cache_mib=1, record=True, ended=True, leg=True, socket=False):
        cache = self.out / "cache" / key
        cache.mkdir(parents=True)
        (cache / "conv.bin").write_bytes(b"x" * (cache_mib * (1 << 20)))
        if record:
            body = {"key": key, "started_epoch": 1795000000}
            if ended:
                body["ended_epoch"] = 1795003600
            (self.out / "boots" / (key + ".json")).write_text(json.dumps(body))
        if leg:
            (self.out / "records" / key).mkdir(parents=True)
            (self.out / "records" / key / (key.split("-")[1] + "-pp239k.json")).write_text("{}")
        (self.out / "logs" / (key + ".engine.log")).write_text("judge line\n")
        if socket:
            (self.out / "run" / ("pa-" + key + ".sock")).write_text("")
        return cache

    def rows(self):
        return {row["key"]: row for row in PRUNE.scan(self.out)}

    def test_completed_boot_is_removable(self):
        self.boot("W9-A-pp-r1")
        self.assertEqual(self.rows()["W9-A-pp-r1"]["action"], "remove")

    def test_no_record_is_kept(self):
        self.boot("W9-A-pp-r2", record=False)
        self.assertIn("no boot record", self.rows()["W9-A-pp-r2"]["reason"])

    def test_open_record_is_kept(self):
        self.boot("W9-A-pp-r3", ended=False)
        self.assertIn("no ended_epoch", self.rows()["W9-A-pp-r3"]["reason"])

    def test_live_socket_is_kept(self):
        self.boot("W9-B-pp-r0", socket=True)
        self.assertIn("socket", self.rows()["W9-B-pp-r0"]["reason"])

    def test_symlink_is_never_followed(self):
        outside = SCRATCH / "prune-outside"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "keep.txt").write_text("evidence")
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        (self.out / "cache" / "W9-A-pp-r9").symlink_to(outside)
        self.assertEqual(self.rows()["W9-A-pp-r9"]["action"], "keep")
        self.assertTrue((outside / "keep.txt").is_file())

    def test_remove_refuses_paths_outside_the_cache_root(self):
        target = self.out / "records" / "W9-A-pp-r1"
        target.mkdir(parents=True)
        with self.assertRaises(SystemExit):
            PRUNE.remove_cache(self.out, target)
        self.assertTrue(target.is_dir())

    def test_purge_key_removes_cache_and_keeps_evidence(self):
        cache = self.boot("W9-A-pp-r1")
        self.assertEqual(PRUNE.purge_key(self.out, "W9-A-pp-r1"), 0)
        self.assertFalse(cache.exists())
        self.assertTrue((self.out / "boots" / "W9-A-pp-r1.json").is_file())
        self.assertTrue((self.out / "records" / "W9-A-pp-r1").is_dir())
        self.assertTrue((self.out / "logs" / "W9-A-pp-r1.engine.log").is_file())

    def test_purge_key_refuses_an_open_boot(self):
        self.boot("W9-A-pp-r3", ended=False)
        self.assertEqual(PRUNE.purge_key(self.out, "W9-A-pp-r3"), 1)
        self.assertTrue((self.out / "cache" / "W9-A-pp-r3").is_dir())

    def test_purge_key_quiet_proven_ignores_a_stale_socket(self):
        self.boot("W9-C-pp-r0", socket=True)
        self.assertEqual(PRUNE.purge_key(self.out, "W9-C-pp-r0"), 1)
        self.assertEqual(PRUNE.purge_key(self.out, "W9-C-pp-r0", quiet_proven=True), 0)
        self.assertFalse((self.out / "cache" / "W9-C-pp-r0").exists())

    def test_human_matches_du_shape(self):
        self.assertEqual(PRUNE.human(1024), "1.0 KiB")
        self.assertEqual(PRUNE.human(1536), "1.5 KiB")
        self.assertEqual(PRUNE.human(3 * (1 << 20)), "3.0 MiB")


if __name__ == "__main__":
    unittest.main()

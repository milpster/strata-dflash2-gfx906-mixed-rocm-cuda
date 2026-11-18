#!/usr/bin/env python3
"""CPU-only tests for the 2026-11-18 champions panel. No GPU, no endpoint, no engine.

  python3 tests/test_champions_cpu.py          # stdlib only
  python3 -m unittest discover -s tests -v     # same thing
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path

RPC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RPC))
sys.path.insert(0, str(RPC / "gpu-rounds"))
import champions_log as LOG  # noqa: E402
import champions_panel as PANEL  # noqa: E402
import champions_prepare as PREP  # noqa: E402

OUT = PREP.DEFAULT_OUT
MAIN = ("serve: prompt 16000 tokens = {r} reused + {k} read in {ms} ms (177.8 tok/s), "
        "512 generated in 21000 ms (24.4 tok/s), timeouts=0 fallbacks=0")
CONT = ("serve: prompt 1200 tokens = 0 reused + 1200 read in 100 ms (12.0 tok/s), "
        "8 generated in 400 ms (20.0 tok/s)")


def pair_windows(fresh_ms: float = 90000.0, cached_ms: float = 21000.0) -> tuple[bytes, bytes]:
    return (f"{MAIN.format(r=0, k=16000, ms=fresh_ms)}\n{CONT}\n".encode(),
            f"{CONT}\n{MAIN.format(r=16000, k=0, ms=cached_ms)}\n".encode())


class Corpus(unittest.TestCase):
    def test_exact_bytes_and_determinism(self):
        for shape in PREP.SHAPES:
            for pair in range(shape.pairs):
                a = PREP.build_paired_prompt(shape.name, pair, shape.ctx)
                b = PREP.build_paired_prompt(shape.name, pair, shape.ctx)
                self.assertEqual(len(a), shape.ctx)
                self.assertEqual(a, b)

    def test_prompt_identical_across_arms(self):
        """Arms must read byte-identical input: the corpus is not arm-parameterised."""
        for entry in json.loads((OUT / "manifest.json").read_text())["corpus"]:
            on_disk = Path(entry["path"]).read_bytes()
            self.assertEqual(on_disk, PREP.build_paired_prompt(entry["shape"], entry["pair"],
                                                               entry["bytes"]))
            self.assertEqual(len(on_disk), entry["bytes"])
            self.assertEqual(PREP.sha256_bytes(on_disk), entry["sha256"])

    def test_distinct_early_prefix_per_repeat(self):
        """The identity tag is at the HEAD: that is what actually defeats prefix reuse."""
        prompts = {pair: PREP.build_paired_prompt("short16k", pair, 88400)
                   for pair in range(4)}
        tags = {p.split(b"\n", 1)[0] for p in prompts.values()}
        self.assertEqual(len(tags), 4)
        for prompt in prompts.values():
            self.assertTrue(prompt.startswith(b"[champs work=short16k pair="))
        # differing only in the tail would leave the leading tokens cache-hit-able
        self.assertEqual(len({p[:200] for p in prompts.values()}), 4)

    def test_no_arm_name_inside_prompt(self):
        for name in PREP.ARM_ORDER:
            for entry in json.loads((OUT / "manifest.json").read_text())["corpus"]:
                self.assertNotIn(name.encode(), Path(entry["path"]).read_bytes())


class Driver(unittest.TestCase):
    # Pinned from the untouched implementation, so an edit to build_prompt that changes
    # existing ab9 leg bytes fails here rather than silently invalidating old results.
    LEGACY_SHA = {
        ("A", "8000-1"): "863b264e228d2fc8ccb10dbb2394ef497942e25059500078ece02db30e33ac0c",
        ("m2", "80000-1"): "acb0359c03d7192ef6d53988e8ceeb3281eda134878a5d311d662b515e248afe",
    }

    def test_legacy_builder_stable(self):
        driver = __import__("ab9_driver")
        for (arm, leg), digest in self.LEGACY_SHA.items():
            ctx = 8000 if leg == "8000-1" else 80000
            prompt = driver.build_prompt(arm, leg, ctx)
            self.assertEqual(len(prompt.encode()), ctx)
            self.assertTrue(prompt.rstrip().endswith(f"[ab9 arm={arm} leg={leg} ctx={ctx}]"))
            self.assertEqual(PREP.sha256_bytes(prompt.encode()), digest)

    def test_bad_prompt_length_rejected_before_any_socket(self):
        """A --prompt-file whose size disagrees with --ctx must fail closed, no request.

        --url points at a closed local port, never 8009, so even a harness bug could not
        put traffic on the production backend from a test.
        """
        corpus = json.loads((OUT / "manifest.json").read_text())["corpus"][0]["path"]
        result = subprocess.run(
            [sys.executable, str(RPC / "ab9_driver.py"), "--arm", "x", "--leg", "l",
             "--kind", "tg", "--ctx", "99", "--prompt-file", str(corpus),
             "--url", "http://127.0.0.1:1/v1/chat/completions",
             "--out", str(RPC / "out" / "champions-20261118" / "logs"),
             "--elog", "/nonexistent.elog", "--hlog", "/nonexistent.hlog"],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prompt-file length", result.stderr + result.stdout)

    def test_pinned_prompt_sha_enforced(self):
        corpus = Path(json.loads((OUT / "manifest.json").read_text())["corpus"][0]["path"])
        result = subprocess.run(
            [sys.executable, str(RPC / "ab9_driver.py"), "--arm", "x", "--leg", "l",
             "--kind", "tg", "--ctx", str(corpus.stat().st_size), "--prompt-file", str(corpus),
             "--prompt-sha256", "0" * 64,
             "--url", "http://127.0.0.1:1/v1/chat/completions",
             "--out", str(RPC / "out" / "champions-20261118" / "logs"),
             "--elog", "/nonexistent", "--hlog", "/nonexistent"],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prompt-file sha256", result.stderr + result.stdout)


class _NoNetwork:
    """Fails the moment anything in the module under test opens a socket."""

    def __enter__(self):
        self._socket, self._urlopen = socket.socket, urllib.request.urlopen

        def refuse(*_a, **_k):
            raise AssertionError("network use in a CPU-only path")

        socket.socket = refuse
        urllib.request.urlopen = refuse
        return self

    def __exit__(self, *_exc):
        socket.socket, urllib.request.urlopen = self._socket, self._urlopen


class LogParser(unittest.TestCase):
    def test_ignores_internal_continuation_lines(self):
        fresh, cached = pair_windows()
        first, second, unmatched = LOG.match_pair(LOG.parse_window(fresh), LOG.parse_window(cached))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(len(unmatched), 2, "the two continuation lines must be reported, not used")
        self.assertEqual((first.reused, first.read), (0, 16000))
        self.assertEqual((second.reused, second.read), (16000, 0))

    def test_refuses_ambiguous_window(self):
        noisy = LOG.parse_window(f"{MAIN.format(r=0, k=16000, ms=1)}\n"
                                 f"{MAIN.format(r=0, k=16000, ms=2)}\n".encode())
        noisy_cached = LOG.parse_window(f"{MAIN.format(r=16000, k=0, ms=3)}\n"
                                        f"{MAIN.format(r=16000, k=0, ms=4)}\n".encode())
        first, second, unmatched = LOG.match_pair(noisy, noisy_cached)
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(unmatched), 4)

    def test_fresh_and_cached_are_never_swapped(self):
        """A reused first leg is not fresh PP; a zero-read first leg is not a prefill."""
        first, second = pair_windows()
        reused_fresh = LOG.parse_window(f"{MAIN.format(r=16000, k=0, ms=1)}\n".encode())
        cached = LOG.parse_window(f"{MAIN.format(r=16000, k=0, ms=1)}\n".encode())
        leg = LOG.classify(_blank_leg(), reused_fresh[0], cached[0])
        self.assertFalse(leg.pp_usable)
        self.assertEqual(leg.fresh_reason, "fresh-leg-reused-16000")
        zero = LOG.parse_window(f"{MAIN.format(r=0, k=0, ms=1)}\n".encode())
        leg = LOG.classify(_blank_leg(), zero[0], cached[0])
        self.assertFalse(leg.pp_usable)
        self.assertEqual(leg.fresh_reason, "fresh-leg-zero-read")
        self.assertTrue(second)

    def test_summary_drops_missing_and_reused_pp(self):
        fresh, cached = pair_windows()
        fresh_pass = LOG.parse_window(fresh)[0]
        cached_pass = LOG.parse_window(cached)[1]

        good = _blank_leg("short16k-p1")
        good.fresh, good.cached = fresh_pass, cached_pass

        reused = _blank_leg("short16k-p2")          # a cache hit is not fresh PP
        reused.fresh, reused.cached = fresh_pass, cached_pass
        reused.fresh_reason = "fresh-leg-reused-16384"

        missing = _blank_leg("short16k-p3")          # no cached timing line at all
        missing.fresh = fresh_pass

        shape = LOG.summarise([good, reused, missing])["shapes"]["short16k"]
        # Partial credit is deliberate and per-metric: a leg with a cache-contaminated fresh
        # line still contributes a valid cached TG, and a leg with no cached line still
        # contributes a valid fresh PP. Only the broken measurement is dropped.
        self.assertEqual(shape["pp_tok_s"]["n"], 2)   # good + missing
        self.assertEqual(shape["tg_tok_s"]["n"], 2)   # good + reused
        self.assertEqual({d["leg"] for d in shape["pairs_dropped"]},
                         {"short16k-p2", "short16k-p3"})
        self.assertEqual(shape["fresh_reused"], [0])

    def test_medians_ignore_outliers_rather_than_mean_them(self):
        line = ("serve: prompt 16000 tokens = 0 reused + 16000 read in 100 ms "
                "({tps} tok/s), 512 generated in 21000 ms (24.4 tok/s)")
        cached = LOG.parse_window(pair_windows()[1])[1]
        legs = []
        for name, tps in [("a", 90.0), ("b", 180.0), ("c", 200.0)]:
            leg = _blank_leg(name)
            leg.fresh = LOG.parse_window(line.format(tps=tps).encode())[0]
            leg.cached = cached
            legs.append(leg)
        shape = LOG.summarise(legs)["shapes"]["short16k"]
        self.assertEqual(shape["pp_tok_s"]["median"], 180.0)
        self.assertEqual(shape["pp_tok_s"]["spread"], 110.0)

    def test_leg_round_trips_through_jsonl(self):
        leg = LOG.classify(_blank_leg(), *
                           [LOG.parse_window(pair_windows()[0])[0],
                            LOG.parse_window(pair_windows()[1])[1]])
        restored = LOG.LegResult.from_dict(json.loads(json.dumps(leg.to_dict())))
        self.assertEqual(restored.to_dict(), leg.to_dict())
        self.assertTrue(restored.pp_usable and restored.tg_usable)


def _blank_leg(name: str = "short16k-p0") -> LOG.LegResult:
    return LOG.LegResult(arm="current", leg=name, shape="short16k", pair=1,
                         measured=True, max_tokens=512)


class FrozenArtifacts(unittest.TestCase):
    def test_dry_run_never_touches_network_or_process_control(self):
        """The default path must not request, not kill, and not start anything."""
        blocked = _NoNetwork()
        blocked.__enter__()
        self.addCleanup(blocked.__exit__, None, None, None)
        real_run, real_popen = subprocess.run, subprocess.Popen
        seen: list[list[str]] = []

        def spy_run(cmd, *a, **k):
            seen.append([str(part) for part in cmd])
            return real_run(cmd, *a, **k)

        def forbidden_popen(*_a, **_k):
            raise AssertionError("dry-run must not spawn processes it needs")

        subprocess.run, subprocess.Popen = spy_run, forbidden_popen
        try:
            self.assertEqual(PANEL.dry_run(OUT), 0)
        finally:
            subprocess.run, subprocess.Popen = real_run, real_popen
        for cmd in seen:
            self.assertTrue(cmd[0].startswith("git"), f"unexpected subprocess in dry-run: {cmd}")

    def test_launcher_names_avoid_the_sweep(self):
        for name in PREP.ARM_ORDER:
            launcher = OUT / "bin" / f"champs-{name}.sh"
            self.assertTrue(launcher.is_file())
            for part in PREP.FORBIDDEN_NAME_PARTS:
                self.assertNotIn(part, launcher.name)

    def test_launcher_retarget_is_minimal_and_canonical_state_kept(self):
        text = (OUT / "bin" / "champs-nov16.sh").read_text()
        self.assertIn(f'BIN="{OUT / "slots" / "nov16"}"', text)
        self.assertIn(f'CONFIG="{OUT / "configs" / "nov16.json"}"', text)
        self.assertIn(f'SOCKET="{OUT / "run" / "nov16.sock"}"', text)
        # stop-all finds the pair through the canonical shared state/lock: never retargeted
        self.assertIn('--state "$ROOT/run/prod-swift15-f16kv-mixed-supervisor.json"', text)
        self.assertIn('--lock "$ROOT/run/mixed-supervisor.lock"', text)

    def test_restore_target_is_the_canonical_production_launcher(self):
        restore = (RPC / "gpu-rounds" / "champions_restore.sh").read_text()
        self.assertIn("/mnt/srcds/Strata/launch-v0238.sh", restore)
        self.assertIn("prod-config-v0238.json", restore)

    def test_slots_point_at_the_era_binaries(self):
        for arm in PREP.ARMS:
            link = OUT / "slots" / arm.name / "rpcw-hip" / "strata"
            self.assertEqual(link.resolve(), arm.engine)

    def test_config_quality_keys_identical_and_capacity_frozen(self):
        manifest = json.loads((OUT / "manifest.json").read_text())
        self.assertTrue(manifest["config_identical_across_arms"])
        for arm in manifest["arms"].values():
            self.assertEqual(arm["quality_identity"]["--max-context"], "262144")
            self.assertEqual(arm["quality_identity"]["--kv"], "int8")
            self.assertEqual(arm["quality_identity"]["--conversation-cache-disk-mib"], "24576")


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""CPU-only tests for the offline RAM-acceptance helper. No GPU, no endpoint, no engine.

  python3 tests/test_ram_acceptance_cpu.py        # stdlib only
The helper is exercised directly (not re-implemented here). Fixtures live under the
project-local, git-ignored out/tests-tmp; the real benchmark/cache trees are untouched.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import socket
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

# pyright: reportMissingImports=false
RPC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RPC))
sys.path.insert(0, str(RPC / "gpu-rounds"))
import ram_acceptance as RA  # noqa: E402  the module under test

PROD_CONFIG = Path("/mnt/srcds/Strata/prod-config-v0238.json")
SCRATCH = RPC / "out" / "tests-tmp"


def fresh_scratch(case: unittest.TestCase) -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=SCRATCH))
    case.addCleanup(shutil.rmtree, root, True)
    return root


def base_config() -> dict:
    return json.loads(PROD_CONFIG.read_text())


def build_arm(base: dict, arm: str, out: Path, **over):
    """Build one matched arm config with isolated paths under `out`."""
    exe = over.pop("exe", str(out / "slots" / arm / "rpcw-hip" / "strata"))
    return RA.build_test_config(
        base, arm=arm, out_dir=out, exe=exe,
        socket1=str(out / "run" / f"{arm}.sock"),
        socket2=str(out / "run" / f"{arm}2.sock"),
        cache_dir=str(out / "cache" / arm),
        engine_log=str(out / "logs" / f"{arm}.engine.log"), **over)[0]


class _NoNetwork:
    """Fails the moment anything in the code under test opens a socket."""

    def __enter__(self):
        self._socket, self._urlopen = socket.socket, urllib.request.urlopen

        def refuse(*_a, **_k):
            raise AssertionError("network use in a CPU-only path")

        socket.socket = refuse
        urllib.request.urlopen = refuse
        return self

    def __exit__(self, *_exc):
        socket.socket, urllib.request.urlopen = self._socket, self._urlopen


# ---- canned actual-source log lines (format verified against generate.cpp) ------

PARK_PHYS = ("strata serve: conversation cache: skip parking (physical RAM admission; "
             "need 512 MiB plus 2560 MiB floor, MemAvailable 100 MiB; evicted 0, 2 still parked)")
PARK_HEADROOM = ("strata serve: conversation cache: skip parking (restore headroom; "
                 "2560 MiB floor, 120 MiB available)")
CKPT_SKIP = ("strata serve: checkpoint at 16000 skipped: physical RAM (the copy needs "
             "400 MiB beside the 2560 MiB floor, 100 MiB)")
CKPT_SKIP_HEADROOM = "strata serve: checkpoint at 16000 skipped: restore headroom"
RESTORE_SKIPPED = ("strata serve: conversation cache: disk restore skipped (manifest geometry "
                   "mismatch); rereading the prompt")
PROMPT_LINE = ("strata serve: prompt 16000 tokens = 0 reused + 16000 read in 90000 ms "
               "(177.8 tok/s), 512 generated in 21000 ms (24.4 tok/s), timeouts=0 fallbacks=0")
PROMPT_CACHED = ("strata serve: prompt 16000 tokens = 16000 reused + 0 read in 100 ms "
                 "(160.0 tok/s), 512 generated in 21000 ms (24.4 tok/s), timeouts=0 fallbacks=0")
PUB = ("strata serve: conversation cache: disk park streamed 32000 tokens chat to /cache/gen.bin "
       "in 12.3 ms; generations=1 bytes=2048 evicted=0 superseded=0 sidecars=0 anchor=16000 "
       "pair=15000 tail-draft=1 pages-released=0 (0 MiB released)")
RESTORED = ("strata serve: conversation cache: restored 32000 tokens from disk (/cache/gen.bin) "
            "in 12.3 ms: complete target+draft hit (pair: tail ring)")
GUARD_ADMIT = "strata serve: conversation cache: disk tier RAM guard admitted: 120 MiB available"
CKPT_EVICT = ("strata serve: checkpoint at 16000: evicted 3 parked conversations for its "
              "400 MiB host copy")


def blob(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


# =============================================================================
# 1. CONFIG + PROVENANCE
# =============================================================================

class Config(unittest.TestCase):
    def test_floor_default_and_explicit(self):
        base = base_config()
        self.assertEqual(RA.min_free_floor_mib(base), 2560)  # absent flag == 2560 default
        raised = {"args": base["args"] + ["--conversation-cache-min-free-mib", "1024"]}
        self.assertEqual(RA.min_free_floor_mib(raised), 1024)
        with self.assertRaises(RA.ConfigRejected):
            RA.build_test_config(raised, arm="x", out_dir=Path("/tmp/x"), exe="/e",
                                 socket1="/tmp/x/a.sock", socket2="/tmp/x/b.sock",
                                 cache_dir="/tmp/x/cache", engine_log="/tmp/x/l.log")

    def test_default_floor_rejected_when_mutable(self):
        """A non-default floor in the base config is rejected; the floor is never a knob."""
        base = base_config()
        base["args"] = base["args"] + ["--conversation-cache-min-free-mib", "3072"]
        with self.assertRaises(RA.ConfigRejected):
            build_arm(base, "baseline", fresh_scratch(self))

    def test_retarget_is_nonmutating(self):
        base = base_config()
        snapshot = json.dumps(base, sort_keys=True)
        out = fresh_scratch(self)
        build_arm(base, "baseline", out)
        self.assertEqual(json.dumps(base, sort_keys=True), snapshot)

    def test_matched_arms_equal_except_exe(self):
        out = fresh_scratch(self)
        base = base_config()
        a = build_arm(base, "baseline", out, exe="/slots/baseline/rpcw-hip/strata")
        b = build_arm(base, "candidate", out, exe="/slots/candidate/rpcw-hip/strata")
        self.assertEqual(RA.verify_test_pair(a, b), [])
        # exe is the only intended difference between the two engine identities
        self.assertNotEqual(a["exe"], b["exe"])

    def test_equal_helper_sockets_rejected(self):
        out = fresh_scratch(self)
        with self.assertRaises(RA.ConfigRejected):
            RA.build_test_config(base_config(), arm="x", out_dir=out, exe="/e",
                                 socket1=str(out / "run/a.sock"), socket2=str(out / "run/a.sock"),
                                 cache_dir=str(out / "cache/x"), engine_log=str(out / "logs/x.log"))

    def test_production_cache_and_socket_targets_rejected(self):
        out = fresh_scratch(self)
        cases = (
            {"cache_dir": "/home/srcds/strata-conv-cache"},
            {"socket1": "/mnt/srcds/Strata/run/prod-iq3xxs-ud-mixed.sock"},
            {"engine_log": "/mnt/srcds/Strata/logs/engine-prod-ud-iq3xxs-v0233.log"},
        )
        for case in cases:
            with self.subTest(**case):
                full = dict(exe="/e", socket1=str(out / "run/a.sock"),
                            socket2=str(out / "run/b.sock"), cache_dir=str(out / "cache/x"),
                            engine_log=str(out / "logs/x.log"))
                full.update(case)
                with self.assertRaises(RA.ConfigRejected):
                    RA.build_test_config(base_config(), arm="x", out_dir=out, **full)

    def test_duplicate_relevant_flags_rejected(self):
        base = base_config()
        base["args"] = base["args"] + ["--conversation-cache-dir", "/home/srcds/strata-conv-cache"]
        out = fresh_scratch(self)
        with self.assertRaises(RA.ConfigRejected):  # duplicate --conversation-cache-dir
            RA.build_test_config(base, arm="x", out_dir=out, exe="/e",
                                 socket1=str(out / "run/a.sock"), socket2=str(out / "run/b.sock"),
                                 cache_dir=str(out / "cache/x"), engine_log=str(out / "logs/x.log"))

    def test_forbidden_env_drift_rejected(self):
        out = fresh_scratch(self)
        with self.assertRaises(RA.ConfigRejected):
            build_arm(base_config(), "baseline", out, env={"STRATA_MEMORY_LIMIT_MIB": "1"})

    def test_state_hash_env_allowed_but_mismatch_between_arms_flagged(self):
        out = fresh_scratch(self)
        base = base_config()
        a = build_arm(base, "baseline", out, env={"STRATA_STATE_HASH": "1"})
        b = build_arm(base, "candidate", out)  # diagnostics must match; here only one arm set it
        problems = RA.verify_test_pair(a, b)
        self.assertTrue(any("env" in p for p in problems), problems)
        c = build_arm(base, "candidate", out, env={"STRATA_STATE_HASH": "1"})
        self.assertEqual(RA.verify_test_pair(a, c), [])

    def test_provenance_hash_and_publicness(self):
        out = fresh_scratch(self)
        files = {}
        for role, content in (("baseconfig", b"{}"), ("launcher", b"#!/bin/sh\n"),
                              ("helper", b"\x7fELF helper"), ("engine", b"\x7fELF engine"),
                              ("server", b"serverpy"), ("control", b"controlpy")):
            path = out / f"{role}.bin"
            path.write_bytes(content)
            files[role] = path
        manifest = RA.provenance_manifest(files)
        for role, path in files.items():
            self.assertEqual(manifest["files"][role]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertTrue(RA.manifest_is_public(manifest))
        # provenance drift: mutate one supplied file, its hash no longer matches the manifest
        (out / "engine.bin").write_bytes(b"\x7fELF changed")
        self.assertNotEqual(RA.provenance_manifest(files)["files"]["engine"]["sha256"],
                            manifest["files"]["engine"]["sha256"])

    def test_no_secrets_in_manifest(self):
        manifest = {"env": {"STRATA_PASSWORD": "hunter2"}, "files": {}}
        self.assertFalse(RA.manifest_is_public(manifest))


# =============================================================================
# 2. LOG / PREDICATE SUPPORT
# =============================================================================

class LogPredicates(unittest.TestCase):
    def test_operation_totals_count_all_reasons(self):
        # candidate refuses park for BOTH physical-RAM and restore-headroom reasons;
        # totals stay comparable (reason-independent) while headroom is a diagnostic.
        window = blob(PARK_PHYS, PARK_HEADROOM, CKPT_SKIP, CKPT_SKIP_HEADROOM, RESTORE_SKIPPED)
        ops = RA.operation_totals(window)
        self.assertEqual(ops["totals"], {"park_refusal": 2, "checkpoint_skip": 2, "restore_refusal": 1})
        self.assertEqual(ops["refusal_grand_total"], 5)
        self.assertEqual(ops["reasons"].get("_restore_headroom(diagnostic)"), 2)

    def test_engagement_and_prompt_split(self):
        window = blob(GUARD_ADMIT, CKPT_EVICT, PROMPT_LINE)
        ops = RA.operation_totals(window)
        self.assertEqual(ops["fix_only_engagement"], 2)
        self.assertEqual(ops["refusal_grand_total"], 0)
        passes = RA.prompt_passes(window)
        self.assertEqual((passes[0].reused, passes[0].read), (0, 16000))

    def test_publication_keeps_t_anchor_pair_distinct(self):
        pub = RA.parse_publication(blob(PUB))[0]
        self.assertEqual((pub["target_depth"], pub["anchor"], pub["pair_prefix"], pub["tail_draft"]),
                         (32000, 16000, 15000, True))

    def test_refusal_turn_does_not_require_restore_success(self):
        window = blob(PARK_PHYS, PROMPT_LINE)  # refusal, but NO restore success
        result = RA.classify_turn(window, RA.REFUSAL)
        self.assertEqual(result["verdict"], RA.PASS)
        self.assertFalse(RA.parse_restore(window)["successes"])

    def test_restore_success_turn_requires_restore_line(self):
        window = blob(PARK_PHYS, PROMPT_CACHED)  # has reused>0 but no restore success
        self.assertEqual(RA.classify_turn(window, RA.RESTORE_SUCCESS)["verdict"], RA.FAIL)

    def test_fallback_is_not_restore_engagement(self):
        # byte-eviction miss: reused must be 0 and no restore success on a FALLBACK turn
        self.assertEqual(RA.classify_turn(blob(PROMPT_LINE), RA.FALLBACK)["verdict"], RA.PASS)
        self.assertEqual(RA.classify_turn(blob(PROMPT_CACHED), RA.FALLBACK)["verdict"], RA.FAIL)
        self.assertEqual(RA.classify_turn(blob(RESTORED, PROMPT_LINE), RA.FALLBACK)["verdict"], RA.FAIL)

    def test_unengaged_pressure_is_never_pass(self):
        self.assertEqual(RA.pressure_engagement(blob(PROMPT_LINE))["verdict"], RA.UNENGAGED)
        self.assertEqual(RA.pressure_engagement(blob(GUARD_ADMIT))["verdict"], RA.PASS)
        self.assertEqual(RA.pressure_engagement(blob(PARK_HEADROOM))["verdict"], RA.PASS)
        # a warm restore with no guard/refusal line does not prove engagement
        self.assertEqual(RA.pressure_engagement(blob(RESTORED))["verdict"], RA.UNENGAGED)

    def test_raw_token_ids_and_state_comparisons(self):
        self.assertEqual(RA.compare_token_ids([1, 2, 3], [1, 2, 3])["verdict"], RA.PASS)
        self.assertEqual(RA.compare_token_ids([1, 2, 3], [1, 2, 4])["verdict"], RA.FAIL)
        self.assertEqual(RA.compare_token_ids([], [])["verdict"], RA.FAIL)
        keys = ["STATE_HASH"]
        self.assertEqual(RA.compare_state({"STATE_HASH": "ab"}, {"STATE_HASH": "ab"}, keys)["verdict"], RA.PASS)
        self.assertEqual(RA.compare_state({"STATE_HASH": "ab"}, {"STATE_HASH": "cd"}, keys)["verdict"], RA.FAIL)
        self.assertEqual(RA.compare_state({}, {"STATE_HASH": "ab"}, keys)["verdict"], RA.FAIL)
        self.assertEqual(RA.compare_state({"STATE_HASH": ""}, {"STATE_HASH": ""}, keys)["verdict"], RA.FAIL)
        self.assertEqual(RA.compare_state({"STATE_HASH": "ab"}, {"STATE_HASH": "ab"}, [])["verdict"], RA.FAIL)

    def test_no_network_in_predicate_paths(self):
        window = blob(PARK_PHYS, PROMPT_LINE, RESTORED)
        with _NoNetwork():
            RA.operation_totals(window)
            RA.classify_turn(window, RA.REFUSAL)
            RA.pressure_engagement(window)


# =============================================================================
# 3. EXACT EXPECTATIONS
# =============================================================================

class ExactRestore(unittest.TestCase):
    def test_correct_expectation_passes(self):
        window = blob(PUB, RESTORED, PROMPT_CACHED)
        exp = RA.RestoreExpectation(published_file="/cache/gen.bin", target_depth=32000,
                                    draft_class="pair: tail ring", expected_reused=16000,
                                    expected_read=0)
        self.assertEqual(RA.evaluate_restore(window, exp)["verdict"], RA.PASS)

    def test_wrong_file_rejected(self):
        window = blob(RESTORED)
        exp = RA.RestoreExpectation(published_file="/cache/other.bin", target_depth=32000)
        self.assertEqual(RA.evaluate_restore(window, exp)["verdict"], RA.FAIL)

    def test_shallower_and_wrong_depth_rejected(self):
        shallow = ("strata serve: conversation cache: restored 16000 tokens from disk "
                   "(/cache/gen.bin) in 5 ms: complete target hit (no drafter)")
        for depth in (16000, 40000):  # shallower than, and different from, the target
            with self.subTest(depth=depth):
                exp = RA.RestoreExpectation(published_file="/cache/gen.bin", target_depth=32000)
                self.assertEqual(RA.evaluate_restore(blob(shallow), exp)["verdict"], RA.FAIL)

    def test_wrong_declared_draft_class_rejected(self):
        window = blob(RESTORED)
        exp = RA.RestoreExpectation(published_file="/cache/gen.bin", target_depth=32000,
                                    draft_class="checkpoint at 15000")
        self.assertEqual(RA.evaluate_restore(window, exp)["verdict"], RA.FAIL)

    def test_restore_skipped_rejected(self):
        window = blob(RESTORE_SKIPPED)
        exp = RA.RestoreExpectation(published_file="/cache/gen.bin", target_depth=32000)
        result = RA.evaluate_restore(window, exp)
        self.assertEqual(result["verdict"], RA.FAIL)
        self.assertTrue(any("skipped" in p for p in result["problems"]))

    def test_reused_greater_than_zero_is_not_a_pass(self):
        window = blob(RESTORED, PROMPT_CACHED)  # reused 16000 but expectation wants 32000
        exp = RA.RestoreExpectation(target_depth=32000, expected_reused=32000)
        self.assertEqual(RA.evaluate_restore(window, exp)["verdict"], RA.FAIL)


# =============================================================================
# 4. EXISTING DRM READER GUARD (fixture proc_root; real read_fdinfo_clients reused)
# =============================================================================

PCI = "0000:03:00.0"


def write_fdinfo(out: Path, pid: int, name: str, pdev: str | None, client: str | None,
                 evicted: str | None):
    d = out / str(pid) / "fdinfo"
    d.mkdir(parents=True, exist_ok=True)
    lines: list[str] = ["pos:\t0", "flags:\t0102000"]
    if pdev is not None:
        lines.append(f"drm-pdev:\t{pdev}")
    if client is not None:
        lines.append(f"drm-client-id:\t{client}")
    if evicted is not None:
        lines.append(f"amd-evicted-vram:\t{evicted}")
    lines.append("drm-resident-vram:\t1 GiB")
    (d / name).write_text("\n".join(lines) + "\n")


class DrmGuard(unittest.TestCase):
    def test_positive_eviction_aborts(self):
        out = fresh_scratch(self)
        write_fdinfo(out, 4242, "7", PCI, "7", "5 MiB")
        self.assertEqual(RA.gpu_eviction_guard(4242, [PCI], str(out))["verdict"], RA.ABORT)

    def test_zero_eviction_with_all_pcis_passes(self):
        out = fresh_scratch(self)
        write_fdinfo(out, 4242, "7", PCI, "7", "0 B")
        self.assertEqual(RA.gpu_eviction_guard(4242, [PCI], str(out))["verdict"], RA.PASS)

    def test_zero_accepted_only_when_all_target_pcis_have_data(self):
        out = fresh_scratch(self)
        write_fdinfo(out, 4242, "7", PCI, "7", "0 B")
        # second required PCI has no fdinfo data at all
        self.assertEqual(RA.gpu_eviction_guard(4242, [PCI, "0000:04:00.0"], str(out))["verdict"],
                         RA.BLOCKED)

    def test_missing_eviction_gauge_blocks(self):
        out = fresh_scratch(self)
        write_fdinfo(out, 4242, "7", PCI, "7", None)  # no amd-evicted-vram field
        self.assertEqual(RA.gpu_eviction_guard(4242, [PCI], str(out))["verdict"], RA.BLOCKED)

    def test_read_error_blocks(self):
        out = fresh_scratch(self)
        # proc_root with no pid directory -> the reused reader reports a read error
        self.assertEqual(RA.gpu_eviction_guard(4242, [PCI], str(out / "missing"))["verdict"],
                         RA.BLOCKED)

    def test_missing_pci_identity_blocks(self):
        out = fresh_scratch(self)
        write_fdinfo(out, 4242, "7", None, "7", "0 B")  # client id present, pdev absent
        self.assertEqual(RA.gpu_eviction_guard(4242, [PCI], str(out))["verdict"], RA.BLOCKED)

    def test_duplicate_fd_collapse_to_one_client(self):
        out = fresh_scratch(self)
        write_fdinfo(out, 4242, "7", PCI, "7", "0 B")
        write_fdinfo(out, 4242, "8", PCI, "7", "0 B")  # same (pci, client) -> dedup, 2 fds
        result = RA.gpu_eviction_guard(4242, [PCI], str(out))
        self.assertEqual(result["verdict"], RA.PASS)
        self.assertEqual(len(result["clients"]), 1)
        self.assertEqual(len(result["clients"][0]["fds"]), 2)

    def test_no_required_pcas_blocks(self):
        self.assertEqual(RA.gpu_eviction_guard(4242, [], "/proc")["verdict"], RA.BLOCKED)

    def test_guard_opens_no_real_network(self):
        out = fresh_scratch(self)
        write_fdinfo(out, 4242, "7", PCI, "7", "0 B")
        with _NoNetwork():
            RA.gpu_eviction_guard(4242, [PCI], str(out))


if __name__ == "__main__":
    unittest.main(verbosity=2)

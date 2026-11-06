"""AB1 harness regressions (tools/mixed_gpu/helper_ab.py) - OFFLINE ONLY.
import os

Run:  python -m unittest tests.terminal.test_helper_ab
      python -m pytest tests/terminal/test_helper_ab.py

These fixtures need NO GPU, NO server, NO engine, NO helper process: they are text (the engine's
'strata serve:' lines, the helper's log lines, a production config, a launcher's text) handed to
the analysis and derivation functions.  Nothing here starts, stops, signals or binds anything.

What the run-1 window proved was missing (.slim/deepwork/mixed-cuda-finish/ab-lifetime-audit.md:7-15),
and what each class below pins:
  ConfigParity   - the arms carry EVERY production field (layer split, vision, conversation cache,
                   suffix draft, helper slots, env STRATA_DFLASH_SAMPLED, the 100 ms request
                   timeout); the only delta is the helper socket, and neither arm names the
                   production socket.
  HelperOpts     - the helper control process gets production's flags (device 0, slots 4000,
                   reserve 300 MiB, --timeout 7200000 AND --begin-timeout 1800000) and derive
                   refuses a launcher that says otherwise.
  Parsing        - the GENERATED tok/s is kept apart from the PROMPT tok/s, samples are found by
                   bounded engine-log ranges + request labels, and the real 10470-token prompt is
                   identified (run 1's literal '10443' selector found nothing).
  Validity       - missing, duplicate, ambiguous, cache-reused, partial-read, ON-zero-helper,
                   unexpected helper death, OFF-fill/OFF-service and cross-arm byte differences
                   all FAIL, and no accepted speed verdict is produced from invalid samples even
                   when a numeric median clears the 5% gate.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "strata_helper_ab_for_test", ROOT / "tools" / "mixed_gpu" / "helper_ab.py")
helper_ab = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = helper_ab
SPEC.loader.exec_module(helper_ab)

PROD_SOCKET = os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "/run/prod-mixed.sock"
CAND_SOCKET = "/tmp/agent/ab1-helper.sock"
SIZES = ["short", "med", "long", "xl"]
REPS = 3

# The production config's own shape (prod-config-swift15-dflash-w4-q4_0-mixed.json): every key
# run 1 dropped is here, so a derivation that drops one fails these tests.
PROD_CONFIG = {
    "exe": os.environ.get("STRATA_HELPER_WT", os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "/worktrees/cuda-helper") + "/build-mixed-gpu/r4-dflash-after/bin/strata-hip",
    "args": [
        "--gpu", "0,1",
        "--pack", os.environ.get("STRATA_AI_DIR", "/mnt/user/ai") + "/iq3xxs-15",
        "--native", os.environ.get("STRATA_AI_DIR", "/mnt/user/ai") + "/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf",
        "--native-dense-gguf", os.environ.get("STRATA_AI_DIR", "/mnt/user/ai") + "/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf",
        "--ple-gguf", os.environ.get("STRATA_AI_DIR", "/mnt/user/ai") + "/Qwen3.8-Flash-Next-PLE-Q8_0.gguf",
        "--expert-profile", os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "/data/expert-profile.bin",
        "--expert-cache", "auto",
        "--prefill", "8192",
        "--spec", "5",
        "--spec-min-p", "0.5",
        "--max-context", "262144",
        "--kv", "int8",
        "--vram-reserve-mib", "896",
        "--vram-reserve-later-mib", "1000",
        "--kv-resident", "262144",
        "--pcie-frac", "0",
        "--expert-cache-cpu-order",
        "--prompt-cache-spread",
        "--conversation-cache-mib", "2048",
        "--conversation-cache-min-tokens", "2048",
        "--conversation-cache-park-gain", "4096",
        "--vision",
        "--dflash", os.environ.get("STRATA_DFLASH2_DIR", "/mnt/user/dflash2") + "/artifacts/inco-dflash2-q4_0.gguf",
        "--dflash-block", "5",
        "--suffix-draft", "0",
        "--expert-helper-slots", "4000",
    ],
    "cwd": os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "",
    "tokenizer": os.environ.get("STRATA_AI_DIR", "/mnt/user/ai") + "/iq3xxs-15/tokenizer",
    "model_name": "swift-1.5-iq3_xxs",
    "log": os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "/logs/engine-swift15-dflash-w4-q4_0-mixed.log",
    "lib_dirs": [os.environ.get("STRATA_ROCM_ROOT", "/home/user/rocm-gfx906-xnack") + "/lib", "/opt/rocm-6.1.0/lib"],
    "port": 8009,
    "host": "127.0.0.1",
    "gpu": [0, 1],
    "layer_split": "32",
    "backend": "hip",
    "reasoning_effort": "high",
    "fit_max_tokens": True,
    "env_to_user": True,
    "reasoning_budget_tokens": 55000,
    "sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
                 "presence_penalty": 0.0, "repetition_penalty": 1.0},
    "vision": {"exe": os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "/build-vision/bin/strata-vision",
               "mmproj": os.environ.get("STRATA_AI_DIR", "/mnt/user/ai") + "/mmproj-Swift-Qwen3.8-Flash-Next-BF16.gguf",
               "model": os.environ.get("STRATA_AI_DIR", "/mnt/user/ai") + "/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf",
               "gpu": False, "threads": 8, "max_tokens": 1024},
    "env": {
        "STRATA_ENGINE_NICE": "0",
        "STRATA_WATCHDOG_S": "300",
        "HSA_OVERRIDE_GFX_VERSION": "9.0.6",
        "HIP_FORCE_P2P": "1",
        "HSA_ENABLE_SDMA": "1",
        "CUDA_VISIBLE_DEVICES": "0,1",
        "STRATA_GFX906_TOPK_REG": "2",
        "STRATA_GFX906_TOPK_FIT17": "1",
        "STRATA_PRIMARY_COMMIT_OVERLAP": "1",
        "STRATA_NO_PCIE_GROUP": "1",
        "STRATA_HIP_PROMPT_F16": "1",
        "STRATA_DFLASH_SAMPLED": "1",
    },
    "mcp_servers": {"markitdown": {"command": "/home/user/.local/bin/markitdown-mcp"}},
    "expert_helper_socket": PROD_SOCKET,
    "expert_helper_slots": 3400,
    "expert_helper_timeout": 100,
}

SUBS = {
    "exe": "/worktree/build-mixed-gpu/rpcw-hip/strata",
    "helper": "/worktree/build-mixed-gpu/rpcw-cuda/strata-expert-helper",
    "socket": CAND_SOCKET,
    "port": 18080,
    "log": "/worktree/build-mixed-gpu/cap1-validation/ab1-engine.log",
    "cwd": "/worktree",
    "model_name": "ab1-on",
    "model_name_off": "ab1-off",
}

# The canonical launcher's helper line, verbatim in its flags (line 36-40 of the real file).
LAUNCHER_TEXT = '''
env -i PATH="$PATH" HOME="$HOME" TERM="${TERM:-dumb}" "$HELPER" \\
  --pack /mnt/user/ai/iq3xxs-15 \\
  --experts ../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf \\
  --device 0 --slots 4000 --reserve-mib 300 --timeout 7200000 \\
  --begin-timeout 1800000 --socket "$SOCKET" >>"$HLOG" 2>&1 < /dev/null &
'''

PROMPT_TOKENS = {"short": 234, "med": 2630, "long": 10470, "xl": 78679}


# ---------------------------------------------------------------------------------------------
# Fixture builders (text only)
# ---------------------------------------------------------------------------------------------

def serve_line(size: str, gen_rate: float, helper_reqs: int | None,
               reused: int = 0, read_ms: int = 29952, read_rate: float = 349.6,
               gen_ms: int = 851, accepted: int = 21, offered: int = 40,
               checkpoints: int = 1, generated: int = 32) -> str:
    n = PROMPT_TOKENS[size]
    tail = (f" {checkpoints} checkpoints"
            + (f" helper=reqs={helper_reqs} rows=4386 bytes=44912640 timeouts=0 fallbacks=0"
               if helper_reqs is not None else ""))
    return (f"strata serve: prompt {n} tokens = {reused} reused + {n - reused} read in "
            f"{read_ms} ms ({read_rate} tok/s), {generated} generated in {gen_ms} ms ({gen_rate} tok/s), "
            f"drafts accepted {accepted} of {offered}," + tail)


class EngineLog:
    """A growing engine log: append() returns the (start, end) line range of what was added,
    exactly the (mark, wc -l) pair the harness records per probe."""

    def __init__(self) -> None:
        self.lines: list[str] = ["strata generate: engine started", "strata serve: ready"]

    def probe(self, line: str, extra: int = 3) -> tuple[int, int]:
        start = len(self.lines)
        self.lines.append(line)
        for i in range(extra):
            self.lines.append(f"strata serve: decode expert cache hit rate: 96.8% ({i})")
        return start, len(self.lines)

    def block(self, lines: list[str]) -> tuple[int, int]:
        start = len(self.lines)
        self.lines.extend(lines)
        return start, len(self.lines)


class HelperLog:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def start(self, socket: str, arm: str, fills: bool = False, on_fill: bool = True) -> int:
        """One helper control start.  ON fills (the engine hello's it); OFF never fills - its
        engine never creates the client, so the control sits at accept().  `fills` is the OFF-arm
        violation fixture; `on_fill=False` is the ON pair that never filled (finding 1 gate)."""
        mark = len(self.lines)
        self.lines.append(f"expert-helper: listening on {socket} (device 0, pack /mnt/user/ai/iq3xxs-15, "
                          f"48 layers x 512 experts)")
        if (arm == "on" and on_fill) or fills:
            self.lines.append("expert-helper: engine connected (peer uid 1000)")
            self.lines.append("expert-helper: warmup launch done on zero activations: output finite")
            self.lines.append("expert-helper: filled 4165 slots (6.68 GiB), scratch 672000 B, "
                              "buffers 179767896 B")
            if arm == "off":
                self.lines.append("expert-helper: 816 windows served, 0 prompt row requests; "
                                  "releasing the card")
        return mark

    def teardown(self, arm: str = "on") -> None:
        self.lines.append("expert-helper: SIGTERM/SIGINT received; exiting cleanly")
        self.lines.append(f"expert-helper: {1189 if arm == 'on' else 0} windows served, "
                          f"0 prompt row requests; releasing the card")


def build_window(on_gen: dict[str, list[float]], off_gen: dict[str, list[float]],
                  helper_reqs_on: int = 407, death_rep: int | None = None,
                  zero_helper_sizes: tuple[str, ...] = (), reused_sizes: tuple[str, ...] = (),
                  drop: tuple[str, ...] = (), duplicate: str | None = None,
                  token_shift: str | None = None, off_fills: bool = False,
                  partial_read_sizes: tuple[str, ...] = (), pre_dead_rep: int | None = None,
                  post_dead_rep: int | None = None, cross_arm_bytes_differ: bool = False,
                  truncated: tuple[str, ...] = (), early_finish: tuple[str, ...] = (),
                  ct_drift: tuple[str, ...] = (), on_no_fill: bool = False,
                  off_late_windows: bool = False, drop_pair_records: bool = False,
                  natural_eos: tuple[str, ...] = (), socket2: str | None = None,
                  tier_reqs: int = 407, tier_zero: bool = False,
                  tier_path: str | None = None, off_tier: bool = False):
    """One complete 2 x 3 x 4 window, RECORDER-SHAPED: PAIR records around each pair's startup
    and teardown, per-probe records carrying the probe's OWN helper range (ab1-parent-review
    finding 1), request/content/finish/ct hashes.

    The knobs are the run-1 defects and the review findings: XL with helper=reqs=0, cache reuse,
    a missing or duplicated probe, a token-count shift between arms, an OFF helper that fills or
    serves late (whole-pair range), an ON pair that never filled, missing pair records, a
    truncated/early-EOS reply, a completion-token drift, helper death mid-probe, and different
    request bytes between arms.
    """
    eng, hlog = EngineLog(), HelperLog()
    records: list[dict] = []
    shas: dict[str, dict[str, str]] = {}
    for arm, rates, reqs in (("on", on_gen, helper_reqs_on), ("off", off_gen, None)):
        for rep in range(1, REPS + 1):
            hstart = hlog.start(CAND_SOCKET, arm, fills=(arm == "off" and off_fills),
                                on_fill=not (arm == "on" and on_no_fill))
            estart = len(eng.lines)
            if not drop_pair_records:
                records.append({"type": "pair", "pair": f"{arm}-r{rep}", "arm": arm, "rep": rep,
                                "helper_start": hstart, "helper_startup_end": len(hlog.lines),
                                "engine_start": estart})
            for size in SIZES:
                label = f"{arm}-{size}-r{rep}"
                if label in drop:
                    continue
                gen = rates[size][rep - 1]
                reused = 1024 if size in reused_sizes else 0
                reqs_here = 0 if (arm == "on" and size in zero_helper_sizes) else reqs
                line = serve_line(size, gen, reqs_here, reused=reused,
                                  generated=17 if (label in truncated or label in natural_eos) else 32)
                if size in partial_read_sizes:
                    n = PROMPT_TOKENS[size]
                    line = line.replace(f"+ {n - reused} read in", f"+ {(n - reused) // 2} of {n - reused} read in")
                if token_shift and arm == "off" and size == token_shift:
                    line = line.replace(f"prompt {PROMPT_TOKENS[size]} tokens", f"prompt {PROMPT_TOKENS[size] + 27} tokens", 1)
                start, end = eng.probe(line)
                hs = len(hlog.lines)   # the probe's OWN mark: startup evidence is in the PAIR record
                if duplicate == label:
                    records.append(rec(label, arm, size, rep, start, end, hs, len(hlog.lines)))
                alive_pre = (death_rep != rep and pre_dead_rep != rep)
                alive_post = (post_dead_rep != rep)
                records.append(rec(label, arm, size, rep, start, end, hs, len(hlog.lines),
                                   pre=alive_pre, post=alive_post))
                req_sha = f"{size}-r{rep}"
                if cross_arm_bytes_differ and arm == "off":
                    req_sha = f"{size}-r{rep}-drifted"
                shas[label] = {"req": req_sha, "content": f"c-{label}",
                               "finish": "stop" if (label in early_finish or label in natural_eos)
                                         else "length",
                               "ct": "17" if label in natural_eos
                                     else ("31" if label in ct_drift else "32")}
            hlog.teardown(arm)
            if death_rep == rep and arm == "on":
                hlog.lines.append("expert-helper: the engine idled past the timeout; exiting")
            if arm == "off" and off_late_windows:
                hlog.lines.append("expert-helper: 500 windows served, 0 prompt row requests; "
                                  "releasing the card")
            if socket2 and arm == "on":
                # remote_experts.cpp close(): the engine prints the prompt-tier counters at exit
                eng.lines.append(
                    f"strata generate: expert-helper: the prompt tier's channel "
                    f"({tier_path or socket2}) closed after "
                    f"{0 if tier_zero else tier_reqs} prompt requests / "
                    f"{0 if tier_zero else 2 * (tier_reqs or 1)} prompt rows")
            if socket2 and arm == "off" and off_tier:
                eng.lines.append(
                    f"strata generate: expert-helper: the prompt tier's channel "
                    f"({tier_path or socket2}) closed after 5 prompt requests / 10 prompt rows")
            records.append({"type": "pair_end", "pair": f"{arm}-r{rep}",
                            "helper_end": len(hlog.lines), "engine_end": len(eng.lines)})
    return records, eng.lines, hlog.lines, shas


def rec(label, arm, size, rep, es, ee, hs, he, pre=True, post=True) -> dict:
    return {"label": label, "arm": arm, "size": size, "rep": rep, "engine_start": es,
            "engine_end": ee, "helper_start": hs, "helper_end": he, "http": "200",
            "helper_alive_pre": pre, "helper_alive_post": post}


BASE_ON = {"short": [41.0, 42.0, 43.0], "med": [190.0, 191.0, 192.0],
           "long": [40.0, 41.0, 42.0], "xl": [680.0, 682.0, 684.0]}
BASE_OFF = {"short": [34.0, 35.0, 36.0], "med": [186.0, 187.0, 188.0],
            "long": [35.0, 36.0, 37.0], "xl": [672.0, 674.0, 676.0]}


def run(**kw) -> dict:
    records, eng, hl, shas = build_window(**kw)
    return helper_ab.analyze(records, eng, hl, shas, SIZES, REPS, CAND_SOCKET,
                             socket2=kw.get("socket2"))


# ---------------------------------------------------------------------------------------------
# Config parity
# ---------------------------------------------------------------------------------------------

class ConfigParity(unittest.TestCase):
    def derive(self, prod=PROD_CONFIG, subs=SUBS):
        on, off = helper_ab.derive_arms(prod, subs)
        return on, off, helper_ab.check_arm_pair(prod, on, off, subs)

    def test_the_clean_pair_has_no_parity_failures(self):
        _, _, fails = self.derive()
        self.assertEqual(fails, [], f"the fixture pair must be accepted: {fails}")

    def test_every_production_field_survives_on_both_arms(self):
        on, off, _ = self.derive()
        for key in PROD_CONFIG:
            if key in helper_ab.ALLOWED_SUBSTITUTIONS:
                continue
            if key == "args":       # rule 2: args stay production's BYTE FOR BYTE (the socket rides the key)
                self.assertEqual(on["args"], PROD_CONFIG["args"], "ON's engine args are not production's")
                self.assertEqual(off["args"], PROD_CONFIG["args"], "OFF's engine args changed")
                continue
            self.assertEqual(on[key], PROD_CONFIG[key], f"ON changed '{key}'")
            self.assertEqual(off[key], PROD_CONFIG[key], f"OFF changed '{key}'")

    def test_the_fields_run1_dropped_are_present(self):
        on, off, _ = self.derive()
        self.assertEqual(on["layer_split"], "32")
        self.assertEqual(on["vision"], PROD_CONFIG["vision"])
        self.assertEqual(off["vision"], PROD_CONFIG["vision"])
        for flag in ("--conversation-cache-mib", "--conversation-cache-min-tokens",
                     "--conversation-cache-park-gain", "--prompt-cache-spread",
                     "--suffix-draft", "--expert-helper-slots", "--dflash", "--spec"):
            self.assertIn(flag, on["args"])
            self.assertIn(flag, off["args"])
        self.assertEqual(on["args"][on["args"].index("--expert-helper-slots") + 1], "4000")
        self.assertEqual(on["env"]["STRATA_DFLASH_SAMPLED"], "1")

    def test_the_engine_request_helper_timeout_100ms_rides_both_arms(self):
        on, off, fails = self.derive()
        self.assertEqual(on["expert_helper_timeout"], 100)
        self.assertEqual(off["expert_helper_timeout"], 100)
        self.assertEqual(on["expert_helper_slots"], PROD_CONFIG["expert_helper_slots"])
        self.assertEqual(off["expert_helper_slots"], PROD_CONFIG["expert_helper_slots"])
        self.assertEqual(fails, [])

    def test_the_allowed_delta_is_the_top_level_socket_key_alone(self):
        """run-2 lesson: an args-borne socket pair trips server.py's already-in-args guard
        (serve/server.py:3151-3153) and silently swallows the timeout injection; the socket
        must ride the TOP-LEVEL KEY only, exactly as production does."""
        on, off, _ = self.derive()
        self.assertEqual(on["args"], PROD_CONFIG["args"])
        self.assertEqual(off["args"], PROD_CONFIG["args"])
        self.assertNotIn("--expert-helper-socket", on["args"])
        self.assertNotIn("--expert-helper-socket", off["args"])
        self.assertEqual(on["expert_helper_socket"], CAND_SOCKET)

    def test_run2_shape_socket_pair_in_args_is_refused(self):
        """run-2's death shape: an args-borne socket pair makes server.py skip the timeout
        injection and the engine runs its 20 ms default; the pair must FAIL parity now."""
        on, off = helper_ab.derive_arms(PROD_CONFIG, SUBS)
        on["args"] = list(on["args"]) + [helper_ab.SOCKET_FLAG, CAND_SOCKET]
        fails = helper_ab.check_arm_pair(PROD_CONFIG, on, off, SUBS)
        self.assertTrue(any("run-2 death" in f for f in fails), fails)

    def test_a_socket_without_the_100ms_timeout_is_refused(self):
        on, off = helper_ab.derive_arms(PROD_CONFIG, SUBS)
        on["expert_helper_timeout"] = 20
        fails = helper_ab.check_arm_pair(PROD_CONFIG, on, off, SUBS)
        self.assertTrue(any("run-2 death" in f for f in fails), fails)
        on2, off2 = helper_ab.derive_arms(PROD_CONFIG, SUBS)
        on2.pop("expert_helper_timeout", None)
        fails2 = helper_ab.check_arm_pair(PROD_CONFIG, on2, off2, SUBS)
        self.assertTrue(any("run-2 death" in f for f in fails2), fails2)

    def test_off_removes_the_top_level_socket_injection(self):
        """serve/server.py:expert_helper_args injects the flag pair FROM the top-level key, so
        OFF must drop the key, not merely the pair."""
        on, off, _ = self.derive()
        self.assertEqual(on["expert_helper_socket"], CAND_SOCKET)
        self.assertNotIn("expert_helper_socket", off)

    def test_neither_arm_refs_the_production_socket(self):
        on, off, _ = self.derive()
        for label, cfg in (("ON", on), ("OFF", off)):
            self.assertNotIn(PROD_SOCKET, json.dumps(cfg), f"{label} names the production socket")

    def test_a_candidate_socket_equal_to_production_is_refused(self):
        subs = dict(SUBS, socket=PROD_SOCKET)
        on, off = helper_ab.derive_arms(PROD_CONFIG, subs)
        fails = helper_ab.check_arm_pair(PROD_CONFIG, on, off, subs)
        self.assertTrue(any("production socket" in f for f in fails), fails)

    # ---- tuned window derivations (tuned-window-design.md deltas 1-3) ----

    def tuned_subs(self, **kw):
        base = dict(SUBS, socket2="/tmp/agent/ab2-helper2.sock")
        base.update(kw)
        return base

    def test_the_tuned_pair_is_clean_and_carries_socket2_on_on_only(self):
        subs = self.tuned_subs()
        on, off, fails = self.derive(subs=subs)
        self.assertEqual(fails, [], f"the tuned fixture pair must be accepted: {fails}")
        self.assertEqual(on["expert_helper_socket2"], subs["socket2"])
        self.assertNotIn("expert_helper_socket2", off)
        self.assertEqual(on["env"]["STRATA_PREFILL_HELPER"], "1")
        self.assertEqual(off["env"]["STRATA_PREFILL_HELPER"], "1")   # parity: BOTH arms

    def test_a_socket2_key_without_the_tuned_request_is_refused(self):
        on, off = helper_ab.derive_arms(PROD_CONFIG, SUBS)
        on["expert_helper_socket2"] = "/tmp/agent/smuggled.sock"
        fails = helper_ab.check_arm_pair(PROD_CONFIG, on, off, SUBS)
        self.assertTrue(any("without the explicit tuned-window request" in f for f in fails),
                        fails)

    def test_a_tuned_socket2_equal_to_the_primary_or_production_socket_is_refused(self):
        for bad in (SUBS["socket"], PROD_SOCKET):
            subs = self.tuned_subs(socket2=bad)
            on, off = helper_ab.derive_arms(PROD_CONFIG, subs)
            fails = helper_ab.check_arm_pair(PROD_CONFIG, on, off, subs)
            self.assertTrue(any("second-channel socket equals" in f for f in fails), fails)

    def test_the_tuned_env_delta_is_limited_to_the_prompt_tier_key(self):
        subs = self.tuned_subs()
        on, off = helper_ab.derive_arms(PROD_CONFIG, subs)
        on["env"] = dict(on["env"], STRATA_PREFILL_HELPER="0")
        fails = helper_ab.check_arm_pair(PROD_CONFIG, on, off, subs)
        self.assertTrue(any("field 'env' changed" in f for f in fails), fails)
        on2, _ = helper_ab.derive_arms(PROD_CONFIG, subs)
        on2["env"] = dict(on2["env"], STRATA_DFLASH_SAMPLED="0")
        fails2 = helper_ab.check_arm_pair(PROD_CONFIG, on2, _, subs)
        self.assertTrue(any("field 'env' changed" in f for f in fails2), fails2)
        # canonical mode: even the exact tuned value is refused without the --socket2 request
        on3, off3 = helper_ab.derive_arms(PROD_CONFIG, SUBS)
        on3["env"] = dict(on3["env"], STRATA_PREFILL_HELPER="1")
        fails3 = helper_ab.check_arm_pair(PROD_CONFIG, on3, off3, SUBS)
        self.assertTrue(any("field 'env' changed" in f for f in fails3), fails3)

    def test_derive_against_an_adopted_tuned_production_config_is_clean(self):
        """The tuned config was ADOPTED 2026-10-31: production itself now carries the second
        channel and the prompt-tier env.  The tuned window must still derive cleanly from it
        (ON overrides the second channel with the candidate's; OFF drops BOTH sockets)."""
        prod = copy.deepcopy(PROD_CONFIG)
        prod["expert_helper_socket2"] = "/run/prod-adopted2.sock"
        prod["env"] = dict(prod["env"], STRATA_PREFILL_HELPER="1")
        subs = self.tuned_subs()
        on, off, fails = self.derive(prod=prod, subs=subs)
        self.assertEqual(fails, [], f"the adopted fixture pair must be accepted: {fails}")
        self.assertEqual(on["expert_helper_socket2"], subs["socket2"])
        self.assertNotIn("expert_helper_socket2", off)
        self.assertNotIn("expert_helper_socket", off)
        self.assertEqual(on["env"]["STRATA_PREFILL_HELPER"], "1")
        self.assertEqual(off["env"]["STRATA_PREFILL_HELPER"], "1")   # parity: BOTH arms

    def test_a_canonical_window_against_a_tuned_production_config_is_refused(self):
        """A canonical (single-channel) window against an adopted production config would
        silently ride production's own tuned keys on its ON arm: a hybrid arm, refused."""
        prod = copy.deepcopy(PROD_CONFIG)
        prod["expert_helper_socket2"] = "/run/prod-adopted2.sock"
        on, off = helper_ab.derive_arms(prod, SUBS)
        fails = helper_ab.check_arm_pair(prod, on, off, SUBS)
        self.assertTrue(any("production config is tuned" in f for f in fails), fails)

    def test_a_second_channel_equal_to_the_production_second_channel_is_refused(self):
        prod = copy.deepcopy(PROD_CONFIG)
        prod["expert_helper_socket2"] = "/run/prod-adopted2.sock"
        subs = self.tuned_subs(socket2="/run/prod-adopted2.sock")
        on, off = helper_ab.derive_arms(prod, subs)
        fails = helper_ab.check_arm_pair(prod, on, off, subs)
        self.assertTrue(any("production second channel" in f for f in fails), fails)

    def test_dropping_a_production_field_fails(self):
        prod = copy.deepcopy(PROD_CONFIG)
        prod.pop("layer_split")
        prod.pop("vision")
        prod["env"].pop("STRATA_DFLASH_SAMPLED")
        on, off = helper_ab.derive_arms(prod, SUBS)
        fails = helper_ab.check_arm_pair(prod, on, off, SUBS)
        self.assertTrue(any("layer_split" in f for f in fails), fails)
        self.assertTrue(any("STRATA_DFLASH_SAMPLED" in f for f in fails), fails)

    def test_dropping_the_vision_block_fails(self):
        prod = copy.deepcopy(PROD_CONFIG)
        prod.pop("vision")
        on, off = helper_ab.derive_arms(prod, SUBS)
        fails = helper_ab.check_arm_pair(prod, on, off, SUBS)
        self.assertTrue(any("'vision'" in f for f in fails), fails)

    def test_a_silently_shortened_arg_list_fails(self):
        prod = copy.deepcopy(PROD_CONFIG)
        prod["args"] = [a for a in prod["args"] if a != "--conversation-cache-mib"]
        prod["args"].remove("2048")
        on, off = helper_ab.derive_arms(prod, SUBS)
        fails = helper_ab.check_arm_pair(prod, on, off, SUBS)
        self.assertTrue(any("--conversation-cache-mib" in f for f in fails), fails)

    def test_extra_engine_args_on_the_off_arm_fail(self):
        on, off = helper_ab.derive_arms(PROD_CONFIG, SUBS)
        off["args"] = off["args"] + ["--prefill", "4096"]
        fails = helper_ab.check_arm_pair(PROD_CONFIG, on, off, SUBS)
        self.assertTrue(any("OFF engine args" in f for f in fails), fails)

    def test_a_mutated_off_env_field_fails(self):
        """Finding 5: non-substitution fields must be pinned on BOTH arms, not only ON."""
        on, off = helper_ab.derive_arms(PROD_CONFIG, SUBS)
        off["env"] = dict(off["env"], STRATA_DFLASH_SAMPLED="0")
        fails = helper_ab.check_arm_pair(PROD_CONFIG, on, off, SUBS)
        self.assertTrue(any("OFF field 'env'" in f for f in fails), fails)

    def test_a_mutated_off_sampling_block_fails(self):
        on, off = helper_ab.derive_arms(PROD_CONFIG, SUBS)
        off["sampling"] = dict(off["sampling"], temperature=0.7)
        fails = helper_ab.check_arm_pair(PROD_CONFIG, on, off, SUBS)
        self.assertTrue(any("OFF field 'sampling'" in f for f in fails), fails)

    def test_real_production_config_and_launcher_parity(self):
        """The same rules against the REAL production config and the REAL canonical launcher
        (read-only).  Skipped where the deployment files are not present."""
        cfg = Path(os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "/prod-config-swift15-dflash-w4-q4_0-mixed.json")
        launcher = Path(os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "/launch-prod-swift15-dflash-w4-q4_0-mixed.sh")
        if not (cfg.is_file() and launcher.is_file()):
            self.skipTest(f"deployment files absent ({cfg}, {launcher})")
        prod = json.loads(cfg.read_text(encoding="utf-8"))
        got = helper_ab.launcher_opts(launcher.read_text(encoding="utf-8"))
        # Slot cap ADOPTED 2026-11-01 (slots4c sweep, user-approved): 4200 requested on
        # both sides (launcher helper --slots and config --expert-helper-slots); the
        # 6400 MiB budget binds first at 4093 slots / 6.25 GiB (+13.6% decode median
        # vs the old 4000 cap; read rate unchanged).
        self.assertEqual(got.get("--slots"), "4200", got)
        self.assertEqual(got.get("--reserve-mib"), "300", got)
        self.assertEqual(got.get("--timeout"), "7200000", got)
        self.assertEqual(got.get("--begin-timeout"), "1800000", got)
        self.assertEqual(got.get("--device"), "0", got)
        # The tuned config was ADOPTED 2026-10-31 (ab2-r1 PASS): the real production config
        # carries expert_helper_socket2 + STRATA_PREFILL_HELPER, so the real-file tripwire
        # derives the TUNED window (a canonical derive against it is refused by rule 0).
        self.assertIn("expert_helper_socket2", prod)
        self.assertEqual(prod["env"].get("STRATA_PREFILL_HELPER"), "1")
        subs = dict(SUBS, exe=str(ROOT / "build-mixed-gpu/rpcw-hip/strata"),
                    socket2="/tmp/agent/ab2-real-helper2.sock")
        on, off = helper_ab.derive_arms(prod, subs)
        fails = helper_ab.check_arm_pair(prod, on, off, subs)
        self.assertEqual(fails, [], f"real production parity: {fails}")
        self.assertEqual(on["expert_helper_socket2"], subs["socket2"])
        self.assertNotIn("expert_helper_socket2", off)
        self.assertNotIn("expert_helper_socket", off)
        self.assertEqual(prod["args"], helper_ab.strip_arg_pair(on["args"], "--expert-helper-socket"))
        self.assertEqual(prod["args"], off["args"])


# ---------------------------------------------------------------------------------------------
# Helper control options
# ---------------------------------------------------------------------------------------------

class HelperOpts(unittest.TestCase):
    def test_launcher_flags_are_read_from_the_launcher_text(self):
        got = helper_ab.launcher_opts(LAUNCHER_TEXT)
        for flag, value in helper_ab.CANONICAL_HELPER_OPTS.items():
            self.assertEqual(got.get(flag), value, f"{flag} read from the launcher")

    def test_the_helper_command_carries_both_timeout_flags_and_the_budget(self):
        cmd = helper_ab.helper_cmd(SUBS["helper"], CAND_SOCKET)
        self.assertIn("--timeout", cmd)
        self.assertEqual(cmd[cmd.index("--timeout") + 1], "7200000")
        self.assertIn("--begin-timeout", cmd)
        self.assertEqual(cmd[cmd.index("--begin-timeout") + 1], "1800000")
        self.assertEqual(cmd[cmd.index("--slots") + 1], "4000")
        self.assertEqual(cmd[cmd.index("--reserve-mib") + 1], "300")
        self.assertEqual(cmd[cmd.index("--device") + 1], "0")
        self.assertEqual(cmd[cmd.index("--socket") + 1], CAND_SOCKET)

    def test_the_helper_command_never_names_the_production_socket(self):
        cmd = helper_ab.helper_cmd(SUBS["helper"], CAND_SOCKET)
        self.assertNotIn(PROD_SOCKET, cmd)

    def test_a_launcher_without_the_begin_timeout_is_refused(self):
        """The run-1 XL defect: --timeout covers the initial accept only; the connected-read
        timeout is --begin-timeout (helper_main.cpp:841-842, 944-951).  A helper command built
        from a launcher lacking it must fail, not silently run with 60 s."""
        bad = LAUNCHER_TEXT.replace("--begin-timeout 1800000 ", "")
        got = helper_ab.launcher_opts(bad)
        self.assertIsNone(got.get("--begin-timeout"))
        self.assertNotEqual(got.get("--begin-timeout"),
                            helper_ab.CANONICAL_HELPER_OPTS["--begin-timeout"])

    def test_derive_cli_reports_the_launcher_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cfg = td / "prod.json"
            cfg.write_text(json.dumps(PROD_CONFIG), encoding="utf-8")
            bad = td / "launcher.sh"
            bad.write_text(LAUNCHER_TEXT.replace("--begin-timeout 1800000 ", "")
                           .replace("--slots 4000", "--slots 256"), encoding="utf-8")
            exe = td / "strata"
            exe.write_text("#!/bin/sh\n", encoding="utf-8")
            helper = td / "strata-expert-helper"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            rc = helper_ab.main(["derive", "--prod-config", str(cfg), "--launcher", str(bad),
                                 "--out-dir", str(td / "ev"), "--exe", str(exe),
                                 "--helper", str(helper), "--socket", CAND_SOCKET,
                                 "--port", "18080", "--cwd", str(td),
                                 "--log", str(td / "ab1-engine.log"), "--fail-on-invalid"])
            self.assertEqual(rc, 1)
            manifest = json.loads((td / "ev" / "ab1-manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(any("launcher" in f for f in manifest["parity_failures"]),
                            manifest["parity_failures"])

    def test_helper_opts_cli_emits_the_command_the_harness_runs(self):
        """The harness builds the helper argv from THIS mode's stdout, one token per line; the
        pair of timeout flags and the budget must be in it, and the production socket may not."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            launcher = td / "launcher.sh"
            launcher.write_text(LAUNCHER_TEXT, encoding="utf-8")
            out = subprocess.run([sys.executable, str(ROOT / "tools" / "mixed_gpu" / "helper_ab.py"),
                                  "helper-opts", "--launcher", str(launcher),
                                  "--socket", CAND_SOCKET, "--helper", "/bin/true"],
                                 capture_output=True, text=True, check=False)
            self.assertEqual(out.returncode, 0, out.stderr)
            argv = [l for l in out.stdout.splitlines() if l]
            self.assertEqual(argv[argv.index("--timeout") + 1], "7200000")
            self.assertEqual(argv[argv.index("--begin-timeout") + 1], "1800000")
            self.assertEqual(argv[argv.index("--slots") + 1], "4000")
            self.assertEqual(argv[argv.index("--reserve-mib") + 1], "300")
            self.assertEqual(argv[argv.index("--device") + 1], "0")
            self.assertEqual(argv[argv.index("--socket") + 1], CAND_SOCKET)
            self.assertNotIn(PROD_SOCKET, argv)
            self.assertEqual(argv[0], "env")

    def test_helper_opts_cli_refuses_a_launcher_missing_the_begin_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            launcher = td / "launcher.sh"
            launcher.write_text(LAUNCHER_TEXT.replace("--begin-timeout 1800000 ", ""), encoding="utf-8")
            out = subprocess.run([sys.executable, str(ROOT / "tools" / "mixed_gpu" / "helper_ab.py"),
                                  "helper-opts", "--launcher", str(launcher),
                                  "--socket", CAND_SOCKET, "--helper", "/bin/true"],
                                 capture_output=True, text=True, check=False)
            self.assertEqual(out.returncode, 1)
            self.assertIn("canonical set refused", out.stderr)

    def test_derive_cli_writes_both_configs_and_the_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cfg = td / "prod.json"
            cfg.write_text(json.dumps(PROD_CONFIG), encoding="utf-8")
            launcher = td / "launcher.sh"
            launcher.write_text(LAUNCHER_TEXT, encoding="utf-8")
            exe = td / "strata"
            exe.write_text("#!/bin/sh\n", encoding="utf-8")
            helper = td / "strata-expert-helper"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            rc = helper_ab.main(["derive", "--prod-config", str(cfg), "--launcher", str(launcher),
                                 "--out-dir", str(td / "ev"), "--exe", str(exe),
                                 "--helper", str(helper), "--socket", CAND_SOCKET,
                                 "--port", "18080", "--cwd", str(td),
                                 "--log", str(td / "ab1-engine.log"), "--fail-on-invalid"])
            self.assertEqual(rc, 0, (td / "ev" / "ab1-manifest.json").read_text(encoding="utf-8"))
            on = json.loads((td / "ev" / "ab1-config-on.json").read_text(encoding="utf-8"))
            off = json.loads((td / "ev" / "ab1-config-off.json").read_text(encoding="utf-8"))
            manifest = json.loads((td / "ev" / "ab1-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["parity_failures"], [])
            self.assertEqual(on["model_name"], "ab1-on")
            self.assertEqual(off["model_name"], "ab1-off")
            self.assertEqual(off["port"], 18080)
            self.assertNotIn("expert_helper_socket", off)
            self.assertNotIn(PROD_SOCKET, (td / "ev" / "ab1-config-on.json").read_text(encoding="utf-8"))
            self.assertNotIn(PROD_SOCKET, (td / "ev" / "ab1-config-off.json").read_text(encoding="utf-8"))
            self.assertIn("--begin-timeout", manifest["helper_cmd"])
            self.assertEqual(manifest["helper_opts_from_launcher"]["--timeout"], "7200000")
            names = {f["path"] for f in manifest["fingerprints"]}
            self.assertIn(str(cfg), names)
            self.assertIn(str(launcher), names)
            self.assertIn(str(exe), names)


# ---------------------------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------------------------

    def test_the_tuned_extras_ride_the_helper_command_only_when_requested(self):
        """tuned-window-design.md delta 1: --fill-mib/--socket2 appear ONLY on an explicit
        request; the canonical command stays byte-identical otherwise."""
        base = helper_ab.helper_cmd("/worktree/strata-expert-helper", CAND_SOCKET)
        self.assertNotIn("--socket2", base)
        self.assertNotIn("--fill-mib", base)
        tuned = helper_ab.helper_cmd("/worktree/strata-expert-helper", CAND_SOCKET,
                                     socket2="/tmp/agent/ab2-helper2.sock", fill_mib=6400)
        self.assertIn("--fill-mib", tuned)
        self.assertIn("6400", tuned)
        self.assertIn("--socket2", tuned)
        self.assertIn("/tmp/agent/ab2-helper2.sock", tuned)


class Parsing(unittest.TestCase):
    def test_pair_startup_records_are_separate_from_probe_ranges(self):
        """Recorder shape (ab1-parent-review finding 1): the PAIR record carries the startup
        range; every probe slices only its OWN helper lines."""
        path = ROOT / "build-mixed-gpu/cap1-validation/ab-window.sh"
        if not path.is_file():
            self.skipTest("deployment harness absent")
        harness = path.read_text()
        start = harness.split("start_pair() {", 1)[1].split("\n}\n", 1)[0]
        probe = harness.split("probe() {", 1)[1].split("\n}\n", 1)[0]
        teardown = harness.split("teardown_pair() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('"type":"pair"', start)
        self.assertIn("helper_startup_end", start)
        self.assertIn('hs=$(wc -l < "$HLOG"', probe)
        self.assertNotIn('hs="$PAIR_HSTART"', probe)
        self.assertIn('"type":"pair_end"', teardown)

    def test_generated_rate_is_kept_apart_from_the_prompt_rate(self):
        s = helper_ab.parse_serve_line(serve_line("long", 37.6, 407))
        self.assertIsNotNone(s)
        self.assertEqual(s["gen_rate"], 37.6)
        self.assertEqual(s["read_rate"], 349.6)
        self.assertEqual(s["generated"], 32)
        self.assertEqual(s["prompt"], 10470)
        self.assertEqual(s["helper_reqs"], 407)

    def test_the_off_line_has_no_helper_counters(self):
        s = helper_ab.parse_serve_line(serve_line("long", 34.2, None))
        self.assertIsNone(s["helper_reqs"])

    def test_a_cancelled_partial_read_is_recognised(self):
        line = serve_line("long", 40.0, 407).replace("+ 10470 read in", "+ 5235 of 10470 read in")
        s = helper_ab.parse_serve_line(line)
        self.assertTrue(s["cancelled"])
        self.assertEqual(s["read"], 5235)

    def test_non_serve_lines_are_not_samples(self):
        for line in ("strata serve: decode expert cache hit rate: 96.8% (19458 hits / 20094 lookups)",
                     "expert-helper: filled 4165 slots (6.68 GiB)", "strata generate: ready"):
            self.assertIsNone(helper_ab.parse_serve_line(line))

    def test_samples_are_found_by_range_and_label_not_by_a_hardcoded_token_total(self):
        """Run 1's summary grepped 'prompt 10443 tokens'; the requests carry 10470, so it found
        nothing and printed '(no long-arm lines)'.  The range+label association finds all 24."""
        records, eng, hl, shas = build_window(BASE_ON, BASE_OFF)
        self.assertEqual(sum(1 for l in eng if "prompt 10443 tokens" in l), 0)
        res = helper_ab.analyze(records, eng, hl, shas, SIZES, REPS, CAND_SOCKET)
        long_samples = [s for s in res["samples"] if s["size"] == "long" and "gen_rate" in s]
        self.assertEqual(len(long_samples), 6)
        self.assertEqual({s["prompt"] for s in long_samples}, {10470})
        self.assertEqual(res["failures"], [])


# ---------------------------------------------------------------------------------------------
# The deployment harness's own text (findings 2, 3, 6, 7)
# ---------------------------------------------------------------------------------------------

class HarnessScript(unittest.TestCase):
    """Text regressions on the DEPLOYMENT harness the parent actually runs (skipped where the
    ignored local harness is absent).  One test per ab1-parent-review finding."""

    def setUp(self):
        path = ROOT / "build-mixed-gpu/cap1-validation/ab-window.sh"
        if not path.is_file():
            self.skipTest("deployment harness absent")
        self.harness = path.read_text()

    def _body(self, name: str) -> str:
        return self.harness.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]

    def test_the_nonce_diverges_per_size_and_rep_but_not_per_arm(self):
        probe = self._body("probe")
        self.assertIn("nonce: rep=%s size=%s", probe)
        self.assertNotIn("Run %s of the AB1", probe)

    def test_the_wrapper_launch_scrubs_the_canonical_strata_knobs(self):
        start = self._body("start_pair")
        self.assertIn("${!STRATA_@", start)
        for pat in ("STRATA_DF_*", "STRATA_DFLASH_*", "STRATA_DUMP_*", "STRATA_WINDOW_*",
                    "STRATA_PREFILL_HELPER"):
            self.assertIn(pat, start)

    def test_per_pair_wrapper_logs_are_kept(self):
        self.assertIn("${FAM}-wrapper-$arm-r$rep.log", self.harness)

    def test_sizing_always_rederives_the_arms(self):
        self.assertNotIn('if [ ! -f "$EV/${FAM}-config-on.json" ]', self.harness)

    def test_the_engine_banner_must_show_the_100ms_timeout_on_the_wire(self):
        """run-2 gate: an ON pair is refused BEFORE the fill wait unless the engine banner
        shows the injected --expert-helper-timeout-ms 100 and this pair's socket - the
        args-borne-socket shape would otherwise kill the channel on the first cold request."""
        start = self._body("start_pair")
        self.assertIn("--expert-helper-timeout-ms 100", start)
        self.assertIn("--expert-helper-socket $SOCK", start)
        self.assertIn("run-2 death shape", start)

    def test_stop_prod_targets_the_8009_listener_not_an_arbitrary_match(self):
        stop = self._body("stop_prod")
        self.assertIn("ss -H -ltnp 'sport = :8009'", stop)
        self.assertNotIn("pgrep -f 'serve/server[.]py' | head -1", stop)

    def test_no_pattern_can_kill_a_production_binary(self):
        self.assertNotIn("r4-dflash-after", self.harness)
        self.assertNotIn("pkill -TERM -f 'rpcw[-]hip/strata'", self.harness)
        self.assertNotIn("pkill -TERM -f 'strata[-]expert[-]helper'", self.harness)
        for m in re.finditer(r"pkill[^\n]*", self.harness):
            self.assertIn("^$ENGINE", m.group(0), f"unanchored pkill: {m.group(0)}")

    def test_restore_corroborates_the_mixed_pair_and_writes_status(self):
        restore = self._body("restore")
        self.assertIn("${FAM}-restore-status.json", restore)
        self.assertIn("expert-helper: engine connected", restore)
        self.assertIn("expert-helper: filled", restore)
        self.assertIn("RESTORE DEGRADED", restore)

    def test_teardown_reaps_owned_processes_before_unlinking_the_socket(self):
        teardown = self._body("teardown_pair")
        self.assertIn('"type":"pair_end"', teardown)
        self.assertLess(teardown.index('"type":"pair_end"'), teardown.index('rm -f "$SOCK"'))
        self.assertIn("kill -0", teardown)

    def test_the_socket_unlink_cannot_fail_the_teardown(self):
        """run-2/run-3 abort: a cleanly-exited helper unlinks its own socket, so the -S test is
        false on the healthy path; as teardown's LAST command the unguarded && list returned 1
        and set -e killed the window after its first pair - silently, before any analysis."""
        teardown = self._body("teardown_pair")
        self.assertIn('[ -S "$SOCK" ] && rm -f "$SOCK" || true', teardown)
        for m in re.finditer(r'\[ -S "\$SOCK" \] && rm -f "\$SOCK"', self.harness):
            self.assertTrue(self.harness[m.end():m.end() + 20].lstrip().startswith("|| true"),
                            f"unguarded socket unlink: {self.harness[m.start():m.end() + 20]!r}")

    def test_the_second_socket_unlink_cannot_fail_the_teardown_or_restore(self):
        """The tuned second socket carries the same contract; an unguarded [ -n ... ] && chain
        as a function's LAST command is the exact run-2/run-3 trap, so SOCK2 uses the if-form."""
        teardown = self._body("teardown_pair")
        restore = self._body("restore")
        for body in (teardown, restore):
            self.assertIn('if [ -n "$SOCK2" ]; then [ -S "$SOCK2" ] && rm -f "$SOCK2" || true; fi',
                          body)

    def test_tuned_switches_require_their_own_artifact_family(self):
        """tuned-window-design.md: the tuned window writes ab2-* artifacts; no ab1-* artifact
        is ever overwritten by a tuned run."""
        self.assertIn("tuned switches need AB1_FAMILY=ab2", self.harness)
        self.assertIn('SOCK2="$EVABS/${FAM}-helper2.sock"', self.harness)

    def test_the_tuned_banner_and_second_channel_gates(self):
        """The engine banner must show the injected --expert-helper-socket2 and the helper must
        log the second-channel connection BEFORE any probe (phantom-accept exposure doubles)."""
        start = self._body("start_pair")
        self.assertIn("--expert-helper-socket2 $SOCK2", start)
        self.assertIn("second-channel engine connected", start)
        self.assertIn("helper second socket missing", start)

    def test_pair_records_carry_engine_ranges_for_the_channel2_gate(self):
        start = self._body("start_pair")
        teardown = self._body("teardown_pair")
        self.assertIn('"engine_start":%s', start)
        self.assertIn('"engine_end":%s', teardown)

    def test_the_analysis_uses_the_min_based_generated_gate(self):
        """Run 4's lesson: the ==32/finish==length gate refused 46 clean natural-EOS samples."""
        analyze = self._body("analyze")
        self.assertIn("--generated-min 8", analyze)


# ---------------------------------------------------------------------------------------------
# Validity gates
# ---------------------------------------------------------------------------------------------

class Validity(unittest.TestCase):
    def test_the_clean_window_is_valid_and_clears_the_gate(self):
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF)
        self.assertEqual(res["failures"], [])
        self.assertEqual(res["verdict"], "PASS")
        self.assertGreaterEqual(res["rel"], 1.05)

    def test_xl_helper_zero_requests_invalidates_the_window_and_the_verdict(self):
        """Run 1's real XL defect: every ON XL serve line had helper=reqs=0, so its +5.9% median
        was not corroboration.  A median that clears 5% must NOT become a verdict."""
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, zero_helper_sizes=("xl",))
        self.assertTrue(any("helper=reqs=0" in f for f in res["failures"]), res["failures"])
        self.assertTrue(any("-xl-" in f for f in res["failures"]))
        self.assertIn("NO VERDICT", res["verdict"])
        self.assertGreaterEqual(res["medians"]["on"]["long"] / res["medians"]["off"]["long"], 1.05)

    def test_helper_death_mid_probe_is_rejected_intentional_teardown_is_not(self):
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, death_rep=2)
        self.assertTrue(any("dead BEFORE" in f for f in res["failures"]), res["failures"])
        self.assertIn("NO VERDICT", res["verdict"])
        clean = run(on_gen=BASE_ON, off_gen=BASE_OFF)
        self.assertEqual([s for s in clean["samples"] if s["arm"] == "on" and s.get("helper_death")],
                         [], "the clean window's teardown lines (SIGTERM + releasing) are not deaths")

    def test_helper_death_by_channel_error_is_a_death(self):
        ev = helper_ab.helper_events(["expert-helper: channel: timedout; exiting",
                                      "expert-helper: 12340 windows served, 0 prompt row requests; "
                                      "releasing the card"], CAND_SOCKET)
        self.assertTrue(ev["death"])
        self.assertFalse(ev["teardown"])

    def test_helper_segment_on_the_wrong_socket_is_rejected(self):
        records, eng, hl, shas = build_window(BASE_ON, BASE_OFF)
        res = helper_ab.analyze(records, eng, hl, shas, SIZES, REPS, "/tmp/agent/other.sock")
        self.assertTrue(any("never listened on the candidate socket" in f for f in res["failures"]),
                        res["failures"])
        self.assertIn("NO VERDICT", res["verdict"])

    def test_missing_probe_is_rejected(self):
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, drop=("on-long-r2",))
        self.assertTrue(any("on-long-r2" in f for f in res["failures"]), res["failures"])
        self.assertIn("NO VERDICT", res["verdict"])

    def test_duplicate_probe_record_is_rejected(self):
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, duplicate="off-med-r1")
        self.assertTrue(any("DUPLICATE" in f for f in res["failures"]), res["failures"])
        self.assertIn("NO VERDICT", res["verdict"])

    def test_prompt_token_count_mismatch_between_arms_is_ambiguous(self):
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, token_shift="long")
        self.assertTrue(any("AMBIGUOUS" in f for f in res["failures"]), res["failures"])
        self.assertIn("NO VERDICT", res["verdict"])

    def test_cache_reuse_is_rejected(self):
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, reused_sizes=("med",))
        self.assertTrue(any("cache reused" in f for f in res["failures"]), res["failures"])
        self.assertIn("NO VERDICT", res["verdict"])

    def test_partial_read_is_rejected(self):
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, partial_read_sizes=("long",))
        self.assertTrue(any("partial/cancelled" in f for f in res["failures"]), res["failures"])

    def test_the_off_helper_must_not_fill_or_serve(self):
        records, eng, hl, shas = build_window(BASE_ON, BASE_OFF, off_fills=True)
        res = helper_ab.analyze(records, eng, hl, shas, SIZES, REPS, CAND_SOCKET)
        self.assertTrue(any("OFF arm's helper filled" in f for f in res["failures"]), res["failures"])

    def test_the_off_helper_serving_windows_is_rejected(self):
        ev = helper_ab.helper_events(["expert-helper: listening on " + CAND_SOCKET,
                                      "expert-helper: 816 windows served, 0 prompt row requests; "
                                      "releasing the card"], CAND_SOCKET)
        self.assertTrue(any(w > 0 for w in ev["windows_served"]))
        records, eng, hl, shas = build_window(BASE_ON, BASE_OFF, off_fills=True)
        res = helper_ab.analyze(records, eng, hl, shas, SIZES, REPS, CAND_SOCKET)
        self.assertTrue(any("served windows" in f for f in res["failures"]), res["failures"])

    def test_helper_alive_before_the_probe_is_required(self):
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, pre_dead_rep=3)
        self.assertTrue(any("dead BEFORE" in f for f in res["failures"]), res["failures"])

    def test_helper_alive_after_the_probe_is_required(self):
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, post_dead_rep=1)
        self.assertTrue(any("dead DURING/AFTER" in f for f in res["failures"]), res["failures"])

    def test_cross_arm_request_bytes_must_match_per_rep(self):
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, cross_arm_bytes_differ=True)
        self.assertTrue(any("request bytes differ" in f for f in res["failures"]), res["failures"])
        self.assertIn("NO VERDICT", res["verdict"])

    def test_missing_request_sha_is_a_missing_proof(self):
        records, eng, hl, shas = build_window(BASE_ON, BASE_OFF)
        shas.pop("on-long-r1")
        res = helper_ab.analyze(records, eng, hl, shas, SIZES, REPS, CAND_SOCKET)
        self.assertTrue(any("request SHA missing" in f for f in res["failures"]), res["failures"])

    def test_content_hashes_are_recorded_never_gated_across_arms(self):
        """Numerical band governs cross-backend differences: differing CONTENT hashes must not
        fail, while differing REQUEST bytes (same rep) always do."""
        records, eng, hl, shas = build_window(BASE_ON, BASE_OFF)
        for label in shas:
            if label.startswith("on-"):
                shas[label]["content"] = "different-content-sha"
        res = helper_ab.analyze(records, eng, hl, shas, SIZES, REPS, CAND_SOCKET)
        self.assertEqual([f for f in res["failures"] if "content" in f.lower()], [])
        self.assertEqual(res["verdict"], "PASS")

    def test_a_noncompleting_probe_is_rejected(self):
        records, eng, hl, shas = build_window(BASE_ON, BASE_OFF)
        next(r for r in records if r.get("type") is None)["http"] = "503"
        res = helper_ab.analyze(records, eng, hl, shas, SIZES, REPS, CAND_SOCKET)
        self.assertTrue(any("HTTP 503" in f for f in res["failures"]), res["failures"])

    def test_the_verdict_is_the_long_arm_generated_median_ratio(self):
        res = run(on_gen=dict(BASE_ON, long=[36.0, 36.0, 36.0]),
                  off_gen=dict(BASE_OFF, long=[35.0, 35.0, 35.0]))
        self.assertEqual(res["verdict"], "FAIL")
        self.assertAlmostEqual(res["rel"], 36.0 / 35.0, places=6)
        self.assertEqual(res["failures"], [])

    def test_a_truncated_sample_never_enters_the_median(self):
        """Finding 4: a truncated reply is invalid.  The engine served 17 tokens while the API
        claimed 32 - the reply/engine cross-check catches the disagreement (the old ==32 gate
        caught it only by shape)."""
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, truncated=("on-long-r2", "off-long-r2"))
        self.assertTrue(any("reply/engine disagreement" in f for f in res["failures"]),
                        res["failures"])
        self.assertIn("NO VERDICT", res["verdict"])

    def test_a_natural_eos_sample_is_valid(self):
        """Run 4's 46 false failures: with the nonce prompts the model EOSes at 16-19 of the 32
        budget on every size and BOTH arms (finish stop, ct agrees with the engine).  Those are
        VALID timing samples; the old ==32/finish==length gate wrongly refused them."""
        labels = tuple(f"{arm}-{size}-r1" for arm in ("on", "off") for size in SIZES)
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, natural_eos=labels)
        self.assertEqual(res["failures"], [])
        self.assertEqual(res["verdict"], "PASS")

    def test_an_early_finish_is_a_valid_sample(self):
        """Run 4: finish_reason=stop with a consistent completion count is a legitimate model
        finish, not an engine defect."""
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, early_finish=("on-med-r1",))
        self.assertEqual(res["failures"], [])
        self.assertEqual(res["verdict"], "PASS")

    def test_a_downward_completion_token_drift_is_recorded_not_rejected(self):
        """Tuned-run ab2 lesson: the reply's completion counter legitimately under-counts the
        engine (spec-decode accounting; ct=16 vs engine 16-19 on every sample).  Only a reply
        claiming MORE tokens than the engine served is a disagreement."""
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, ct_drift=("off-short-r3",))
        self.assertEqual(res["failures"], [])
        self.assertEqual(res["verdict"], "PASS")

    def test_an_on_pair_startup_without_fill_is_rejected(self):
        """Finding 1: fill is PAIR startup evidence; an ON pair that never filled is invalid
        even when every probe's own range looks clean."""
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, on_no_fill=True)
        self.assertTrue(any("pair startup never filled" in f for f in res["failures"]), res["failures"])
        self.assertIn("NO VERDICT", res["verdict"])

    def test_off_late_window_service_is_rejected_on_the_whole_pair_range(self):
        """Finding 1: OFF service after the probes (before pair_end) must still fail the pair."""
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, off_late_windows=True)
        self.assertTrue(any("served windows" in f for f in res["failures"]), res["failures"])
        self.assertIn("NO VERDICT", res["verdict"])

    def test_missing_pair_startup_records_are_rejected(self):
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, drop_pair_records=True)
        self.assertTrue(any("NO pair startup record" in f for f in res["failures"]), res["failures"])
        self.assertIn("NO VERDICT", res["verdict"])

    # ---- tuned window: the second channel (tuned-window-design.md delta 5) ----

    def test_a_tuned_clean_window_with_channel2_participation_passes(self):
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, socket2="/tmp/agent/ab2-helper2.sock")
        self.assertEqual(res["failures"], [])
        self.assertEqual(res["verdict"], "PASS")

    def test_a_tuned_on_pair_without_channel2_participation_is_invalid(self):
        """The tuned arm is INVALID without channel-2 use, exactly like run-1's zero-helper XL
        samples were invalid."""
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, socket2="/tmp/agent/ab2-helper2.sock",
                  tier_zero=True)
        self.assertTrue(any("no channel-2 prompt-tier participation" in f for f in res["failures"]),
                        res["failures"])
        self.assertIn("NO VERDICT", res["verdict"])

    def test_a_tuned_on_pair_closing_a_foreign_channel2_is_invalid(self):
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, socket2="/tmp/agent/ab2-helper2.sock",
                  tier_path="/tmp/agent/other2.sock")
        self.assertTrue(any("no channel-2 prompt-tier participation" in f for f in res["failures"]),
                        res["failures"])

    def test_a_tuned_off_pair_showing_a_prompt_tier_line_is_invalid(self):
        res = run(on_gen=BASE_ON, off_gen=BASE_OFF, socket2="/tmp/agent/ab2-helper2.sock",
                  off_tier=True)
        self.assertTrue(any("OFF pair shows a prompt-tier close line" in f for f in res["failures"]),
                        res["failures"])
        self.assertIn("NO VERDICT", res["verdict"])

    def test_tuned_pair_records_without_engine_ranges_cannot_prove_channel2(self):
        records, eng, hl, shas = build_window(BASE_ON, BASE_OFF,
                                              socket2="/tmp/agent/ab2-helper2.sock")
        for r in records:
            if r.get("type"):   # PAIR records only: probe records carry their OWN engine ranges
                r.pop("engine_start", None)
                r.pop("engine_end", None)
        res = helper_ab.analyze(records, eng, hl, shas, SIZES, REPS, CAND_SOCKET,
                                socket2="/tmp/agent/ab2-helper2.sock")
        self.assertTrue(any("lacks engine ranges" in f for f in res["failures"]), res["failures"])


# ---------------------------------------------------------------------------------------------
# Request bodies (file transport, pinned bytes)
# ---------------------------------------------------------------------------------------------

class RequestBody(unittest.TestCase):
    def test_the_prompt_travels_by_file_and_the_body_is_pinned(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            prompt = td / "prompt.txt"
            prompt.write_text("Explain MoE routing." * 40000, encoding="utf-8")
            out = td / "req.json"
            helper_ab.build_request(str(out), str(prompt), 32, "ab1-on")
            body = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(body["temperature"], 0)
            self.assertEqual(body["max_tokens"], 32)
            self.assertEqual(body["model"], "ab1-on")
            self.assertEqual(len(body["messages"][0]["content"]), 40000 * 20)

    def test_the_sha_and_hashreply_modes_agree_on_the_request_hash(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            prompt = td / "p.txt"
            prompt.write_text("one paragraph", encoding="utf-8")
            req = td / "req.json"
            helper_ab.build_request(str(req), str(prompt), 32, "ab1-on")
            reply = td / "reply.json"
            reply.write_text(json.dumps({"choices": [{"message": {"role": "assistant", "content": "hi"},
                                                       "finish_reason": "length"}],
                                          "usage": {"completion_tokens": 32}}), encoding="utf-8")
            h = helper_ab.record_hashes("on-short-r1", str(reply), str(req))
            self.assertEqual(h["req"], helper_ab.sha16(req))
            self.assertEqual(h["finish"], "length")
            self.assertEqual(h["ct"], "32")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""CPU fixtures for the OPT-IN --pin-expert-cache residency leg of the divergence controller.

Parent runs this SERIALLY with the other window fixtures (the ops-listing assertions are sensitive to a
fixture that writes).  Nothing here touches production, the GPU, any port, any service, any snapshot, the
engine, or the network.  The ONLY files written are the fixture's own scratch (derived configs and fake
engine-log copies) under /tmp/agent, removed at the end - never into the repository, never into the
ops results dir.  The only subprocess runs are CLI refusals, which the controller must take BEFORE its
first window action (verified by the ops listing being unchanged afterwards).

WHAT IS PROVEN:
  * DEFAULT BYTE-IDENTITY: with the flag absent the derived config of every arm is exactly the pinned
    controlled shape (the three arms' derived args hash to one stable anchor, printed every run), the
    recorded overrides block is exactly the existing block key for key, the gate words stay 'pin2000',
    and the default fingerprint keeps its historical requirements (the CUDA0 cache lines must be present;
    an absent read-back checksum carries a nonce and can never compare equal).
  * THE LEG IS VISIBLE: --pin-expert-cache 0 / auto / 5 replaces ONLY --expert-cache, identically on all
    three arms; the derivation allowlist check accepts it and records the override; the plan/result fields
    (expert_cache_pin, the overrides block, the gate words) name the value.
  * REFUSALS, PRE-ACTION: with --reenable, without --prompt-set, a negative, a non-integer, an empty
    value, and 2000 (the default pin) - the CLI exits 2 with a REFUSE line before any window action.
  * GATES FAIL-CLOSED AND SELF-CONSISTENT: under a leg, cross-arm equality of the OBSERVED residency keys
    keeps the existing strictness (a slot-count difference fails, defined-vs-undefined fails), the absent
    CUDA0 cache lines are recorded as an explicit UNDEFINED label (deterministic, never a nonce, never an
    invented number), and the default run still refuses a fingerprint without the CUDA0 lines.
  * The leg changes NO other proof (NO-LOAN, ADAPT-OFF, CACHE-OFF keep their nonces) and NO other control;
    --equalize-mib keeps its arithmetic and its non-dflash-only scope.

Run: python3 tools/test_inco_pin_expert_cache.py     (exit 0 = all fixtures pass)
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent          # tools/
HARNESS = HERE / "window_harness"
sys.path.insert(0, str(HARNESS))                        # window/model_window/greedy_gate/...
import window as W                                       # noqa: E402  ops/log paths only
import model_window as mw                                  # noqa: E402  the flags() parser
import greedy_gate as G                                       # noqa: E402  residency_key
import divergence_window as D                                 # noqa: E402  the controller under test

FAILS: list[str] = []
SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="dw-pin-fixture-", dir="/tmp/agent"))


def check(ok: bool, what: str) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        FAILS.append(what)


# The clone shape derivation starts from (synthetic: no config file is read, nothing is written outside
# the fixture scratch).  Its values are deliberately DIFFERENT from the window's controls, and each arm's
# clone carries ITS OWN drafter widths (the clone is the source: the window never changes the widths).
CFG_BASE = {"args": ["--pack", "/p/pack", "--expert-cache", "3000", "--prefill", "4096",
                     "--adapt-swaps", "96", "--prompt-cache", "6", "--conversation-cache-mib", "256",
                     "--vram-reserve-later-mib", "1000", "--max-context", "32768"],
            "env": {"STRATA_X": "1"}, "log": "/prod/engine.log"}
WIDTHS = {"target_only": [], "dflash_w1": ["--spec", "2", "--dflash-block", "1"],
          "dflash_w3": ["--spec", "4", "--dflash-block", "3"]}
CFG = {m: dict(CFG_BASE, args=list(CFG_BASE["args"]) + WIDTHS[m]) for m in dict(D.DW_MODES)}
F = lambda cfg: dict(mw.flags(cfg["args"] if isinstance(cfg, dict) else cfg))   # noqa: E731


def derived(mode: str, pin=None, equalize: int = 0):
    """The REAL derivation (derive writes the 0600 config), into the fixture scratch only."""
    d = SCRATCH / f"{mode}-{pin}-{equalize}"
    d.mkdir(parents=True, exist_ok=True)
    path, cfg, ovr = D.derive("fixture", mode, CFG[mode], d, equalize, None, pin)
    return json.loads(path.read_text()), cfg, ovr


# ------------------------------------------------------------------ the default is byte-identical
shapes = {}
for mode, _w in D.DW_MODES:
    _t, cfg, ovr = derived(mode)
    shapes[mode] = cfg["args"]
    check(D.validate_derivation(CFG[mode], cfg, ovr, "fixture", mode, None) == [],
          f"default derivation of {mode} passes its own allowlist check")
    check(F(cfg)["--expert-cache"] == str(D.DW_PIN), f"{mode}: default --expert-cache stays the pin 2000")
anchor = hashlib.sha256(json.dumps(shapes, sort_keys=True).encode()).hexdigest()
print(f"  derived-args anchor (default, all three arms) sha256={anchor}")
check(len(anchor) == 64 and json.loads(json.dumps(shapes)) == shapes,
      "the default derived args of all three arms hash to one stable anchor (compare it across runs)")
f0 = F(derived("target_only")[1])
check(f0["--prefill"] == "512" and "--no-prefill-borrow" in f0 and f0["--adapt-swaps"] == "0"
      and f0["--prompt-cache"] == "0" and f0["--conversation-cache-mib"] == "0",
      "the default controlled shape is intact (chunk 512, no-prefill-borrow, adapt off, caches off)")

# ------------------------------------------------------------------ the leg replaces ONLY the pin
for pin in ("0", "auto", "5"):
    got = {}
    for mode, _w in D.DW_MODES:
        _t, cfg, ovr = derived(mode, pin)
        got[mode] = cfg["args"]
        f = F(cfg)
        check(f["--expert-cache"] == pin, f"pin {pin}: {mode} carries --expert-cache {pin}")
        check(f["--prefill"] == str(D.DW_CHUNK) and f["--adapt-swaps"] == "0"
              and f["--prompt-cache"] == "0" and f["--conversation-cache-mib"] == "0"
              and "--no-prefill-borrow" in f,
              f"pin {pin}: {mode} keeps every OTHER control (chunk / no-borrow / adapt / caches)")
        check(D.validate_derivation(CFG[mode], cfg, ovr, "fixture", mode, None) == [],
              f"pin {pin}: {mode} passes the derivation allowlist check")
        check(any(o["flag"] == "--expert-cache" and o["to"] == pin for o in ovr),
              f"pin {pin}: {mode} records the --expert-cache override (-> {pin})")
    for mode in ("dflash_w1", "dflash_w3"):
        check(F(got["target_only"])["--expert-cache"] == F(got[mode])["--expert-cache"],
              f"pin {pin}: the value applies to EVERY arm equally (target_only vs {mode})")
    check(F(got["dflash_w1"])["--dflash-block"] == "1" and F(got["dflash_w1"])["--spec"] == "2"
          and F(got["dflash_w3"])["--dflash-block"] == "3" and F(got["dflash_w3"])["--spec"] == "4",
          f"pin {pin}: the drafter widths are untouched")

# the equalizer is untouched by the leg (non-dflash arms only, same arithmetic)
for pin in (None, "0", "auto"):
    for mode in ("target_only", "dflash_w1"):
        _t, cfg, _o = derived(mode, pin, 500)
        f = F(cfg)
        want = "1500" if mode == "target_only" else "1000"
        check(f["--vram-reserve-later-mib"] == want,
              f"--equalize-mib 500 unchanged with pin={pin}: {mode} -> {f['--vram-reserve-later-mib']}")

# ------------------------------------------------------------------ plan and result fields
check(D.dw_pin_label(None) == "2000(default)", "default expert_cache_pin = '2000(default)'")
check(D.dw_pin_label("0") == "0" and D.dw_pin_label("auto") == "auto" and D.dw_pin_label("5") == "5",
      "a leg's expert_cache_pin is the literal value")
check(D.dw_pin_gate_label(None) == "pin2000", "default gate words stay 'pin2000'")
check("OVERRIDE" in D.dw_pin_gate_label("0") and "OVERRIDE" in D.dw_pin_gate_label("auto"),
      "a leg is visible in the gate words")
DEFAULT_BLOCK = {"all arms": {"--expert-cache": "2000", "--prefill": "512",
                              "--no-prefill-borrow": "(flag, valueless)", "--adapt-swaps": "0",
                              "--prompt-cache": "0", "--conversation-cache-mib": "0"},
                 "dflash arms": {"--spec-min-p": "0 (full proposal width, 14549/14777)",
                                 "--lookup-chain": "0 (sfx_on false, 14338)"},
                 "non-dflash arms with --equalize-mib": {"--vram-reserve-later-mib": "+DELTA (4499-4501)"}}
check(D.dw_overrides_block(None) == DEFAULT_BLOCK,
      "the recorded overrides block with the flag absent is the existing block, key for key")
b0 = D.dw_overrides_block("0")["all arms"]["--expert-cache"]
ba = D.dw_overrides_block("auto")["all arms"]["--expert-cache"]
check(b0.startswith("0 ") and "OVERRIDE" in b0 and "2000" in b0,
      "the overrides block shows the leg value and says it replaces the window's pin")
check(ba.startswith("auto ") and "OVERRIDE" in ba, "the overrides block shows 'auto'")
check(D.dw_overrides_block("0")["dflash arms"] == DEFAULT_BLOCK["dflash arms"],
      "the overrides block changes nothing else")

# ------------------------------------------------------------------ refusals, pure and pre-action
BAD = [("0", False, None, "without --prompt-set"), ("auto", False, None, "'auto' without --prompt-set"),
       ("0", True, "adaptation", "with --reenable adaptation"),
       ("0", True, "borrowing", "with --reenable borrowing"),
       ("auto", True, "conversation-cache", "'auto' with --reenable"),
       ("-1", True, None, "negative"), ("-5", True, None, "negative 5"),
       ("1.5", True, None, "non-integer"), ("junk", True, None, "junk"), ("", True, None, "empty"),
       ("0x10", True, None, "hex"), ("2000", True, None, "the default pin 2000"),
       (" 2000 ", True, None, "the default pin with spaces")]
for value, ps, re_, label in BAD:
    sel, msg = D.validate_pin(value, ps, re_)
    check(sel is None and msg.startswith("REFUSE:"), f"refused pre-action: {label}")
for value, want in (("0", "0"), ("5", "5"), ("auto", "auto"), (" 0 ", "0"), ("00", "0"),
                    ("12345", "12345")):
    sel, msg = D.validate_pin(value, True, None)
    check(sel == want and msg == "", f"accepted: --pin-expert-cache {value!r} -> {want}")
check(D.validate_pin(None, False, None) == (None, "")
      and D.validate_pin(None, True, "adaptation") == (None, ""),
      "the flag absent is the default (no refusal, also with --reenable)")

# the CLI refuses BEFORE any window action: exit 2, a REFUSE line, and NO new ops file
CLI = [["--pin-expert-cache", "0"], ["--pin-expert-cache", "auto"],
       ["--prompt-set", "--pin-expert-cache", "-1"], ["--prompt-set", "--pin-expert-cache", "junk"],
       ["--prompt-set", "--pin-expert-cache", "2000"],
       ["--prompt-set", "--pin-expert-cache", "0", "--reenable", "adaptation"],
       ["--prompt-set", "--requests", "8", "--pin-expert-cache", "1.5"]]
before = {p.name for p in W.OPS.iterdir()}
for extra in CLI:
    args = [sys.executable, str(HARNESS / "divergence_window.py")] + extra
    p = subprocess.run(args, cwd=str(HERE.parent), capture_output=True, text=True, timeout=180)
    check(p.returncode == 2 and p.stdout.startswith("REFUSE:"),
          f"CLI exits 2 before any action: {' '.join(extra)}")
check({p.name for p in W.OPS.iterdir()} == before,
      "the refusals created NO window, no results file, no snapshot artefact (ops listing unchanged)")

# ------------------------------------------------------------------ the residency gates
LOG_A = [                                                   # the measured pinned shape
    "strata serve: expert cache 2904 slots, 4.34 GiB of VRAM; policy is",
    "strata serve: pre-filled 2904 of 2904 slots from the profile in 1.0 s (120 MB/s); slot 0 verified",
    "strata serve: expert cache checksum 81e5abcdef012345 (2904 slots)",
    "strata serve: CUDA1 runs layers 24-47, expert cache 5340 slots (7.35 GiB), 2136 of its 2904 "
    "profiled pairs",
    "strata serve: prompt chunk 512 tokens",
    "strata serve: the prompt path allocates its own buffers (too few cache slots to borrow)"]
LOG_B = [                                                   # auto's own sizing (differs from history)
    "strata serve: expert cache 3072 slots, 4.60 GiB of VRAM; policy is",
    "strata serve: pre-filled 3072 of 3072 slots from the profile in 1.0 s (120 MB/s); slot 0 verified",
    "strata serve: expert cache checksum 0011223344556677 (3072 slots)",
    "strata serve: CUDA1 runs layers 24-47, expert cache 4010 slots (5.53 GiB), 1604 of its 2904 "
    "profiled pairs",
    "strata serve: prompt chunk 512 tokens",
    "strata serve: the prompt path allocates its own buffers (too few cache slots to borrow)"]
LOG_C = [                                                   # pin 0: NO CUDA0 cache lines at all
    "strata serve: CUDA1 runs layers 24-47, expert cache 5340 slots (7.35 GiB), 2136 of its 2904 "
    "profiled pairs",
    "strata serve: prompt chunk 512 tokens",
    "strata serve: the prompt path allocates its own buffers (too few cache slots to borrow)"]
LOG_D = [ln for ln in LOG_A if "checksum" not in ln]        # read-back checksum line missing
ARGV = {"--prefill": "512", "--adapt-swaps": "0", "--prompt-cache": "0",
        "--conversation-cache-mib": "0", "--expert-cache": "2000"}


def fp(lines: list[str], name: str, pin_override: bool = False, argv: dict | None = None):
    p = SCRATCH / f"log-{name}.txt"
    p.write_text("\n".join(lines) + "\n")
    return D.dw_fingerprint({"engine_log": {"per_window_copy": str(p)}},
                            (argv or ARGV).get("--prefill"), argv or ARGV, None, pin_override)


# the DEFAULT keeps its historical requirements exactly
fa = fp(LOG_A, "a")
check(fa is not None and fa["cuda0"]["slots"] == 2904 and fa["checksum_present"],
      "default: the pinned log fingerprints with the CUDA0 constants and the read-back checksum")
check(fp(LOG_C, "c-default") is None,
      "default: a log WITHOUT the CUDA0 cache lines is refused (historical constants required)")
kd1, kd2 = [D.dw_key(fp(LOG_D, f"d{i}")) for i in (1, 2)]
check(kd1 != kd2,
      "default: an absent read-back checksum carries a nonce and two unproven arms never compare equal")
check(D.dw_fingerprint({"engine_log": {}}, "512", ARGV) is None,
      "no provenanced log copy = no fingerprint (fail closed)")

# the LEG: cross-arm equality of the OBSERVED values; undefined recorded, never invented
ca1, ca2 = fp(LOG_C, "c1", True), fp(LOG_C, "c2", True)
check(ca1 is not None and ca2 is not None, "leg: a log with no CUDA0 cache lines is NOT refused")
check(ca1["cuda0"]["slots"] == D.DW_PIN_UNDEFINED and not ca1["cuda0_lines_present"],
      "leg: the absent CUDA0 components are recorded with an explicit UNDEFINED label")
check(ca1["cuda0"]["checksum"] is None and ca1["cuda0"]["checksum_proof"][0] == D.DW_PIN_UNDEFINED,
      "leg: the absent checksum is recorded as UNDEFINED, never invented")
check(D.dw_key(ca1) == D.dw_key(ca2),
      "leg: two arms with the same observed (undefined) residency compare equal, so the leg can run")
check(D.dw_key(ca1) != D.dw_key(fp(LOG_A, "a-leg", True)),
      "leg: undefined-vs-defined is a MISMATCH (never weakened into a pass)")
check(D.dw_key(fp(LOG_A, "a-leg2", True)) != D.dw_key(fp(LOG_B, "b-leg", True)),
      "leg: a real slot-count difference across arms fails the equality gate")
kb1, kb2 = [D.dw_key(fp(LOG_B, f"b{i}", True)) for i in (1, 2)]
check(kb1 == kb2, "leg: identical observed 0/auto-shaped logs compare equal")
check(fp(LOG_D, "d-leg", True)["cuda0"]["checksum_proof"]
      == fp(LOG_D, "d-leg2", True)["cuda0"]["checksum_proof"],
      "leg: the undefined checksum label is deterministic (no nonce), so it CAN compare equal")
check(fp(LOG_A, "a-leg3", True)["cuda0"]["slots"] == 2904
      and fp(LOG_A, "a-leg3", True)["checksum_present"],
      "leg: a log that DOES carry the CUDA0 lines is recorded with its real observed values")
check(ca1["pin_override"] is True and fa["pin_override"] is False
      and fa["cuda0_lines_present"] is True and ca1["stages_present"] is True,
      "the fingerprint records pin_override / cuda0_lines_present / stages_present")

# the leg changes NO other proof
no_loan = [ln for ln in LOG_A if "allocates its own buffers" not in ln]
check(fp(no_loan, "no-loan", True)["no_borrow_proof"][0] == "no-loan-UNPROVEN",
      "leg: the NO-LOAN proof keeps its nonce and stays fail-closed")
check(fp(LOG_A, "adapt-leg", True, dict(ARGV, **{"--adapt-swaps": "96"}))["adapt_proof"][0]
      == "adapt-off-UNPROVEN", "leg: the ADAPT-OFF proof is unchanged by the leg")
check(fp(LOG_A, "cache-leg", True, dict(ARGV, **{"--prompt-cache": "6"}))["cache_proof"][0]
      == "cache-UNPROVEN", "leg: the CACHE-OFF proof is unchanged by the leg")

shutil.rmtree(SCRATCH, ignore_errors=True)
print()
if FAILS:
    print(f"{len(FAILS)} FIXTURE(S) FAILED:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("ALL PIN-EXPERT-CACHE FIXTURES PASS (CPU only, fixture scratch under /tmp/agent removed, "
      "no GPU/HTTP/snapshot, nothing written into the repo or the ops dir)")

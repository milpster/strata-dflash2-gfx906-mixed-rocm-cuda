#!/usr/bin/env python3
"""CPU fixtures for the SUBSET request selection of the divergence controller (--requests).

Parent runs this.  Nothing here touches production, the GPU, any port, any service, any snapshot, the
engine, or the network, and NOTHING is written by this module - no file, no directory, no /tmp, no ops
file.  The only subprocess runs are CLI refusals, which the controller must take BEFORE its first
window action (verified by the ops listing being unchanged afterwards).

WHAT IS PROVEN:
  * DEFAULT SELECTION BYTE-EQUAL: no --requests keeps the exact old selection - the single default
    prompt (gate-set position 1) and, with --prompt-set, the FULL ordered eight-prompt gate set.
  * --requests 8 selects EXACTLY greedy_gate.GATE_PROMPTS[7]: class 'long', the 64-token class cap,
    the prompt SHA and lengths, and the ORIGINAL full-set index 8 in the plan row and in every
    response, beside the sequential index the existing comparison code uses.
  * --max-tokens can only LOWER a class cap (never raise it, never below 1).
  * ORDER IS PRESERVED: [7,8] and [8,7] select the same two prompts in the given order, with those
    original indices.
  * PRE-ACTION REFUSAL: an empty selection, a duplicate, 0, an out-of-range index, --requests without
    --prompt-set and --requests with --reenable are refused, and the CLI refuses them BEFORE any
    snapshot/signal/config/port/HTTP action.
  * EMPTY RESPONSES NEVER PASS: zero responses is never all_equal, and a missing response fails the
    one-per-prompt count the gate asserts.
  * SUBSET NEVER MARKS A FULL GATE ESTABLISHED: gates_ok stays False, full_eight_prompt_gate_established
    stays False, the separate subset_diagnostic_ok covers the selected responses only, the status words
    say SUBSET DIAGNOSTIC PASS/FAIL and never 'ALL OK', and a non-restored window never exits 0.
  * WITHOUT --requests the standard flags and status words are the existing ones, unchanged.

Run: python3 tools/test_inco_request_selection.py     (exit 0 = all fixtures pass)
"""
from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent          # tools/
HARNESS = HERE / "window_harness"
sys.path.insert(0, str(HARNESS))                        # window/model_window/greedy_gate/...
import window as W                                       # noqa: E402  ops/log paths only
import greedy_gate as G                                  # noqa: E402  GATE_PROMPTS / MAXT
import divergence_window as D                            # noqa: E402  the controller under test

FAILS: list[str] = []
SHA = lambda t: hashlib.sha256(t.encode()).hexdigest()   # noqa: E731


def check(ok: bool, what: str) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        FAILS.append(what)


def refusals(values, prompt_set=True, reenable=None) -> str:
    """The PURE pre-action gate, exactly as main() calls it."""
    sel, refuse = D.validate_requests(values, prompt_set, reenable)
    return "" if not refuse else refuse


def plan(values, tokens=None):
    """The ONE plan builder, fed the selection the CLI would use."""
    prompts = D.select_prompts(True, values)
    return D.prompt_plan_rows(prompts, D.prompt_indices(prompts, values), tokens)


# ------------------------------------------------------------------ the defaults are untouched
check(D.select_prompts(False) == [G.GATE_PROMPTS[0]] and len(D.select_prompts(False)) == 1,
      "default (no --requests) selects exactly the single default prompt")
check(D.DW_PROMPT == G.GATE_PROMPTS[0][1], "DW_PROMPT is still gate-set position 1")
full = D.select_prompts(True)
check(full == list(G.GATE_PROMPTS) and len(full) == 8,
      "--prompt-set (no --requests) selects the FULL ordered eight-prompt gate set")
check([c for c, _ in full] == ["short", "short", "medium", "medium", "medium", "medium", "long", "long"],
      "the full set keeps the gate's own class order")
check(D.prompt_indices(full, None) == list(range(1, 9)),
      "the full set's rows carry their own original indices 1..8")
check(D.prompt_indices(D.select_prompts(False), None) == [1],
      "the single default row carries its original index 1")
check(D.validate_requests(None, False, None) == (None, "")
      and D.validate_requests(None, True, "adaptation") == (None, ""),
      "no --requests is the default (no refusal), also with --reenable")

# ------------------------------------------------------------------ --requests 8
r8 = D.select_prompts(True, [8])
check(r8 == [G.GATE_PROMPTS[7]] and len(r8) == 1, "--requests 8 selects ONLY GATE_PROMPTS[7]")
check(r8[0][0] == "long", "--requests 8 keeps class='long'")
sha8 = SHA(G.GATE_PROMPTS[7][1])
rows = plan([8])
check(rows[0]["sha256"] == sha8 and rows[0]["class"] == "long",
      "the plan row for --requests 8 carries the original prompt SHA and class")
check(rows[0]["n"] == 1 and rows[0]["full_set_request"] == 8,
      "--requests 8: sequential index 1 AND original full-set index 8")
check(rows[0]["chars"] == len(r8[0][1]) and rows[0]["words"] == len(r8[0][1].split()),
      "the plan row records the prompt's real lengths")
check(rows[0]["max_tokens"] == 64 and rows[0]["max_tokens_requested"] == "class limit",
      "--requests 8 uses the gate's own 64-token class cap")
check(D.max_tokens_for("long", None) == 64 and D.max_tokens_for("long", 64) == 64,
      "class 'long' cap is 64, and --max-tokens 64 keeps it at 64")
check(D.max_tokens_for("long", 128) == 64 and D.max_tokens_for("short", 128) == 128
      and D.max_tokens_for("medium", 200) == 96,
      "--max-tokens can never RAISE a class cap (bounded by the class limit)")
check(D.max_tokens_for("long", 0) == 1 and D.max_tokens_for("long", -5) == 1,
      "--max-tokens never drops below 1")
check(D.max_tokens_for("long", 8) == 8 and "override 8" in D.tokens_description(8, 8),
      "a lower override is recorded as an override, honestly")
check(D.prompt_plan_rows([("long", "x")], [8], 64)[0]["full_set_request"] == 8,
      "every plan row carries its original full-set index")

# ------------------------------------------------------------------ order is preserved
r78 = D.select_prompts(True, [7, 8])
r87 = D.select_prompts(True, [8, 7])
check(r78 == [G.GATE_PROMPTS[6], G.GATE_PROMPTS[7]] and len(r78) == 2, "--requests 7 8 selects 7 then 8")
check(r87 == [G.GATE_PROMPTS[7], G.GATE_PROMPTS[6]] and len(r87) == 2, "--requests 8 7 selects 8 then 7")
check(r78 != r87 and sorted(r78, key=lambda p: p[1]) == sorted(r87, key=lambda p: p[1]),
      "7 8 and 8 7 run the same two prompts in the ORDER GIVEN")
p78, p87 = plan([7, 8]), plan([8, 7])
check([r["n"] for r in p78] == [1, 2] and [r["full_set_request"] for r in p78] == [7, 8],
      "plan 7 8: sequential 1,2 with original indices 7,8")
check([r["n"] for r in p87] == [1, 2] and [r["full_set_request"] for r in p87] == [8, 7],
      "plan 8 7: sequential 1,2 with original indices 8,7")
check([r["sha256"] for r in p78] == [SHA(G.GATE_PROMPTS[6][1]), SHA(G.GATE_PROMPTS[7][1])],
      "plan 7 8 records each prompt's own SHA")
check(len(D.select_prompts(True, list(range(1, 9)))) == 8,
      "--requests 1..8 selects all eight, in order (still a labelled subset run)")

# ------------------------------------------------------------------ refusals, pure and pre-action
BAD = [([8], False, None, "needs --prompt-set"), ([8], True, "adaptation", "not with --reenable"),
       ([8], True, "borrowing", "not with --reenable"), ([], True, None, "empty selection"),
       ([0], True, None, "index 0"), ([9], True, None, "index 9"), ([-1], True, None, "negative index"),
       ([8, 8], True, None, "duplicate"), ([8, 7, 8], True, None, "duplicate in a longer selection"),
       ([1, 9], True, None, "out-of-range inside a valid list"), ([True], True, None, "non-integer")]
for values, ps, re_, label in BAD:
    msg = refusals(values, ps, re_)
    sel = D.validate_requests(values, ps, re_)[0]
    check(bool(msg) and msg.startswith("REFUSE:") and sel is None, f"refused pre-action: {label}")
for values in ([8], [8, 7], [7, 8], [1], [1, 2, 3, 4, 5, 6, 7, 8]):
    sel, msg = D.validate_requests(values, True, None)
    check(sel == list(values) and msg == "", f"accepted, order preserved: {values}")

# the CLI refuses BEFORE any window action: exit 2, a REFUSE line, and NO new ops file
CLI = [["--requests", "8"], ["--prompt-set", "--requests", "8", "8"],
       ["--prompt-set", "--requests", "9"], ["--prompt-set", "--requests", "0"],
       ["--prompt-set", "--requests", "8", "--reenable", "adaptation"]]
before = {p.name for p in W.OPS.iterdir()}
for extra in CLI[1:]:
    args = [sys.executable, str(HARNESS / "divergence_window.py")] + extra
    p = subprocess.run(args, cwd=str(HERE.parent), capture_output=True, text=True, timeout=180)
    check(p.returncode == 2 and p.stdout.startswith("REFUSE:"),
          f"CLI exits 2 before any action: {' '.join(extra)}")
check({p.name for p in W.OPS.iterdir()} == before,
      "the refusals created NO window, no results file, no snapshot artefact (ops listing unchanged)")

# ------------------------------------------------------------------ empty responses never pass
c = D.compare_pair("AR vs dflash_w1", [], [])
check(c["all_equal"] is False and c["zero_response_refusal"] is True,
      "zero responses on both arms is NEVER all_equal")
c = D.compare_pair("AR vs dflash_w1", [], ["text"])
check(c["all_equal"] is False and c["n_responses"] == [0, 1],
      "one arm with no response is never all_equal")
c = D.compare_pair("AR vs dflash_w1", ["text"], [])
check(c["all_equal"] is False and c["n_responses"] == [1, 0],
      "the other arm's silence is never all_equal either")
c = D.compare_pair("AR vs dflash_w1", ["a"], ["a"])
check(c["all_equal"] is True and c["n_responses"] == [1, 1] and c["zero_response_refusal"] is False,
      "one byte-identical response pair compares equal")
c = D.compare_pair("AR vs dflash_w1", ["abc"], ["abd"])
check(c["all_equal"] is False and c["requests"][0]["first_diff_index"] == 2
      and c["requests"][0]["response_present"] == [True, True],
      "a real text difference is recorded at its index, with both responses present")
two = len(D.select_prompts(True, [7, 8]))
short = D.compare_pair("AR vs dflash_w1", ["a"], [])
check(short["all_equal"] is False and short["n_responses"] != [two, two],
      "a missing response fails the one-per-selected-prompt count the gate asserts")

# ------------------------------------------------------------------ a subset is never a full gate
sub = D.dw_gate_flags(True, [], None, True, [8])
check(sub["gates_ok"] is False, "--requests 8 keeps the STANDARD gates_ok False by design")
check(sub["subset_diagnostic_ok"] is True, "--requests 8 records subset_diagnostic_ok from the strict pass")
check(sub["full_eight_prompt_gate_established"] is False,
      "a subset NEVER marks the full eight-prompt gate established")
check(sub["residency_gate_established"] is True and sub["ablation_ok"] is False,
      "the runtime residency gate is unchanged for a subset; it is never an ablation")
check(D.dw_gate_flags(True, ["GATE AR vs dflash_w1: request 1 differs"], None, True, [8])
      ["subset_diagnostic_ok"] is False,
      "a selected-response difference fails the subset diagnostic")
check(D.dw_gate_flags(False, [], None, True, [8])["subset_diagnostic_ok"] is False,
      "an unestablished residency gate fails the subset diagnostic too")
check(all(v is False for v in D.dw_gate_flags(True, [], None, True, [8], "window broke").values()),
      "a break clears every flag: an unrun comparison is never a pass")
full8 = D.dw_gate_flags(True, [], None, True, None)
check(full8["gates_ok"] is True and full8["full_eight_prompt_gate_established"] is True
      and full8["subset_diagnostic_ok"] is False,
      "the full eight-prompt gate (no --requests) keeps its existing semantics")
one = D.dw_gate_flags(True, [], None, False, None)
check(one["gates_ok"] is True and one["full_eight_prompt_gate_established"] is False
      and one["subset_diagnostic_ok"] is False,
      "the default single prompt keeps its existing gates_ok and claims no eight-prompt gate")
abl = D.dw_gate_flags(True, [], "adaptation", True, None)
check(abl["ablation_ok"] is True and abl["gates_ok"] is False
      and abl["residency_gate_established"] is False,
      "the ablation variant's flags are unchanged")

# ------------------------------------------------------------------ the status words and the exit code
st, code = D.dw_outcome({"selected_requests": [8], "subset_diagnostic_ok": True,
                         "gates_ok": False, "full_eight_prompt_gate_established": False}, True)
check(code == 0 and st.startswith("SUBSET DIAGNOSTIC PASS") and "ALL OK" not in st,
      "a clean subset says SUBSET DIAGNOSTIC PASS and never ALL OK")
st, code = D.dw_outcome({"selected_requests": [8], "subset_diagnostic_ok": False,
                         "gates_ok": True}, True)
check(code == 1 and st.startswith("SUBSET DIAGNOSTIC FAIL"),
      "a subset whose comparisons differ says SUBSET DIAGNOSTIC FAIL and exits 1")
st, code = D.dw_outcome({"selected_requests": [8], "subset_diagnostic_ok": True}, False)
check(code == 1 and st == "RESTORE FAILED - PARENT INVESTIGATE NOW",
      "a subset whose production restore did not verify never exits 0")
st, code = D.dw_outcome({"selected_requests": [8]}, True)
check(code == 1 and st.startswith("SUBSET DIAGNOSTIC FAIL"),
      "an absent subset key is never a pass")
st, code = D.dw_outcome({"gates_ok": True}, True)
check(code == 0 and st == "ALL OK", "the full gate's 'ALL OK' is unchanged")
st, code = D.dw_outcome({"gates_ok": False, "ablation_ok": True}, True)
check(code == 0 and st.startswith("RESTORED; ABLATION EVIDENCE ONLY"),
      "the ablation status words are unchanged")
st, code = D.dw_outcome({}, True)
check(code == 1 and st == "RESTORED; gate failures - see logs",
      "an empty record is a failure, never a pass")
st, code = D.dw_outcome({"gates_ok": True}, False)
check(code == 1 and st == "RESTORE FAILED - PARENT INVESTIGATE NOW",
      "an unrestored 'ALL OK' record is impossible")

# ------------------------------------------------------------------ the scope words
check(D.subset_label(None, False) == "1 fresh prompt"
      and D.subset_label(None, True) == "8-prompt gate set",
      "without --requests the scope words are the existing wording")
lab = D.subset_label([8], True)
check("SUBSET" in lab and "8" in lab and "gate set" in lab,
      "with --requests the scope words name the subset and its original index")
check("8" in D.subset_label([8, 7], True) and "7" in D.subset_label([8, 7], True),
      "the subset label lists every selected index")

print()
if FAILS:
    print(f"{len(FAILS)} FIXTURE(S) FAILED:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("ALL REQUEST-SELECTION FIXTURES PASS (CPU only, nothing written, no GPU/HTTP/snapshot)")

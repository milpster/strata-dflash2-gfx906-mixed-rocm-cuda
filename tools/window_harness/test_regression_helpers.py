#!/usr/bin/env python3
"""CPU-ONLY unit fixtures for the L22 regression-window controller - PARENT executes these.

ZERO signals, ZERO GPU, ZERO HTTP, ZERO servers, ZERO /proc scan, ZERO snapshot, ZERO production
reads. Everything runs on SYNTHETIC configs, SYNTHETIC engine-log texts, a SYNTHETIC generate.cpp
text and temp dirs under /mnt/user/dflash2/ops. The REAL reviewed helpers are used where they are
pure (the cloner's clone_server_config, model_window.currency/flags, regression_window's
derive/check_derived/source_support/usage_proof/log_proof/evaluate_regression/write_derived_config/
identity_note/restore_gate) and the REAL `execute_checks` with its HTTP helper MOCKED; every lifecycle call
is a MOCK and its call ORDER is pinned.

Pinned behaviors:
 1. derivation = the reviewed dflash_w3 clone + ONLY the allowlist's flags at the allowlist's
    values, with the unique per-window log; the already/present/replaced/appended classification;
    valueless --no-prefill-borrow appended WITHOUT a value; every out-of-allowlist shape refused
    (changed value, extra flag, removed flag, reordering, exe/port/log swaps, non-absolute log).
 2. CURRENT currency: the REAL cloner's dflash_w3 clone passes the REAL model_window.currency
    against a synthetic production config; a stale width, a foreign flag, a missing production
    flag and a reordered clone are all refused.
 3. source support: the REAL generate.cpp is scanned read-only and every allowlisted flag must
    have its parse line; a flag with no parse line is a problem (fixture), and a file that cannot
    be read returns a LIST (never a set) so the controller never KeyErrors.
 4. identity-config handling: the EXISTING runner's pack arm keys on --model, which clones never
    carry (they use --native), so the pack identity is labeled NOTASSERTED - the controller states
    it, it never claims the pack was checked.
 5. runtime proofs in their L22 form: the reuse/reset proof is the RUNNER's structured HTTP usage
    (usage.prompt_tokens_details.cached_tokens, serve/server.py:5335) - repeat-2 > 0, the unrelated
    reset == 0, the cold repeat-1 never the witness, and all four requests COMPLETED; the sampled
    proof is the session log with its routing cited (generate.cpp:15775/12499 stderr ->
    serve/server.py:998 stderr=self.log): EXACTLY ONE fallback line, AFTER the greedy prompt lines.
    Extra fallback lines, a greedy-phase fallback, a missing line, a missing/absent field and a
    CLI-only text all fail. mw.evaluate's gate is used UNCHANGED; only the single expected sampled
    fallback is exempted, and the exemption is recorded.
 6. the EXISTING runner driven for real with its HTTP helper MOCKED (no socket): success, no
    reuse, failed sampled, missing props.model_path (no None-as-success), alias mismatch, missing
    cached_tokens field, no-results-path, thinking-off declarations, request order, temperature
    pattern, and the untouched production-absence/8009 guards.
 7. lifecycle mock ordering with a REAL-SHAPE snapshot mock (the masked display keys AND the
    in-memory '_env_full' device flags that run_regression merges into the child env - the
    parent's first run crashed on a mock that lacked it): stop -> start -> integration ->
    evidence -> evaluate -> cleanup, cleanup on a refused startup, cleanup on an interrupted
    integration, NOTHING after a refused stop, unreadable evidence fails, and the restore gate
    refusing while a survivor is recorded. Derived configs are written 0600 (mask 0o777).

 8. own-engine phases (--phase cancel-park | rewind): the command is the production VENV python
    running the CLONE's tool with the derived config (cancel_park_test.py: positional config;
    dflash_cache.py: --config/--engine(prototype)/--output), the env carries STRATA_REPO +
    PYTHONPATH = the PROTOTYPE tree's tools (never the production install) and
    STRATA_KEEP_LEFTOVERS=1 (the tool's leftover/orphan engine signalling, serve/server.py:103, is
    switched off); the tool's forced flags are recorded verbatim (dflash_cache forces context 4096 /
    spec 2 / block 1 / fixed caches / --spec-split) so a phase is never labeled as 262144 width-3
    coverage, and BOTH tools now add the config's structured split to the engine argv with the
    server's own functions (serve/server.py:2871 layer_split_of/layer_split_value) - the split is
    REQUIRED, and its presence is proven from the engine's OBSERVED /proc argv, never assumed;
    a phase REFUSES on a listener on 8019, on a live prototype-exe process, on a refused verified
    stop, and on a survivor; exactly ONE tool subprocess is launched, NO mw.start_mode call is ever
    made there; the tool's lineage is observed WHILE IT RUNS (a scripted child that appears only
    after the launch, and the three sequential engines of the rewind tool), each pid pinned with
    its start ticks and its exe/argv/cwd/env proof captured while it is alive; a timeout is exit
    124; ONLY the tool pid and OUR observed children are ever signalled (an unrelated same-exe
    process is a restore refusal and is never signalled); the child environment carries the live
    original engine's device/library flags while os.environ stays untouched; success REQUIRES at
    least one observed prototype engine carrying the config's --layer-split value - an empty
    engines[] plus a PASS substring never passes; the restore gate refuses while a phase survivor
    lives and runs the SAME window.py restore when all are gone. A static scan pins the
    controller's whole `subprocess.*` surface (Popen / TimeoutExpired / DEVNULL) and the Popen
    keywords the mock must accept - the parent's run #5 hit a missing DEVNULL.
 9. phase-only diagnostic controls and engine-evidence (L23, after the parent's 101959 cancel-park
    run measured "conversation cache: skip parking (1829 tokens, below the 2048 minimum; parked=0)"):
    `PHASE_CONTROLS` writes `--conversation-cache-min-tokens 1` and
    `--conversation-cache-park-gain 0` into the CANCEL-PARK phase's own config file only (asserting
    each flag exists, keeping every other arg in place, keeping 0600) - the integration config and
    every production file stay at the production 2048/4096, and 262144 / width 3 / the layer-32
    split / the target quant / PLE / adaptation-off all stay; `apply_phase_controls` REFUSES a
    control whose flag is absent (a phase never invents a flag). Source-confirmed sufficient: the
    size gate is `conversations.wants(live.size())` (generate.cpp:9516-9526, parse 2639) and park-gain
    0 "parks every turn" (12879-12887, parse 2640) - the same two controls dflash_cache.py forces.
    `PHASE_EVIDENCE` demands, for cancel-park, that the engine's OWN log lines show a
    `conversation cache: parked` event (9691) AND a `conversation cache: restored` event (13075) -
    a tool PASS with nothing parked is REFUSED, so the tool's words can never become coverage. The
    tool side is strengthened the same way: it prints no unproved "A is parked now", it reads only
    the bytes the engine appended after a pre-launch offset, and it stops with
    `PRECONDITION FAIL` / exit 3 when nothing was parked or nothing was restored, echoing the
    engine's own reason; its real thresholds (`fin2 == "cancel"`, `reused3 >= len(A1)+len(outA)-3`)
    are unchanged.

Run: python3 ops/test_regression_helpers.py     (exit 0 = all fixtures pass)
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                                  # the snapshot dir: siblings resolve here
import regression_window as R
import window as W
import model_window as mw

# Fixture TEMP OUTPUT ONLY: window.py's existing external project ops dir (the same dir the real
# controllers write their results into), never this snapshot dir - the source tree stays clean.
# Assertions are untouched: TMP is a scratch dir either way and the real config dir is never used.
TMP = pathlib.Path(tempfile.mkdtemp(dir=str(W.OPS), prefix="tmp-regfixtures-"))
FAILS: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        FAILS.append(what)


def synth_prod() -> dict:
    """Synthetic production config with the real schema keys and the real flag SHAPES (no real
    paths are read, nothing here touches /mnt/user/Strata)."""
    args = ["--gpu", "0,1", "--pack", "/synth/pack", "--native", "/synth/model.gguf",
            "--native-dense-gguf", "/synth/model.gguf", "--ple-gguf", "/synth/ple.gguf",
            "--expert-profile", "/synth/profile.bin",
            "--expert-cache", "auto", "--prefill", "8192", "--spec", "6", "--spec-min-p", "0.7",
            "--max-context", "262144", "--kv", "int8", "--vram-reserve-mib", "896",
            "--vram-reserve-later-mib", "1000", "--kv-resident", "262144", "--pcie-frac", "0",
            "--expert-cache-cpu-order", "--prompt-cache-spread", "--conversation-cache-mib", "2048",
            "--conversation-cache-min-tokens", "2048", "--conversation-cache-park-gain", "4096",
            "--vision"]
    return {"exe": "/synth/build/strata", "args": args, "cwd": "/synth/cwd",
            "tokenizer": "/synth/tokenizer", "model_name": "swift-1.5-iq3_xxs",
            "log": "/mnt/user/dflash2/logs/engine-synth-prod.log", "env": {"HSA_OVERRIDE_GFX_VERSION": "9.0.6"},
            "lib_dirs": ["/synth/lib"], "port": 8009, "host": "127.0.0.1", "gpu": [0, 1],
            "layer_split": "32", "backend": "hip", "vision": {"exe": "/synth/vision"},
            "sampling": {"temperature": 1.0}, "reasoning_effort": "high",
            "reasoning_budget_tokens": 55000, "fit_max_tokens": True, "env_to_user": True}


def base_clone() -> dict:
    return R.clone_server_config("dflash_w3", synth_prod())


def drop_flag(args: list[str], flag: str) -> list[str]:
    """Fixture-side removal of ONE flag (with its value when it has one). It ASSERTS the flag is
    present first, so a mutation can never die with a bare ValueError from index() - the parent's
    run #2 hit exactly that on a flag the synthetic config did not carry."""
    assert flag in args, f"fixture requires {flag} present before removal"
    i = args.index(flag)
    n = 1 if i + 1 >= len(args) or args[i + 1].startswith("--") else 2
    return args[:i] + args[i + n:]


# ------------------------------------------------------------------ 1. derivation + allowlist
def test_derivation():
    base = base_clone()
    d, ov, problems = R.derive("W0", base)
    check(problems == [], f"clean derivation has zero problems ({problems[:2]})")
    fa = dict(mw.flags(d["args"]))
    check(fa["--expert-cache"] == "2000" and fa["--prefill"] == "512" and fa["--adapt-swaps"] == "0"
          and fa["--prompt-cache"] == "4" and fa["--conversation-cache-mib"] == "2048"
          and fa["--conversation-cache-slots"] == "3" and "--no-prefill-borrow" in fa
          and fa["--no-prefill-borrow"] is None,
          "every allowlisted control is present at its intended value (valueless flag carries none)")
    act = {o["flag"]: o["action"] for o in ov}
    check(act.get("--expert-cache") == "replaced" and act.get("--prefill") == "replaced"
          and act.get("--conversation-cache-mib") == "already"
          and act.get("--prompt-cache") == "appended" and act.get("--conversation-cache-slots") == "appended"
          and act.get("--no-prefill-borrow") == "appended" and act.get("--adapt-swaps") == "appended",
          "overrides classified already/replaced/appended (the clone's 2048 is 'already', not a diff)")
    check(d["log"] == f"{W.LOGS}/engine-regwindow-W0-dflash_w3.log",
          "unique per-window log under the dflash2 logs dir")
    check(d["port"] == mw.PROTO_PORT and d["exe"] == mw.PROTO_BINARY,
          "derived config stays on the isolated port with the prototype binary")
    check(d.get("layer_split") == base.get("layer_split") and fa["--max-context"] == "262144"
          and fa["--kv"] == base_kv(base) and fa["--ple-gguf"] == "/synth/ple.gguf"
          and fa["--vram-reserve-mib"] == "896" and "vision" in d,
          "context/layer-split/target quant/PLE/vision/reserves are the clone's, untouched by derivation")
    i = d["args"].index("--no-prefill-borrow")
    check(d["args"][i + 1].startswith("--"), "--no-prefill-borrow is valueless in the args (parse unambiguous)")
    check(base["args"] == R.clone_server_config("dflash_w3", synth_prod())["args"],
          "the reviewed clone is never mutated by derivation (deep copy + list copy)")


def base_kv(base: dict) -> str:
    return dict(mw.flags(base["args"]))["--kv"]


def test_allowlist_refusals():
    base = base_clone()
    d, _ov, _p = R.derive("W0", base)
    t = copy.deepcopy(d)
    t["args"][t["args"].index("--max-context") + 1] = "131072"
    check(bool(R.check_derived("W0", base, t)), "refusal: a context change outside the allowlist")
    t = copy.deepcopy(d); t["args"] += ["--kv", "fp16"]
    check(bool(R.check_derived("W0", base, t)), "refusal: a quant flag outside the allowlist")
    t = copy.deepcopy(d)
    t["args"] = drop_flag(t["args"], "--adapt-swaps")          # presence asserted by the helper
    check(bool(R.check_derived("W0", base, t)), "refusal: an allowlisted control removed from the derived args")
    t = copy.deepcopy(d)
    t["args"] = drop_flag(t["args"], "--spec") + ["--spec", "4"]   # flag MOVED to the end: order differs
    check(bool(R.check_derived("W0", base, t)), "refusal: reordered flags (derivation appends, never reorders)")
    t = copy.deepcopy(d); t["exe"] = W.PROD_EXE
    check(bool(R.check_derived("W0", base, t)), "refusal: the production binary in a derived config")
    t = copy.deepcopy(d); t["port"] = 8009
    check(bool(R.check_derived("W0", base, t)), "refusal: the production port in a derived config")
    t = copy.deepcopy(d); t["log"] = f"{W.LOGS}/engine-prototype-dflash_w3.log"
    check(bool(R.check_derived("W0", base, t)), "refusal: a shared (non-unique) engine log")
    t = copy.deepcopy(d); t["log"] = "engine-relative.log"
    check(bool(R.check_derived("W0", base, t)), "refusal: a relative log (would land inside the prod tree)")
    t = copy.deepcopy(d); t["args"] += ["--no-prefill-borrow"]
    check(bool(R.check_derived("W0", base, t)), "refusal: a duplicated flag in the derived args")
    clean = copy.deepcopy(d)
    check(R.check_derived("W0", base, clean) == [], "the untampered derived config passes its own check")


# ------------------------------------------------------------------ 2. currency (current clone)
def test_currency():
    prod = synth_prod()
    base = base_clone()
    old_dir, old_logs = mw.CONFIG_DIR, mw.LOGS
    mw.CONFIG_DIR = TMP                      # fixture dir only: the real config dir is never read/written
    mw.LOGS = str(old_logs)                  # log_want must keep the real absolute log shape
    try:
        (mw.CONFIG_DIR / "proto-server-swift15-dflash_w3.json").write_text(json.dumps(base))
        summary, problems = mw.currency("dflash_w3", 3, prod)
        check(problems == [], f"the real cloner's dflash_w3 clone is CURRENT ({problems[:2]})")
        check(summary["args_added"] and "--dflash" in summary["args_added"],
              "currency reports the template's added drafter flags")
        stale = copy.deepcopy(base)
        stale["args"][stale["args"].index("--dflash-block") + 1] = "2"
        (mw.CONFIG_DIR / "proto-server-swift15-dflash_w3.json").write_text(json.dumps(stale))
        check(bool(mw.currency("dflash_w3", 3, prod)[1]), "currency refuses a stale width (block/spec template)")
        foreign = copy.deepcopy(base); foreign["args"] += ["--adapt-swaps", "0"]
        (mw.CONFIG_DIR / "proto-server-swift15-dflash_w3.json").write_text(json.dumps(foreign))
        check(bool(mw.currency("dflash_w3", 3, prod)[1]),
              "currency refuses a control flag baked into the shared clone (control lives in derivation only)")
        missing = copy.deepcopy(base)
        missing["args"] = [a for a in missing["args"] if a != "--expert-cache-cpu-order"]
        (mw.CONFIG_DIR / "proto-server-swift15-dflash_w3.json").write_text(json.dumps(missing))
        check(bool(mw.currency("dflash_w3", 3, prod)[1]), "currency refuses a production flag absent from the clone")
        reordered = copy.deepcopy(base); reordered["args"] = reordered["args"][::-1]
        (mw.CONFIG_DIR / "proto-server-swift15-dflash_w3.json").write_text(json.dumps(reordered))
        check(bool(mw.currency("dflash_w3", 3, prod)[1]), "currency refuses a reordered clone")
        (mw.CONFIG_DIR / "proto-server-swift15-dflash_w3.json").unlink()
        check(any("missing" in p for p in mw.currency("dflash_w3", 3, prod)[1]),
              "currency refuses a missing config file")
        check(bool(mw.currency("invented_mode", 1, prod)[1]),
              "currency refuses an invented mode STRUCTURALLY (no template guessing from a filename)")
    finally:
        mw.CONFIG_DIR, mw.LOGS = old_dir, old_logs
    check(mw.CONFIG_DIR != TMP and str(mw.LOGS) == str(old_logs), "the fixture patches are restored")


# ------------------------------------------------------------------ 3. source support
def test_source_support():
    proof, problems = R.source_support()
    check(problems == [], f"every allowlisted flag has a real parse line in generate.cpp ({problems[:2]})")
    found = proof.get("flags_line", {})
    check(all(n > 0 for n in found.values()) and len(found) == len(R.ALLOW),
          "source support covers exactly the allowlist, with real line numbers")
    check(f"{mw.CLONE}/src/program/generate.cpp" == proof["source"], "the scan reads the clone's own source")
    keep = dict(R.ALLOW)
    R.ALLOW["--invented-control-flag"] = ("7", "fixture: no such parse")
    p2, prob2 = R.source_support()
    R.ALLOW.clear(); R.ALLOW.update(keep)
    check(any("invented-control-flag" in p for p in prob2), "a flag with no source parse is a problem")
    keep = dict(R.ALLOW)
    try:
        R.ALLOW = {}
        _p3, prob3 = R.source_support()
        check(prob3 == [], "an empty allowlist yields no source problems")
    finally:
        R.ALLOW = keep
    check(R.ALLOW == keep, "the allowlist fixture is restored")


# ------------------------------------------------------------------ 4. identity-config handling
def test_identity():
    d, _ov, _p = R.derive("W0", base_clone())
    note = R.identity_note(d)
    check(note["pack_want"] == "/synth/model.gguf" and note["pack_source_flag"] == "--native",
          "identity takes the pack from --native (the flag clones actually carry)")
    check(note["model_alias_want"] == d["model_name"] and note["no_none_as_success"] is True,
          "the controller records the exact model_alias and states no None-as-success")
    check("NOTASSERTED" not in str(note["pack_want"]) and "inert" in note["history"],
          "the pre-L22 inert pack arm is recorded as HISTORY, not as a current claim")
    check("--native" in d["args"] and "--native-dense-gguf" in d["args"],
          "the synthetic clone carries BOTH native flags, exactly as the current production config does")
    no_dense = copy.deepcopy(d)
    no_dense["args"] = drop_flag(no_dense["args"], "--native-dense-gguf")   # presence asserted by the helper
    n2 = R.identity_note(no_dense)
    check(n2["pack_want"] == "/synth/model.gguf" and n2["pack_source_flag"] == "--native",
          "dropping --native-dense-gguf leaves the identity source unchanged (--native IS the pack)")
    no_native = copy.deepcopy(d)
    no_native["args"] = drop_flag(no_native["args"], "--native")
    n3 = R.identity_note(no_native)
    check(n3["pack_want"] is None and n3["pack_source_flag"] is None,
          "with no --native/--model the controller records NO pack (the runner skips that arm, stated)")


# ------------------------------------------------------------------ 5. runtime proofs (HTTP usage + session log)
SERVE_SAMPLED = "dflash: sampled request uses target-only decoding"
CLI_SAMPLED = "dflash: sampled CLI requests use target-only decoding"


def test_usage_proof():
    p = R.usage_proof(good_cases())
    check(p["ok"] and p["four_completed"] and p["repeat_cached"] == 118 and p["reset_cached"] == 0,
          "good cases: four completed, repeat-2 cached 118 > 0, reset-new cached 0 (server.py:5335 shape)")
    no_reuse = [dict(c) for c in good_cases()]
    no_reuse[1]["usage"]["prompt_tokens_details"]["cached_tokens"] = 0
    q = R.usage_proof(no_reuse)
    check(not q["ok"] and any("not > 0" in w for w in q["why"]),
          "repeat-2 cached_tokens 0: repeat reuse NOT proven (a printed request is not a proof)")
    no_field = [dict(c) for c in good_cases()]
    no_field[1]["usage"].pop("prompt_tokens_details")
    q = R.usage_proof(no_field)
    check(not q["ok"] and any("no integer" in w for w in q["why"]),
          "repeat-2 without prompt_tokens_details: the field is REQUIRED, never defaulted to success")
    dirty_reset = [dict(c) for c in good_cases()]
    dirty_reset[2]["usage"]["prompt_tokens_details"]["cached_tokens"] = 77
    q = R.usage_proof(dirty_reset)
    check(not q["ok"] and any("is not 0" in w for w in q["why"]),
          "reset-new with cached_tokens 77: the unrelated conversation did not start clean")
    cold_only = [dict(c) for c in good_cases()]
    cold_only[1]["ok"] = False                       # only the cold repeat-1 reuses, nothing completed twice
    q = R.usage_proof(cold_only)
    check(not q["ok"] and not q["four_completed"],
          "a cold repeat-1 never counts as the reuse/reset witness; an incomplete lane fails")
    sampled_failed = [dict(c) for c in good_cases()]
    sampled_failed[3]["ok"] = False
    sampled_failed[3]["err"] = "OSError: connection reset"
    q = R.usage_proof(sampled_failed)
    check(not q["ok"] and not q["four_completed"], "a failed sampled request fails completion")
    q = R.usage_proof([])
    check(not q["ok"] and not q["four_completed"], "no cases at all: completion fails (nothing invented)")


def test_log_proof():
    lp = R.log_proof(good_log())
    check(lp["ok"] and lp["sampled_count"] == 1 and lp["sampled_after_greedy"] and lp["prompt_lines"] == 4,
          f"good log: one sampled fallback line AFTER the three greedy prompt lines ({lp['why']})")
    check("12499" in lp["routing"] and "998" in lp["routing"],
          "the proof cites the routing (stderr at 15775/12499 -> stderr=self.log at server.py:998)")
    missing = "\n".join([l for l in good_log().splitlines() if "sampled request" not in l])
    lp = R.log_proof(missing)
    check(not lp["ok"] and lp["sampled_count"] == 0 and any("12499" in w for w in lp["why"]),
          "no sampled line in the session copy: the fallback is UNPROVEN (never assumed from the HTTP 200)")
    extra = good_log() + SERVE_SAMPLED + "\n"
    lp = R.log_proof(extra)
    check(not lp["ok"] and lp["sampled_count"] == 2 and any("beyond the sampled case" in w for w in lp["why"]),
          "two fallback lines: an extra target-only fallback stays a FAILURE")
    greedy_phase = SERVE_SAMPLED + "\n" + good_log()
    lp = R.log_proof(greedy_phase)
    check(not lp["ok"] and not lp["sampled_after_greedy"]
          and any("during the greedy" in w for w in lp["why"]),
          "a fallback line DURING the greedy requests is a failure, not an exemption")
    cli = good_log().replace(SERVE_SAMPLED, CLI_SAMPLED)
    lp = R.log_proof(cli)
    check(not lp["ok"] and lp["sampled_count"] == 0,
          "the CLI-only text (2831) never satisfies the serve fallback proof")
    lp = R.log_proof("")
    check(not lp["ok"] and lp["prompt_lines"] == 0, "an empty session copy fails every direction")


def test_fallback_exemption():
    """mw.evaluate's gate is used UNCHANGED; only the sampled case's single, correctly positioned
    fallback line is exempted. Extra fallbacks and greedy-phase fallbacks remain failures."""
    rec = {"evidence": {"startup_lines": ["taps [0, 8, 16, 24, 32]", "cross-GPU tap collection serial",
                                          "shared target head", "128 MiB of VRAM held"],
                        "fallback_lines": ["dflash: sampled request uses target-only decoding"],
                        "acceptance": [{"accepted": 3, "offered": 4}], "window_counter_lines": []}}
    bad = R.evaluate_regression("dflash_w3", rec, R.log_proof(good_log()))
    check(bad == [], "the single expected sampled fallback is exempted; activation checks stay")
    check("sampled_fallback_exempted" in rec, "the exemption is RECORDED with its lines and citation")
    rec2 = {"evidence": dict(rec["evidence"], fallback_lines=[SERVE_SAMPLED, "target-only decoding engaged"])}
    bad = R.evaluate_regression("dflash_w3", rec2, R.log_proof(good_log() + SERVE_SAMPLED + "\n"))
    check(any("MODE FAILURE" in b for b in bad) and "sampled_fallback_exempted" not in rec2,
          "an extra fallback line: the MODE FAILURE stays, no exemption")
    rec3 = {"evidence": dict(rec["evidence"], fallback_lines=[SERVE_SAMPLED])}
    bad = R.evaluate_regression("dflash_w3", rec3, R.log_proof(greedy_phase := SERVE_SAMPLED + "\n" + good_log()))
    check(any("MODE FAILURE" in b for b in bad),
          "a fallback positioned during greedy: the MODE FAILURE stays (position is checked, not text)")
    rec4 = {"evidence": dict(rec["evidence"], startup_lines=["taps [0, 8, 16, 24, 32]"])}
    bad = R.evaluate_regression("dflash_w3", rec4, R.log_proof(good_log()))
    check(any("collector" in b for b in bad) and any("VRAM" in b for b in bad),
          "geometry/collector/VRAM checks are preserved, not weakened")
    rec5 = {"evidence": dict(rec["evidence"], acceptance=[{"accepted": 0, "offered": 0}])}
    bad = R.evaluate_regression("dflash_w3", rec5, R.log_proof(good_log()))
    check(any("no draft was ever offered" in b for b in bad), "the draft-offer check is preserved")
    rec6 = {"evidence": dict(rec["evidence"],
                             fallback_lines=[SERVE_SAMPLED, "verify: timed out"])}
    bad = R.evaluate_regression("dflash_w3", rec6, R.log_proof(good_log()))
    check(any("watchdog" in b for b in bad), "watchdog/timeout lines stay failures")
    rec7 = {"evidence": {"startup_lines": [], "fallback_lines": [], "acceptance": [],
                         "window_counter_lines": []}}          # a GENUINE AR record: no drafter lines, no offers
    bad = R.evaluate_regression("target_only", rec7, {"ok": True, "sampled_count": 0, "sampled_after_greedy": False})
    check(bad == [], "default target_only smoke is unchanged: no drafter lines, no offers, no fallback -> clean")
    rec8 = {"evidence": {"startup_lines": ["taps [0, 8, 16, 24, 32]", "cross-GPU tap collection serial",
                                           "shared target head", "128 MiB of VRAM held"],
                          "fallback_lines": [SERVE_SAMPLED],
                          "acceptance": [{"accepted": 3, "offered": 4}], "window_counter_lines": []}}
    bad = R.evaluate_regression("dflash_w3", rec8, {"ok": False, "sampled_count": 1, "sampled_after_greedy": False})
    check(any("MODE FAILURE" in b for b in bad) and "sampled_fallback_exempted" not in rec8,
          "an unexpected fallback on a VALID dflash_w3 record (proof not satisfied) stays a MODE FAILURE")


def test_write_config_mode():
    p = TMP / "derived-config-mode.json"
    d, _ov, _p = R.derive("W0", base_clone())
    R.write_derived_config(p, d)
    mode = p.stat().st_mode & 0o777        # L19 lesson: mask 0o777, not 0o077 (0600 has 0 in the last two digits)
    check(mode == 0o600, f"the derived config is written owner-only 0600 (mode {oct(mode)})")
    check(json.loads(p.read_text())["log"].endswith("engine-regwindow-W0-dflash_w3.log"),
          "the written config carries the unique per-window log")
    check(p not in list(mw.CONFIG_DIR.glob("proto-server-swift15-dflash_w3*.json")) if mw.CONFIG_DIR.is_dir() else True,
          "the fixture never writes into the real config dir (TMP only)")


def test_structured_drift():
    base = base_clone()
    d, _ov, _p = R.derive("W0", base)
    t = copy.deepcopy(d); t["vision"] = {"exe": "/synth/other-vision"}
    check(any("vision" in p for p in R.check_derived("W0", base, t)),
          "refusal: the vision block drifted from the clone")
    t = copy.deepcopy(d); t["tokenizer"] = "/synth/other-tokenizer"
    check(any("tokenizer" in p for p in R.check_derived("W0", base, t)), "refusal: the tokenizer drifted")
    t = copy.deepcopy(d); t["layer_split"] = "16"
    check(any("layer_split" in p for p in R.check_derived("W0", base, t)), "refusal: layer_split drifted")
    t = copy.deepcopy(d); t["env"] = {"HSA_OVERRIDE_GFX_VERSION": "0.0.0"}
    check(any("env" in p for p in R.check_derived("W0", base, t)), "refusal: the engine env block drifted")
    t = copy.deepcopy(d); t["sampling"] = {"temperature": 0.0}
    check(any("sampling" in p for p in R.check_derived("W0", base, t)), "refusal: the sampling block drifted")
    t = copy.deepcopy(d); t["reasoning_effort"] = "low"
    check(any("reasoning_effort" in p for p in R.check_derived("W0", base, t)),
          "refusal: reasoning_effort drifted from the clone")
    t = copy.deepcopy(d); t["an_extra_key"] = 1
    check(any("an_extra_key" in p for p in R.check_derived("W0", base, t)),
          "refusal: a key the clone does not have (no silent schema growth)")
    t = copy.deepcopy(d); del t["lib_dirs"]
    check(any("lib_dirs" in p for p in R.check_derived("W0", base, t)),
          "refusal: a clone key dropped from the derived config")
    check(R.check_derived("W0", base, copy.deepcopy(d)) == [], "the untampered derived config stays clean")


# ------------------------------------------------------------------ 6. the EXISTING runner (mocked HTTP, no network)
import test_inco_dflash2_integration as I

CHAT = "/v1/chat/completions"
PROMPT_TEXT = "Explain, in two short sentences, why layer splitting moves K/V pools per stage."
RESET_TEXT = "Count from 1 to 5."
SAMPLED_TEXT = "Say hi."


def runner_case(specs: list, props: dict, results_name: str | None = "runner-results.json"):
    """Drives the REAL execute_checks with the HTTP helper replaced by an in-memory fake: the four
    chat calls are consumed in ORDER (repeat-1, repeat-2, reset-new, sampled), so order and
    thinking-off declarations are asserted, and NO socket is ever opened."""
    cfg_path = TMP / "runner-identity-config.json"
    d, _ov, _p = R.derive("W0", base_clone())
    R.write_derived_config(cfg_path, d)
    keep, calls, seq = I.http_json, [], []

    def fake(url: str, payload: dict | None = None, timeout: float = 120.0) -> dict:
        calls.append((url, payload))
        if url.endswith("/health"):
            return {"status": "ok"}
        if url.endswith("/props"):
            return props
        i = len(seq)
        seq.append(i)
        spec = specs[i]
        if isinstance(spec, OSError):
            raise spec
        usage = {"prompt_tokens": spec["prompt"], "completion_tokens": spec["gen"],
                 "total_tokens": spec["prompt"] + spec["gen"]}
        if "cached" in spec:
            usage["prompt_tokens_details"] = {"cached_tokens": spec["cached"]}
        return {"choices": [{"message": {"role": "assistant", "content": spec["text"]}}], "usage": usage}

    I.http_json = fake
    rp = TMP / results_name if results_name else None
    try:
        rc = I.execute_checks("http://127.0.0.1:8019", str(cfg_path), str(rp) if rp else None)
    finally:
        I.http_json = keep
    out = json.loads(rp.read_text()) if rp else None
    return rc, out, calls, cfg_path


GOOD_SPECS = [{"prompt": 120, "cached": 0, "gen": 8, "text": "answer one"},
              {"prompt": 120, "cached": 118, "gen": 8, "text": "answer one"},
              {"prompt": 9, "cached": 0, "gen": 6, "text": "1, 2, 3, 4, 5"},
              {"prompt": 12, "cached": 0, "gen": 4, "text": "hi"}]
GOOD_PROPS = {"model_alias": "swift-1.5-iq3_xxs", "model_path": "/synth/model.gguf"}


def test_runner_success():
    rc, out, calls, cfg = runner_case(GOOD_SPECS, GOOD_PROPS)
    check(rc == 0, f"good lane: the REAL execute_checks exits 0 on the mocked lane (rc {rc})")
    check(out is not None and out["completed"] == 4 and out["fails"] == 0,
          "results: four cases COMPLETED, zero fails (completion is asserted, not printing)")
    check(out["repeat_cached"] == 118 and out["reset_cached"] == 0,
          "results record the real cached_tokens values (repeat 118 > 0, reset 0)")
    by = {c["label"]: c for c in out["cases"]}
    check(all(by[l]["usage"].get("prompt_tokens_details", {}).get("cached_tokens") is not None
             for l in by), "every case carries the FULL usage block, not a truncated summary")
    check(all(isinstance(by[l].get("text"), str) for l in by), "every case carries the returned text")
    chat = [p for u, p in calls if u.endswith(CHAT)]
    check(len(chat) == 4 and [p["messages"][0]["content"] for p in chat] ==
          [PROMPT_TEXT, PROMPT_TEXT, RESET_TEXT, SAMPLED_TEXT],
          "request order is repeat-1, repeat-2, reset-new, sampled")
    check(all(p.get("reasoning_effort") == "none"
              and (p.get("chat_template_kwargs") or {}).get("enable_thinking") is False for p in chat),
          "every short test declares thinking OFF explicitly (reasoning_effort=none + enable_thinking=false)")
    check([p["temperature"] for p in chat] == [0.0, 0.0, 0.0, 1.0],
          "the sampled case keeps temperature > 0 (the fallback profile depends on it)")
    check(out["pack_want"] == "/synth/model.gguf" and out["model"] == "swift-1.5-iq3_xxs",
          "identity is taken from --native and the config's model_name")
    check(not any("results" in u for u, _ in calls), "no HTTP call is made to any results path")


def test_runner_refusals():
    print("   (the tool's own 'FAIL ...' lines below are the EXPECTED stdout of each negative lane; the "
          "fixture verdicts are the ok/FAIL lines printed by check() - the lanes are delimited below)")

    def lane(name: str, specs: list, props: dict, results: str):
        print(f"   >>> negative lane: {name}")
        return runner_case(specs, props, results)

    no_reuse = [dict(s) for s in GOOD_SPECS]; no_reuse[1]["cached"] = 0
    rc, out, _c, _cfg = lane("no reuse", no_reuse, GOOD_PROPS, "r-no-reuse.json")
    check(rc == 1 and out["repeat_cached"] == 0, "no reuse: repeat-2 cached 0 -> exit 1, recorded")

    failed_sampled = [dict(s) for s in GOOD_SPECS]
    failed_sampled[3] = OSError("connection reset")
    rc, out, _c, _cfg = lane("failed sampled", failed_sampled, GOOD_PROPS, "r-sampled.json")
    check(rc == 1 and out["completed"] == 3,
          "failed sampled request: exit 1 and completed 3 (a printed line is never a completion)")
    check([c for c in out["cases"] if c["label"] == "sampled"][0]["ok"] is False,
          "the failed sampled case is recorded with ok False")

    rc, out, _c, _cfg = lane("missing props.model_path", GOOD_SPECS,
                             {"model_alias": "swift-1.5-iq3_xxs"}, "r-nopath.json")
    check(rc == 1, "missing props.model_path: the identity check FAILS (no None-as-success)")
    rc, out, _c, _cfg = lane("alias mismatch", GOOD_SPECS,
                             {"model_alias": "other-alias", "model_path": "/synth/model.gguf"}, "r-alias.json")
    check(rc == 1, "a model_alias that differs from the config FAILS (exact match required)")

    missing_field = [dict(s) for s in GOOD_SPECS]
    del missing_field[2]["cached"]
    rc, out, _c, _cfg = lane("missing cached_tokens field", missing_field, GOOD_PROPS, "r-nofield.json")
    check(rc == 1 and out["reset_cached"] is None,
          "reset-new without prompt_tokens_details: exit 1, never defaulted to success")

    rc, out, _c, _cfg = lane("no results path", GOOD_SPECS, GOOD_PROPS, None)
    check(rc == 0 and out is None, "no --results-json: the lane still runs and asserts (stdout only)")
    check(I.http_json.__name__ == "http_json", "the HTTP helper is restored after every mocked lane")


def test_runner_guards_unchanged():
    check(I.guard_execute("http://127.0.0.1:8009", True) is not None,
          "guard: production port 8009 stays refused (untouched by the L22 edit)")
    check(I.guard_execute("http://127.0.0.1:8019", False) is not None,
          "guard: --confirm-execution is still required")
    orig = I.prod_status
    try:
        I.prod_status = lambda: "running"
        check(I.guard_execute("http://127.0.0.1:8019", True) is not None,
              "guard: production lineage RUNNING still refuses (the absence guard is not waivable)")
        I.prod_status = lambda: "unknown"
        check(I.guard_execute("http://127.0.0.1:8019", True) is not None,
              "guard: unknown lineage still refuses (fail closed)")
    finally:
        I.prod_status = orig
    check(I.prod_status is orig, "the lineage fixture is restored")
    src = pathlib.Path(f"{mw.CLONE}/tools/test_inco_dflash2_integration.py").read_text()
    check(src.count("--results-json") == 1 and "REFUSED: production lineage detection" in src
          and "prod-config-swift15.json" in src and I.execute_checks.__code__.co_argcount == 3,
          "the L22 edits are confined to execute_checks + one optional results flag; the guard text stays")
def good_cases() -> list[dict]:
    """The runner's structured per-case shape (the L22 format) with real cached_tokens values."""
    def u(prompt: int, cached: int, gen: int = 8) -> dict:
        return {"prompt_tokens": prompt, "completion_tokens": gen, "total_tokens": prompt + gen,
                "prompt_tokens_details": {"cached_tokens": cached}}
    return [{"label": "repeat-1", "ok": True, "err": None, "temperature": 0.0,
             "thinking": "off (reasoning_effort=none, enable_thinking=false)", "usage": u(120, 0), "text": "one"},
            {"label": "repeat-2", "ok": True, "err": None, "temperature": 0.0,
             "thinking": "off (reasoning_effort=none, enable_thinking=false)", "usage": u(120, 118), "text": "one"},
            {"label": "reset-new", "ok": True, "err": None, "temperature": 0.0,
             "thinking": "off (reasoning_effort=none, enable_thinking=false)", "usage": u(9, 0), "text": "12345"},
            {"label": "sampled", "ok": True, "err": None, "temperature": 1.0,
             "thinking": "off (reasoning_effort=none, enable_thinking=false)", "usage": u(12, 0, 4), "text": "hi"}]


def good_log() -> str:
    """The serve session's engine-log lines: THREE greedy prompt lines (the engine prints them to
    stderr at generate.cpp:15775, routed to the log by serve/server.py:998) then the single
    sampled fallback line (generate.cpp:12499), then the sampled request's own prompt line."""
    def p(prompt: int, reused: int, gen: int = 8) -> str:
        return (f"strata serve: prompt {prompt} tokens = {reused} reused + {prompt - reused} read in "
                f"100.0 ms (1200.0 tok/s), {gen} generated in 40 ms (200.0 tok/s), drafts accepted 2 of 3")
    return "\n".join([p(120, 0), p(120, 118), p(9, 0),
                      "dflash: sampled request uses target-only decoding", p(12, 0, 4)]) + "\n"


def lifecycle(orders: list, start_ok: bool = True, integration_raises: bool = False,
              stop_ok: bool = True, ev_ok: bool = True, cases: list[dict] | None = None,
              log_text: str | None = None) -> dict:
    """Mock snapshot with the REAL snapshot's shape (W.snapshot() always carries the masked display
    keys AND the in-memory '_env_full' device flags that run_regression merges into the child env)."""
    snap = {"verified": True, "proxy": 100, "proxy_ticks": 1000, "proxy_pgid": 100,
            "engine": 101, "engine_ticks": 1001, "engine_exe": W.PROD_EXE, "vision": {},
            "config_sha256": "0" * 64, "maxctx_in_original": True,
            "engine_device_env_masked": {"HSA_OVERRIDE_GFX_VERSION": "9.0.6"},
            "_env_full": {"HSA_OVERRIDE_GFX_VERSION": "9.0.6", "HIP_FORCE_P2P": "1",
                          "CUDA_VISIBLE_DEVICES": "0,1", "LD_LIBRARY_PATH": "/synth/rocm/lib"}}
    record = {"window": "W0", "stop_sent": False, "modes": []}
    d, _ov, _p = R.derive("W0", base_clone())
    cfg_path = TMP / "proto-server-swift15-dflash_w3-reg-W0.json"

    keep = {k: getattr(R.W, k) for k in ("stop_prod", "restore_and_verify")}
    keep.update({k: getattr(R.mw, k) for k in ("start_mode", "draft_evidence", "evaluate", "stop_mode",
                                               "proto_survivors")})
    keep_run = R.run_integration
    log = TMP / "engine-regwindow-W0-dflash_w3.log"
    log.write_text(good_log() if log_text is None else log_text)

    R.W.stop_prod = lambda s, r, n: (orders.append("stop_prod"), r.__setitem__("stop_sent", True),
                                     (stop_ok,)[0])[-1]
    def fake_start(window, mode, cfg, width, env, rec, note):
        orders.append("start_mode")
        rec["env_device_flags_merged"] = env.get("HSA_OVERRIDE_GFX_VERSION")
        rec.update({"server_pid": 1, "server_ticks": 1, "engine": 2, "engine_ticks": 2,
                    "family": {"2": 2}, "vision": {}, "config": str(cfg_path)})
        return start_ok
    R.mw.start_mode = fake_start

    def fake_integration(cfg_p, window, rec, note):
        orders.append("run_integration")
        rec["integration"] = {"exit": 0, "note": "ok", "log": str(TMP / "i.log"), "tail": [],
                              "results": str(TMP / "i.json"), "n_cases": 4}
        rec["cases"] = good_cases() if cases is None else cases
        if integration_raises:
            raise RuntimeError("integration exploded")
        return True
    R.run_integration = fake_integration

    def fake_ev(window, cfg, mode, rec):
        orders.append("draft_evidence")
        rec["engine_log"] = {"per_window_copy": str(log)}
        return ev_ok
    R.mw.draft_evidence = fake_ev
    R.mw.evaluate = lambda mode, rec: (orders.append("evaluate"), [])[1]
    R.mw.stop_mode = lambda mode, rec, note: (orders.append("stop_mode"), True)[1]
    try:
        R.run_regression(snap, record, d, cfg_path, lambda m: orders.append(f"note:{m[:20]}"))
    finally:
        for k, v in keep.items():
            mod = R.W if k in ("stop_prod", "restore_and_verify") else R.mw
            setattr(mod, k, v)
        R.run_integration = keep_run
    return record


def test_lifecycle_order():
    orders: list[str] = []
    rec = lifecycle(orders)
    steps = [o for o in orders if not o.startswith("note:")]
    check(steps == ["stop_prod", "start_mode", "run_integration", "draft_evidence", "evaluate", "stop_mode"],
          f"order is stop -> start -> integration -> evidence -> evaluate -> cleanup ({steps})")
    mode = rec["modes"][0]
    check(mode.get("mode_ok") is True and mode.get("family_gone") is True,
          f"a clean lane reports mode_ok with the owned family gone ({mode.get('mode_failures')})")
    check(mode.get("usage_proof", {}).get("ok") is True and mode.get("log_proof", {}).get("ok") is True,
          "both proofs recorded: HTTP cached_tokens (reuse+reset) and the session-log sampled fallback")
    check(rec["modes"][0].get("env_device_flags_merged") == "9.0.6",
          "the mock snapshot carried the real snapshot's '_env_full' and it merged into the child env")

    no_reuse = [dict(c) for c in good_cases()]
    no_reuse[1]["usage"]["prompt_tokens_details"]["cached_tokens"] = 0
    orders = []
    rec = lifecycle(orders, cases=no_reuse)
    mode = rec["modes"][0]
    check(mode.get("mode_ok") is False and any("not > 0" in f for f in mode["mode_failures"]),
          "no reuse in the runner's own results: the lane FAILS (no log-based fallback claim)")

    orders = []
    rec = lifecycle(orders, log_text=good_log().replace("dflash: sampled request uses target-only decoding\n", ""))
    mode = rec["modes"][0]
    check(mode.get("mode_ok") is False and any("12499" in f for f in mode["mode_failures"]),
          "no sampled fallback line in the session copy: the lane FAILS (the HTTP 200 proves nothing)")

    orders = []
    rec = lifecycle(orders, log_text="dflash: sampled request uses target-only decoding\n" + good_log())
    mode = rec["modes"][0]
    check(mode.get("mode_ok") is False and any("during the greedy" in f for f in mode["mode_failures"]),
          "a sampled fallback line positioned during greedy FAILS the lane")

    orders = []
    rec = lifecycle(orders, start_ok=False)
    steps = [o for o in orders if not o.startswith("note:")]
    check(steps == ["stop_prod", "start_mode", "stop_mode"],
          f"a refused startup sends NO integration request and still stops its family ({steps})")
    check(rec["modes"][0].get("mode_ok") is False, "a refused startup is a mode failure")

    orders = []
    rec = lifecycle(orders, integration_raises=True)
    steps = [o for o in orders if not o.startswith("note:")]
    check("run_integration" in steps and steps[-1] == "stop_mode" and "mode_error" in rec["modes"][0],
          f"an interrupted integration is recorded and the family is still stopped ({steps})")

    orders = []
    rec = lifecycle(orders, stop_ok=False)
    steps = [o for o in orders if not o.startswith("note:")]
    check(steps == ["stop_prod"], f"a refused verified stop starts NOTHING ({steps})")

    orders = []
    rec = lifecycle(orders, ev_ok=False)
    mode = rec["modes"][0]
    check(mode.get("mode_ok") is False and any("unreadable" in f for f in mode["mode_failures"]),
          "unreadable engine evidence is a mode failure, never a pass")
    check(rec.get("all_modes_ok") is not True, "a failed lane never sets all_modes_ok")


def test_restore_gate():
    calls: list[str] = []
    keep = R.W.restore_and_verify
    R.W.restore_and_verify = lambda snap, record, dl: (calls.append("restore"), {"restored": True})[1]
    try:
        surv = {"exe_matches": [123], "recorded_family_alive": [], "port_8019_listener": None}
        r = R.restore_gate({}, {"window": "W0"}, surv, 0.0)
        check(r.get("restored") is False and "REFUSED" in r.get("refused", "") and calls == [],
              "a surviving prototype exe REFUSES the restore (no duplicate load, no launch)")
        surv = {"exe_matches": [], "recorded_family_alive": [45], "port_8019_listener": None}
        r = R.restore_gate({}, {"window": "W0"}, surv, 0.0)
        check(r.get("restored") is False and calls == [], "a recorded family pid REFUSES the restore")
        surv = {"exe_matches": [], "recorded_family_alive": [], "port_8019_listener": 999}
        r = R.restore_gate({}, {"window": "W0"}, surv, 0.0)
        check(r.get("restored") is False and calls == [], "a listener on 8019 REFUSES the restore")
        surv = {"exe_matches": [], "recorded_family_alive": [], "port_8019_listener": None}
        r = R.restore_gate({}, {"window": "W0"}, surv, 0.0)
        check(r.get("restored") is True and calls == ["restore"],
              "with every owned process gone the SAME window.py restore path runs")
    finally:
        R.W.restore_and_verify = keep
    check(R.W.restore_and_verify is keep, "the restore fixture is restored")


def test_constants():
    check(R.MODE == "dflash_w3" and R.WIDTH == 3, "constants: ONE mode, width 3")
    check(set(R.ALLOW) == {"--expert-cache", "--prefill", "--no-prefill-borrow", "--adapt-swaps",
                           "--prompt-cache", "--conversation-cache-mib", "--conversation-cache-slots"},
          "constants: the strict allowlist has exactly the seven controls (no hidden knobs)")
    check(R.ALLOW["--expert-cache"][0] == "2000" and R.ALLOW["--prefill"][0] == "512"
          and R.ALLOW["--adapt-swaps"][0] == "0" and R.ALLOW["--prompt-cache"][0] == "4"
          and R.ALLOW["--conversation-cache-mib"][0] == "2048"
          and R.ALLOW["--conversation-cache-slots"][0] == "3"
          and R.ALLOW["--no-prefill-borrow"][0] is None,
          "constants: expert-cache 2000, prefill 512, adapt-swaps 0, prompt-cache 4, "
          "conversation-cache 2048/3 slots, borrowing off")
    check(R.VALUELESS == {"--no-prefill-borrow"}, "constants: the valueless arity set")
    check(R.INTEGRATION.endswith("tools/test_inco_dflash2_integration.py"),
          "constants: the EXISTING integration runner is the executed tool (no parallel framework)")
    gsrc = pathlib.Path(f"{mw.CLONE}/src/program/generate.cpp").read_text()
    check(R.SAMPLED_LINE in gsrc and R.CLI_SAMPLED_LINE in gsrc and R.SAMPLED_LINE != R.CLI_SAMPLED_LINE
          and R.SAMPLED_LINE not in R.CLI_SAMPLED_LINE and R.CLI_SAMPLED_LINE not in R.SAMPLED_LINE,
          "constants: the SERVE sampled text (generate.cpp:12499) and the CLI text (2831) are the two "
          "distinct source strings - each is found in the source, neither is a substring of the other")
    check("prompt_tokens_details" in R.usage_proof([])["citations"]["cached_tokens"],
          "constants: the reuse proof cites the usage field the server emits (server.py:5335)")
    check("998" in R.log_proof("")["routing"] and "15775" in R.log_proof("")["routing"],
          "constants: the log proof cites the stderr->log routing (15775/12499 -> server.py:998)")
    check(R.FALLBACK_FAIL == "silent fallback to target-only in the engine log = MODE FAILURE",
          "constants: the exemption matches mw.evaluate's exact failure text (no string drift)")


# ------------------------------------------------------------------ 7. own-engine phases (CPU-only mocks)
import os
import re
import subprocess as _sp
import types

MW_KEYS_W = ("stop_prod", "same_proc", "start_ticks", "children", "exe_of", "cmdline",
             "listener_pid", "restore_and_verify")
MW_KEYS_R = ("_term", "_proto_pids", "_copy_session", "owned_run", "subprocess", "time",
             "_cwd_of", "_env_proof")
PHASE_MOCK_SUBPROCESS = {"Popen", "TimeoutExpired", "DEVNULL"}     # the phase path's whole surface
# window.py's snapshot carries the LIVE engine's own device/library flags in memory. The phase CHILD
# must receive them (the engine counts the VISIBLE GPUs when it validates --layer-split), and the
# parent's os.environ must never be touched. PATH/HOME are here to prove the filter: they are NOT
# device keys, and a key the parent process already has is INHERITED, not injected from the snapshot.
PHASE_SNAP = {"verified": True,
              "_env_full": {"HSA_OVERRIDE_GFX_VERSION": "9.0.6", "HSA_ENABLE_SDMA": "0",
                            "HIP_VISIBLE_DEVICES": "0,1", "ROCM_PATH": "/synth/rocm",
                            "LD_LIBRARY_PATH": "/synth/rocm/lib", "STRATA_DECODE_TIMING": "1",
                            "STRATA_API_KEY": "synthetic-secret-value", "PATH": "/bin/not/a/device/key",
                            "HOME": "/home/not/a/device/key"}}
ENGINE_ARGV = (f"{mw.PROTO_BINARY} --serve --gpu 0,1 --dflash /synth/dflash --max-context 262144 "
               f"--spec 4 --dflash-block 3 --adapt-swaps 0 --layer-split 32")
ENGINE_ARGV_NOSPLIT = ENGINE_ARGV.replace(" --layer-split 32", "")
TOOL_LINES = {          # the lines the tools really print (cancel_park_test.py:71-90)
    "cancel-park": ["A: prompt 900 tokens, 58 generated, reused 0",
                    "B: prompt 40 tokens, reused 0 (A is parked now)",
                    "A2 (cancelled): restored/reused at RESUME 900, finish cancel, STOP sent True",
                    "A3: prompt 960 tokens, reused 958 (A + its answer = 961; need >= 958)",
                    "PASS"],
    "rewind": ["DFlash live continuation, checkpoint rewind and conversation parking: PASS"],
}


def phase_case(phase: str, behavior: str = "ok", live: set[str] | None = None,
               proto_after: tuple[int, ...] = (), listener: int | None = None,
               run_window: bool = False, stop_ok: bool = True,
               stages: list[list[int]] | None = None,
               engine_argv: str = ENGINE_ARGV,
               cache_markers: tuple[str, ...] = ("parked", "restored")) -> tuple[dict, list, list, list, dict]:
    """Drive ONE own-engine phase with NO process, NO /proc, NO GPU, NO sleep, NO server and NO
    write outside the fixture temp dir. 'stages' is the tool's DIRECT-CHILD lineage at each
    observation - a real engine appears only AFTER the launch and dflash_cache spawns three in
    sequence, so the scan must catch them while they live; 'live' marks the pids that are alive;
    'behavior' drives the tool's exit ("ok" / "staged" / "timeout" / "interrupt").
    Returns (record, notes, signals, calls, child_env): 'signals' are the pids the controller
    signalled, 'calls' the lifecycle calls it made (a 'start_mode' entry is a bug), 'child_env' the
    environment the child process was actually given."""
    notes: list[str] = []
    signals: list[int] = []
    calls: list[str] = []
    child: dict[str, str] = {}
    live = live if live is not None else set()
    stages = stages if stages is not None else [[7001]]
    stage_i = [0]
    ticks = {"4242": 5000, "7001": 5001, "7002": 5002, "7003": 5003, "9999": 9990}
    cfg_path = TMP / "phase-derived-config.json"
    d, _ov, _p = R.derive("W0", base_clone())
    R.write_derived_config(cfg_path, d)

    keep = {k: getattr(R.W, k) for k in MW_KEYS_W}
    keep.update({k: getattr(R, k) for k in MW_KEYS_R})
    keep.update({k: getattr(R.mw, k) for k in ("start_mode", "log_start")})
    keep_dirs = (R.W.OPS, R.W.LOGS)
    t = [0.0]

    def kids(pid: int) -> list[int]:                       # the lineage the controller scans
        i = min(stage_i[0], len(stages) - 1)
        stage_i[0] += 1
        return list(stages[i])

    class FakeProc:
        def __init__(self, argv, **kw):
            self.pid, self.n = 4242, 0
            fh = kw.get("stdout")                                  # the tool log opened in 'ab'
            if fh:
                fh.write(("\n".join(TOOL_LINES[phase]) + "\n").encode())
                fh.flush()

        def wait(self, timeout=None):
            self.n += 1
            if behavior == "timeout":
                raise _sp.TimeoutExpired(cmd="tool", timeout=timeout)
            if behavior == "interrupt":
                raise KeyboardInterrupt
            if behavior == "staged" and self.n < len(stages):
                raise _sp.TimeoutExpired(cmd="tool", timeout=timeout)      # tool still running
            return 0

    class FakePopen:
        def __init__(self, argv, **kw):
            child.update({k: str(v) for k, v in (kw.get("env") or {}).items()})
            calls.append("popen")
            self.p = FakeProc(argv, **kw)

        def __getattr__(self, name):
            return getattr(self.p, name)

    def stop_yes(s, r, n):
        notes.append("stop_prod")
        r["stop_sent"] = True
        return True

    def stop_no(s, r, n):
        notes.append("stop_prod_refused")
        return False

    R.W.OPS, R.W.LOGS = TMP, TMP                                    # no writes into the real ops/logs
    R.W.stop_prod = stop_yes if stop_ok else stop_no                # a plain def: a lambda ternary
    #                                             returns a lambda, which is TRUTHY (parent run #5)
    R.W.same_proc = lambda pid, tk: str(pid) in live and ticks.get(str(pid)) == tk
    R.W.start_ticks = lambda pid: ticks.get(str(pid), 1)
    R.W.children = kids
    R.W.exe_of = lambda pid: mw.PROTO_BINARY if str(pid) in ("7001", "7002", "7003") else "/synth/python"
    R.W.cmdline = lambda pid: engine_argv if str(pid) in ("7001", "7002", "7003") \
        else f"{mw.VENV_PY} {R.PHASES[phase]['tool']} cfg"
    R.W.listener_pid = lambda port=8019: listener
    R.W.restore_and_verify = lambda s, r, dl: (calls.append("restore"), {"restored": True})[1]
    R.mw.start_mode = lambda *a, **k: calls.append("start_mode") or True
    R.mw.log_start = lambda cfg: {"path": cfg["log"], "bytes": 0, "inode": None, "absent": True}
    R._term = lambda pid, tk: signals.append(int(pid))
    R._proto_pids = lambda: list(proto_after)
    R._copy_session = lambda src, st, dst: {
        "provenance": "mocked", "per_window_copy": str(TMP / "sess.log"),
        "markers": {("conversation cache: " + m): (m in cache_markers) for m in ("parked", "restored")}
        | {"skip parking": "skip parking" in cache_markers, "RESUME": True}}
    R._cwd_of = lambda pid: mw.CLONE
    R._env_proof = lambda pid: {"HIP_VISIBLE_DEVICES": "0,1", "HSA_OVERRIDE_GFX_VERSION": "9.0.6"}
    R.subprocess = types.SimpleNamespace(Popen=FakePopen, TimeoutExpired=_sp.TimeoutExpired,
                                         DEVNULL=_sp.DEVNULL)
    assert set(PHASE_MOCK_SUBPROCESS) <= set(vars(R.subprocess)), \
        "the mock namespace must cover every subprocess attribute the phase path uses"
    R.time = types.SimpleNamespace(time=lambda: (t.__setitem__(0, t[0] + 1), t[0])[1],
                                   sleep=lambda s: None, strftime=lambda fmt: "2026-10-23T00:00:00+0000")
    if run_window:
        R.owned_run = lambda ph, cfg, win, rec, n, snap=None: (calls.append("owned_run"), True)[1]
    try:
        record = {"window": "W0-phase", "stop_sent": False, "modes": []}
        if run_window:
            R.run_phase_window(phase, PHASE_SNAP, record, d, cfg_path, lambda m: notes.append(m[:70]))
        else:
            record["phase_ok"] = R.owned_run(phase, cfg_path, "W0-phase", record,
                                             lambda m: notes.append(m[:70]), PHASE_SNAP)
    finally:
        for k, v in keep.items():
            mod = R.W if k in MW_KEYS_W else (R.mw if k in ("start_mode", "log_start") else R)
            setattr(mod, k, v)
        R.W.OPS, R.W.LOGS = keep_dirs
    return record, notes, signals, calls, child


def flag_value(args: list[str], flag: str) -> str | None:
    """The value that follows a flag in an engine argv (None for a valueless flag) - a scan, so a
    valueless flag in the list can never misalign a zip."""
    i = args.index(flag)
    v = args[i + 1] if i + 1 < len(args) else ""
    return None if v.startswith("--") else v


def test_phase_dispatch():
    check(set(R.PHASES) == {"cancel-park", "rewind"},
          "--phase offers exactly cancel-park and rewind (integration is the default, not a PHASES entry)")
    check(R.phase_argv("cancel-park", pathlib.Path("/cfg.json"), pathlib.Path("/out"))
          == [mw.VENV_PY, f"{mw.CLONE}/tools/cancel_park_test.py", "/cfg.json"],
          "cancel-park: venv python + the CLONE's tool + the derived config as the single positional arg "
          "(the tool reads it at sys.argv[1])")
    check(R.phase_argv("rewind", pathlib.Path("/cfg.json"), pathlib.Path("/out"))
          == [mw.VENV_PY, f"{mw.CLONE}/tools/dflash_cache.py", "--config", "/cfg.json",
              "--engine", mw.PROTO_BINARY, "--output", "/out"],
          "rewind: venv python + the CLONE's tool + --config / --engine (the prototype binary) / --output")
    check(R.PHASE_ENV == {"STRATA_REPO": mw.CLONE, "PYTHONPATH": f"{mw.CLONE}/tools",
                          "STRATA_KEEP_LEFTOVERS": "1"},
          "phase env: STRATA_REPO and PYTHONPATH point at the PROTOTYPE tree's tools, and the tool's "
          "leftover/orphan engine signalling (serve/server.py:103) is switched off")
    check("/mnt/user/Strata" not in R.PHASE_ENV["PYTHONPATH"]
          and "/mnt/user/Strata" not in R.PHASE_ENV["STRATA_REPO"],
          "the prototype imports never resolve into the production install")
    f = R.PHASES["rewind"]["forced"]
    check(f["--max-context"].startswith("4096") and f["--spec"].startswith("2")
          and f["--dflash-block"].startswith("1") and "--spec-split" in f
          and f["--conversation-cache-mib"].startswith("256") and f["--conversation-cache-slots"].startswith("4"),
          "the rewind phase records the tool's FORCED 4096 context / spec 2 / block 1 / fixed caches / "
          "--spec-split verbatim (a tool run is never labeled as 262144 width-3 coverage)")
    check("4096" in R.PHASES["rewind"]["shape"] and "262144 width-3 integration lane" in R.PHASES["rewind"]["shape"]
          and "disk sessions UNSUPPORTED" in R.PHASES["rewind"]["shape"],
          "the rewind shape says 4096, says it is narrower than the 262144 width-3 integration lane, and "
          "says disk sessions are unsupported")
    check("--layer-split" in R.PHASES["cancel-park"]["shape"] and "WITH the real two-GPU" in R.PHASES["cancel-park"]["shape"]
          and "single-GPU 262144 run is NOT an acceptable substitute" in R.PHASES["cancel-park"]["shape"],
          "the cancel-park shape says the REAL two-GPU --layer-split runs and a single-GPU 262144 run is "
          "NOT an acceptable substitute")
    check("WITH the real two-GPU --layer-split kept" in R.PHASES["rewind"]["shape"]
          and "NARROWER" in R.PHASES["rewind"]["shape"]
          and "disk sessions UNSUPPORTED" in R.PHASES["rewind"]["shape"],
          "the rewind shape says its 4096/spec2/block1 coverage is explicitly NARROWER and keeps the "
          "two-GPU split, and says disk sessions are unsupported")
    check("--layer-split" in R.PHASES["rewind"]["forced"],
          "the rewind table records --layer-split as NOT in the tool's forced list, so replace() cannot "
          "remove the split the tool adds")
    m = R.PHASES["cancel-park"]["markers"]
    check("PASS" in m and "finish cancel" in m and "STOP sent True" in m
          and any(ln.startswith("A2 (cancelled):") for ln in m) and any(ln.startswith("A3: prompt") for ln in m),
          "cancel-park requires the tool's SCENARIO lines (the A2 cancel result, the STOP, the A3 reuse) "
          "and not the generic PASS alone")
    check(len(R.PHASES["rewind"]["markers"]) == 1 and "PASS" in R.PHASES["rewind"]["markers"][0],
          "the rewind phase names the single PASS sentence its own tool prints")
    check(R.PHASES["cancel-park"]["timeout"] == 2400 and R.PHASES["rewind"]["timeout"] == 5400,
          "the tool timeouts are recorded and a timeout is exit 124, never a success")
    check(R.phase_out_path("rewind", "W9-unseen").parent == R.W.OPS
          and not R.phase_out_path("rewind", "W9-unseen").exists(),
          "the dry run computes the phase output path WITHOUT creating it (a plan writes nothing)")
    d, _o, _p = R.derive("W0", base_clone())
    check("--dflash" in d["args"] and "--mtp" not in d["args"],
          "the derived config satisfies dflash_cache.py's own guard (--dflash present, --mtp absent)")
    check(flag_value(d["args"], "--conversation-cache-slots") == "3"
          and flag_value(d["args"], "--conversation-cache-mib") == "2048"
          and flag_value(d["args"], "--prompt-cache") == "4"
          and flag_value(d["args"], "--adapt-swaps") == "0"
          and flag_value(d["args"], "--max-context") == "262144"
          and flag_value(d["args"], "--expert-cache") == "2000"
          and flag_value(d["args"], "--prefill") == "512"
          and "--no-prefill-borrow" in d["args"],
          "the profile the tools receive carries the controlled caches 4/2048/3, expert-cache 2000, "
          "prefill 512, borrowing off, adaptation OFF and context 262144 (the cancel-park engine shape, verbatim)")


def test_phase_window_guards():
    rec, notes, signals, calls, _child = phase_case("cancel-park", run_window=True)
    check(calls == ["owned_run"] and "start_mode" not in calls,
          f"an own-engine phase makes exactly ONE tool run and NO mw.start_mode - no prototype server "
          f"is started there ({calls})")
    check("stop_prod" in notes and signals == [],
          "the phase runs inside a verified original-down window and signals nothing when it is clean")
    rec, notes, signals, calls, _c = phase_case("rewind", listener=555, run_window=True)
    check(calls == [] and "REFUSED" in rec.get("break_reason", "") and "8019" in rec["break_reason"]
          and signals == [],
          f"a listener on 8019 REFUSES the phase, fail closed, nothing signalled ({rec.get('break_reason')})")
    rec, notes, signals, calls, _c = phase_case("rewind", proto_after=(999,), run_window=True)
    check(calls == [] and "REFUSED" in rec.get("break_reason", "") and signals == [],
          f"a live prototype-exe process REFUSES the phase (no double load), nothing signalled "
          f"({rec.get('break_reason')})")
    rec, notes, signals, calls, _c = phase_case("cancel-park", run_window=True, stop_ok=False)
    check(calls == [] and signals == [] and notes == ["stop_prod_refused"],
          f"a refused verified production stop starts NOTHING in a phase window (notes {notes})")


def test_owned_run_ownership():
    rec, notes, signals, calls, _child = phase_case("cancel-park", "ok")
    ev = rec["phase_run"]
    check(ev["exit"] == 0 and ev["markers_found"] == ev["markers_required"] and ev["phase_ok"] is True,
          f"a clean tool run needs exit 0 AND every marker of the tool's own output to be phase_ok "
          f"({ev['exit']}, {ev['markers_found']})")
    check(signals == [] and ev["survivors_after"] == {} and rec["survivors"]["recorded_family_alive"] == []
          and rec["survivors"]["exe_matches"] == [] and rec["survivors"]["port_8019_listener"] is None,
          "a clean phase signals nothing and records no owned survivor, no prototype exe, no 8019 listener")
    check("start_mode" not in calls and calls.count("popen") == 1,
          f"ONE tool subprocess, NO prototype server ({calls})")
    check(ev["engine_log"] == json.loads((TMP / "phase-derived-config.json").read_text())["log"],
          "the engine log recorded is the derived config's unique per-window log")
    check(ev["engine_log_start"]["absent"] is True,
          "the engine-log offset is pinned by mw.log_start BEFORE the launch (append-mode history is never copied)")
    check(ev["env_extra"]["STRATA_REPO"] == mw.CLONE and ev["env_extra"]["STRATA_KEEP_LEFTOVERS"] == "1",
          "the recorded env carries the prototype import roots and the disabled leftover-engine signalling")
    check(ev["shape"] == R.PHASES["cancel-park"]["shape"] and ev["forced_overrides"] == R.PHASES["cancel-park"]["forced"],
          "the tool shape and the tool's forced overrides are recorded verbatim in the evidence")
    check(ev["engines_observed"] == 1 and ev["engines"][0]["exe"] == mw.PROTO_BINARY
          and "--layer-split" in ev["engines"][0]["argv"] and ev["engines"][0]["cwd"] == mw.CLONE
          and ev["engines"][0]["env_proof"].get("HIP_VISIBLE_DEVICES") == "0,1",
          "the observed engine is recorded with its exe, full argv, cwd and device/library env proof")

    rec, notes, signals, calls, _c = phase_case("rewind", "timeout", live={"7001"})
    ev = rec["phase_run"]
    check(ev["exit"] == 124, "TOOL_TIMEOUT is recorded as exit 124, never as a success")
    check(signals[:1] == [4242] and 7001 in signals,
          f"the timeout signals OUR tool pid first, then the engine pid pinned by pid+ticks, and nothing "
          f"else ({signals})")
    check(ev["survivors_after"] == {"7001": 5001} and rec["survivors"]["recorded_family_alive"] == [7001],
          "an engine that outlives the timeout is recorded as OUR owned survivor with its start ticks")
    check(any("TOOL_TIMEOUT" in n for n in notes) and ev["phase_ok"] is False,
          "the timeout is noted and the phase is not ok")

    rec, notes, signals, calls, _c = phase_case("cancel-park", "interrupt", live={"7001"})
    check("KeyboardInterrupt" in rec.get("phase_error", ""), "an interruption inside the phase is recorded")
    check(signals[:2] == [4242, 7001] and set(signals) == {4242, 7001},
          f"the interruption signals ONLY our tool pid and its observed engine pid, never anything else "
          f"({signals})")
    check(rec["phase_run"]["phase_ok"] is False, "an interrupted phase is never ok")

    rec, notes, signals, calls, _c = phase_case("rewind", "ok", live={"7001"})
    ev = rec["phase_run"]
    check(ev["exit"] == 0 and signals == [7001] and ev["survivors_after"] == {"7001": 5001}
          and ev["phase_ok"] is False,
          f"exit 0 with OUR engine still alive: the pinned pid+ticks is signalled and the phase is NOT ok "
          f"(exit {ev['exit']}, signals {signals})")
    check(all(e["exe"] == mw.PROTO_BINARY for e in ev["engines"]) and bool(ev["engines"]),
          "every observed child is recorded with its exe and argv (the tool's own engine, ours to clean)")


def test_phase_restore_refusal():
    rec, _n, _s, _c, _ch = phase_case("rewind", "timeout", live={"7001"})
    keep = R.W.restore_and_verify
    calls: list[str] = []
    R.W.restore_and_verify = lambda s, r, dl: (calls.append("restore"), {"restored": True})[1]
    try:
        r = R.restore_gate({}, rec, rec["survivors"], 0.0)
        check(r.get("restored") is False and calls == [],
              "the original restore is REFUSED while a phase engine pid we own is alive")
    finally:
        R.W.restore_and_verify = keep
    rec2, _n2, _s2, _c2, _ch2 = phase_case("cancel-park", "ok")
    calls2: list[str] = []
    R.W.restore_and_verify = lambda s, r, dl: (calls2.append("restore"), {"restored": True})[1]
    try:
        r = R.restore_gate({}, rec2, rec2["survivors"], 0.0)
        check(r.get("restored") is True and calls2 == ["restore"],
              "with every owned pid gone the SAME window.py restore path runs for a phase window too")
    finally:
        R.W.restore_and_verify = keep
    check(R.W.restore_and_verify is keep, "the phase restore fixture is restored")
    # a same-exe process we NEVER observed is a restore refusal, never an ownership claim
    rec3, _n3, signals3, _c3, _ch3 = phase_case("rewind", "ok", proto_after=(9999,))
    calls3: list[str] = []
    R.W.restore_and_verify = lambda s, r, dl: (calls3.append("restore"), {"restored": True})[1]
    try:
        r = R.restore_gate({}, rec3, rec3["survivors"], 0.0)
        check(r.get("restored") is False and calls3 == [] and rec3["survivors"]["exe_matches"] == [9999]
              and 9999 not in signals3,
              "an unrelated same-exe process REFUSES the restore and is never signalled - the global "
              "scan never makes a pid ours")
    finally:
        R.W.restore_and_verify = keep


def test_phase_live_lineage():
    """The engine appears AFTER the launch and the rewind tool spawns THREE in sequence, so the
    controller must catch each one while it lives, keep its metadata, and signal only ours."""
    seq = [[], [7001], [7001], [7002], [7003]]
    rec, notes, signals, calls, _c = phase_case("rewind", "staged", stages=seq)
    ev = rec["phase_run"]
    pids = [e["pid"] for e in ev["engines"]]
    check(pids == [7001, 7002, 7003],
          f"all THREE sequential engines are pinned by the live scans, in the order they appeared ({pids})")
    check(all(e["ticks"] > 0 and e["exe"] == mw.PROTO_BINARY and e["argv"] and e["cwd"] == mw.CLONE
              and e["env_proof"].get("HIP_VISIBLE_DEVICES") == "0,1" for e in ev["engines"]),
          "each engine is captured WHILE ALIVE: pid + start ticks, exe, full argv, cwd, env proof")
    check(ev["phase_ok"] is True and signals == [], "a clean staged run passes and signals nothing")
    check(sum(1 for n in notes if "observed OUR child pid" in n) == 3,
          "every engine is noted at its first observation (the proof is a live scan, not a post-exit guess)")
    rec, notes, signals, calls, _c = phase_case("cancel-park", "staged", stages=[[]], live={"7001"})
    ev = rec["phase_run"]
    check(ev["engines_observed"] == 0 and ev["phase_ok"] is False and signals == [],
          "a child no live scan ever saw is NOT owned - no ownership and no signal from a post-exit guess")


def test_phase_split_gate():
    rec, _n, _s, _c, _ch = phase_case("rewind", "ok", engine_argv=ENGINE_ARGV_NOSPLIT)
    ev = rec["phase_run"]
    check(ev["split_wanted"] == "32" and ev["split_observed"] == [] and ev["phase_ok"] is False
          and any("--layer-split 32" in w for w in ev["phase_ok_why"]),
          f"no success from a PASS substring with an engine argv that has no split - the observed "
          f"--layer-split 32 is required ({ev['phase_ok_why']})")
    rec, _n, _s, _c, _ch = phase_case("cancel-park", "ok",
                                      engine_argv=ENGINE_ARGV.replace("--layer-split 32", "--layer-split 18"))
    ev = rec["phase_run"]
    check(ev["phase_ok"] is False and ev["split_observed"] == []
          and any("'18'" in w for w in ev["phase_ok_why"]),
          f"an engine with the WRONG split width is not the split this profile requires - no pass "
          f"({ev['phase_ok_why']})")
    d, _o, _p = R.derive("W0", base_clone())
    check(R._want_split(d) == "32" and R._gpu_count(d) == 2,
          "the derived config's structured layer_split '32' on two GPUs is exactly what a phase must prove")
    check(R._want_split({"gpu": [0], "layer_split": "32"}) is None,
          "a config naming ONE GPU has no split to prove, so no phase can claim a two-GPU run")
    check(R._argv_split(ENGINE_ARGV) == "32" and R._argv_split(ENGINE_ARGV_NOSPLIT) is None,
          "the argv scan reads the engine's actual --layer-split value")


def test_phase_child_env():
    parent_env = dict(os.environ)                      # what the parent process ALREADY has
    rec, _n, _s, _c, child = phase_case("cancel-park", "ok")
    injected = {k: v for k, v in child.items() if parent_env.get(k) != v}   # added OR overridden
    want = {k: v for k, v in PHASE_SNAP["_env_full"].items() if R._device_key(k)}
    check(child.get("HIP_VISIBLE_DEVICES") == "0,1" and child.get("HSA_OVERRIDE_GFX_VERSION") == "9.0.6"
          and child.get("HSA_ENABLE_SDMA") == "0" and child.get("ROCM_PATH") == "/synth/rocm"
          and child.get("LD_LIBRARY_PATH") == "/synth/rocm/lib" and child.get("STRATA_DECODE_TIMING") == "1",
          "the snapshot's LIVE device/library flags reach the CHILD - the engine counts the VISIBLE "
          "GPUs when it validates --layer-split and needs HSA_ENABLE_SDMA / ROCM_PATH, so this is a "
          "functional requirement, not hygiene")
    check(set(injected) <= set(want) | set(R.PHASE_ENV) and set(R.PHASE_ENV) <= set(injected),
          f"every key the phase ADDED or OVERRODE is a filtered snapshot device key or a phase key, "
          f"and the phase keys are always there ({sorted(injected)})")
    check(all(child.get(k) == v for k, v in want.items()),
          "every filtered snapshot device key reaches the child WITH the snapshot's value (a device "
          "key the parent already carries with the same value is inherited, so it is not 'injected')")
    check("PATH" not in injected and "HOME" not in injected
          and child.get("PATH") == parent_env.get("PATH") and child.get("HOME") == parent_env.get("HOME"),
          "PATH/HOME are INHERITED from the parent process, never injected and never overridden by the "
          "snapshot's synthetic values - the earlier check confused the two sources (parent run #6, line 274)")
    check(child.get("STRATA_REPO") == mw.CLONE and child.get("STRATA_KEEP_LEFTOVERS") == "1",
          "the prototype import roots and the disabled leftover signalling reach the child")
    ev = rec["phase_run"]
    check(ev["parent_env_mutated"] is False and dict(os.environ) == parent_env,
          "the parent's os.environ is READ, never mutated (no key added, no value changed)")
    check(sorted(ev["child_device_flags"]) == sorted(want),
          f"the recorded child device flags are exactly the filtered ones ({ev['child_device_flags']})")
    check(R.DEVICE_KEYS == W.DEVICE_PREFIX + ("ROCM",)
          and R._device_key("HSA_ENABLE_SDMA") and R._device_key("HSA_OVERRIDE_GFX_VERSION")
          and R._device_key("HIP_VISIBLE_DEVICES") and R._device_key("CUDA_VISIBLE_DEVICES")
          and R._device_key("CUDA_DEVICE_ORDER") and R._device_key("LD_LIBRARY_PATH")
          and R._device_key("ROCM_PATH") and R._device_key("STRATA_DECODE_TIMING"),
          "the device filter is window.py's OWN prefix list (HSA_*, HIP_*, CUDA_*, LD_LIBRARY_PATH, "
          "STRATA_*) plus ROCM* - not a hand-picked five, so HSA_ENABLE_SDMA is included")
    check(not R._device_key("PATH") and not R._device_key("HOME") and not R._device_key("SHELL")
          and not R._device_key("USER"),
          "PATH/HOME/SHELL/USER are NOT device keys, so no unrelated snapshot entry leaks to the child")
    src = pathlib.Path(R.__file__).read_text()
    check("W.masked(W.env_captured(pid))" in src,
          "the persisted engine env proof reuses window.py's own capture AND its masking - no private "
          "/proc parser and no raw secret values")
    m = W.masked({"STRATA_API_KEY": "secret-value", "HSA_ENABLE_SDMA": "0", "MY_TOKEN": "x"})
    check(m["STRATA_API_KEY"] == "***" and m["MY_TOKEN"] == "***" and m["HSA_ENABLE_SDMA"] == "0",
          "window.py's mask turns KEY/TOKEN/SECRET/PASSWORD values into '***' and keeps device values "
          "(the persisted _env_proof uses it, so a STRATA_API_KEY-like value is never written raw)")
    check("PROOF_KEYS" not in src,
          "the old five-name PROOF_KEYS list is gone (it omitted HSA_ENABLE_SDMA)")


def test_phase_copy_session():
    """The session copy's provenance: pinned offset, inode check, SHRINK refusal, owner-only mode."""
    src = TMP / "copy-session-log.log"
    src.write_bytes(b"a" * 400 + b"prompt 1 tokens = 0 reused\n")
    st = src.stat()
    dst = TMP / "copy-out.log"
    out = R._copy_session(str(src), {"bytes": 400, "inode": st.st_ino}, dst)
    check(out.get("session_bytes") == len("prompt 1 tokens = 0 reused\n")
          and out.get("start_bytes") == 400 and out.get("end_bytes") == st.st_size
          and "provenance" not in out,
          f"the copy carries ONLY the bytes after the pinned offset ({out})")
    check(dst.stat().st_mode & 0o777 == 0o600 and out.get("copy_mode_is_0600") is True,
          f"the copied evidence is written 0600, owner-only ({out.get('copy_mode')})")
    check("conversation cache: parked" in out.get("markers", {}),
          "the copy records the parking markers it looked for - a plain dict, never a claim")
    out = R._copy_session(str(src), {"bytes": 9000, "inode": st.st_ino}, TMP / "copy-shrink.log")
    check("SHRANK" in out.get("provenance", "") and not (TMP / "copy-shrink.log").exists(),
          f"a log that shrank below the pinned offset (rotated/truncated) REFUSES the copy ({out})")
    out = R._copy_session(str(src), {"bytes": 0, "inode": st.st_ino + 7}, TMP / "copy-inode.log")
    check("inode changed" in out.get("provenance", "") and not (TMP / "copy-inode.log").exists(),
          f"a log whose inode changed REFUSES the copy ({out})")
    out = R._copy_session(str(TMP / "copy-missing.log"), {"bytes": 0, "inode": None}, TMP / "copy-out2.log")
    check("copy failed" in out.get("provenance", ""),
          "an unreadable source returns a provenance failure, never an exception")


def test_phase_split_source():
    """The split comes from the PROTOTYPE's own rule, applied in the tools, standalone behavior kept."""
    cp = pathlib.Path(f"{mw.CLONE}/tools/cancel_park_test.py").read_text()
    dc = pathlib.Path(f"{mw.CLONE}/tools/dflash_cache.py").read_text()
    srv = pathlib.Path(f"{mw.CLONE}/serve/server.py").read_text()
    gen = pathlib.Path(f"{mw.CLONE}/src/program/generate.cpp").read_text()
    check("from serve.server import StrataEngine, child_env, layer_split_of, layer_split_value" in cp
          and 'if layer_split_of(cfg) and "--layer-split" not in args:' in cp
          and 'args += ["--layer-split", layer_split_value(cfg)]' in cp,
          "cancel_park_test.py puts the config's structured split into the engine argv with the server's "
          "own functions (serve/server.py:2871), guarded so it never doubles")
    check("from serve.server import layer_split_of, layer_split_value" in dc
          and 'if layer_split_of(cfg) and "--layer-split" not in args:' in dc
          and dc.index('args += ["--layer-split", layer_split_value(cfg)]') < dc.index('args.append("--spec-split")'),
          "dflash_cache.py does the same, BEFORE its --spec-split append (the split VERIFY window, "
          "generate.cpp:710 - a different option from --layer-split, source-verified independent)")
    check('args += ["--layer-split", layer_split_value(cfg)]' in srv,
          "the tools use the SAME expression the server path uses (no re-invented rule)")
    check('a == "--layer-split"' in gen
          and "o.serve && (split_auto || ints(o.layer_split, split_at))" in gen,
          "the engine parses --layer-split and requires --serve (both tools pass it) - read from source, "
          "not assumed")
    forced = dc.split("for key, value in (")[1].split("replace(args, key, value)")[0]
    check("--layer-split" not in forced,
          "--layer-split is NOT in dflash_cache's forced list, so its replace() loop cannot delete the split")
    check("--peer-device" not in " ".join(R.derive("W0", base_clone())[0]["args"]),
          "the profile carries no --peer-device (the engine refuses it beside --layer-split, generate.cpp:3301)")


def test_phase_static_surface():
    src = pathlib.Path(R.__file__).read_text()
    used = sorted(set(re.findall(r"\bsubprocess\.([A-Za-z_][A-Za-z0-9_]*)", src)))
    check(set(used) <= {"DEVNULL", "Popen", "TimeoutExpired", "run"},
          f"the controller's whole subprocess surface is {used} - every attribute the mock must provide")
    phase_src = src[src.index("def owned_run("):src.index("def run_phase_window(")]
    phase_used = sorted(set(re.findall(r"\bsubprocess\.([A-Za-z_][A-Za-z0-9_]*)", phase_src)))
    check(set(phase_used) <= PHASE_MOCK_SUBPROCESS,
          f"the phase path touches only {phase_used}, all of them in the fixture's mock namespace "
          f"{sorted(PHASE_MOCK_SUBPROCESS)} (the parent's run #5 hit a missing DEVNULL)")
    check(src.count("subprocess.run") == 1 and "subprocess.run" not in phase_src
          and src.index("subprocess.run") > src.index("def run_integration"),
          "subprocess.run belongs to the integration lane's runner call ONLY - the phase path uses Popen")
    call = re.search(r"subprocess\.Popen\((.*?)\)\n", phase_src, re.S)
    kwnames = sorted(set(re.findall(r"(\w+)=", call.group(1))))
    check(kwnames == sorted(["cwd", "env", "stderr", "stdin", "stdout", "start_new_session"]),
          f"the Popen call passes these keywords ({kwnames}) - the fake takes them all through **kw")
    check("split_observed" in src and "_observe(" in src and "phase_ok_why" in src
          and "parent_env_mutated" in src,
          "the observed-split gate, the live scan, the recorded failure reasons and the env-mutation "
          "proof are all present in the controller")
    check("start_new_session=True" in phase_src and "threading" not in src,
          "the tool runs in its own process group and the controller has NO process-watch thread")


def test_phase_controls():
    check(set(R.PHASE_CONTROLS) == {"cancel-park", "rewind"} and R.PHASE_CONTROLS["rewind"] == {}
          and set(R.PHASE_CONTROLS["cancel-park"]) == {"--conversation-cache-min-tokens",
                                                        "--conversation-cache-park-gain"}
          and R.PHASE_CONTROLS["cancel-park"]["--conversation-cache-min-tokens"][0] == "1"
          and R.PHASE_CONTROLS["cancel-park"]["--conversation-cache-park-gain"][0] == "0",
          "the cancel-park phase has exactly the two gate controls min-tokens 1 / park-gain 0 (the same "
          "two controls dflash_cache.py forces for its own arms); rewind has none (its tool forces them)")
    check(R.PHASE_EVIDENCE["cancel-park"] == ("parked", "restored") and R.PHASE_EVIDENCE["rewind"] == (),
          "cancel-park demands the engine's own park AND restore events as evidence; rewind demands "
          "what its own tool asserts")
    d, _o, _p = R.derive("W0", base_clone())
    integ = TMP / "controls-integration.json"
    R.write_derived_config(integ, d)
    before = integ.read_text()
    rec_i: dict = {}
    R.apply_phase_controls(integ, "integration", rec_i)
    check(integ.read_text() == before and rec_i == {"phase_controls": {}},
          "the integration config is NEVER touched - no controls exist for it, so the passing "
          "integration profile is untouched")
    cp = TMP / "controls-cancel-park.json"
    R.write_derived_config(cp, d)
    rec: dict = {}
    R.apply_phase_controls(cp, "cancel-park", rec)
    cfg = json.loads(cp.read_text())
    check(R.flag_value(cfg["args"], "--conversation-cache-min-tokens") == "1"
          and R.flag_value(cfg["args"], "--conversation-cache-park-gain") == "0",
          "the phase config carries min-tokens 1 and park-gain 0")
    orig, new = list(d["args"]), list(cfg["args"])
    diff = [(i, a, b) for i, (a, b) in enumerate(zip(orig, new)) if a != b]
    check(len(orig) == len(new) and len(diff) == 2
          and all(orig[i - 1].startswith("--conversation-cache-") for i, a, b in diff),
          f"exactly the two control VALUES changed, every other arg stays in place ({diff})")
    check(cp.stat().st_mode & 0o777 == 0o600, "the phase config stays owner-only 0600")
    check(R.flag_value(cfg["args"], "--max-context") == "262144"
          and R.flag_value(cfg["args"], "--adapt-swaps") == "0"
          and R.flag_value(cfg["args"], "--prompt-cache") == "4"
          and R.flag_value(cfg["args"], "--conversation-cache-mib") == "2048"
          and R.flag_value(cfg["args"], "--conversation-cache-slots") == "3"
          and R.flag_value(cfg["args"], "--expert-cache") == "2000"
          and R.flag_value(cfg["args"], "--prefill") == "512",
          "the phase keeps context 262144, adaptation OFF, caches 4/2048/3, expert-cache 2000, "
          "prefill 512 - ONLY the two gate controls moved")
    check(cfg.get("gpu") == [0, 1] and str(cfg.get("layer_split")) == "32"
          and "--ple-gguf" in cfg["args"] and "--kv" in cfg["args"]
          and "--gpu" in cfg["args"] and R.flag_value(cfg["args"], "--gpu") == "0,1",
          "the dual-GPU layer-32 split, the target quant and PLE stay exactly the clone's - structured "
          "fields and the GPU pair untouched")
    check(rec["phase_controls"]["--conversation-cache-min-tokens"]["was"] == "2048"
          and "generate.cpp" in rec["phase_controls"]["--conversation-cache-min-tokens"]["source"]
          and "scope" in " ".join(rec).lower(),
          f"the recorded control says what it replaced and cites the source ({rec['phase_controls']})")
    missing = json.loads(json.dumps(d))
    missing["args"] = [a for a in missing["args"] if a != "--conversation-cache-park-gain"]
    mp = TMP / "controls-missing.json"
    R.write_derived_config(mp, missing)
    raised = False
    try:
        R.apply_phase_controls(mp, "cancel-park", {})
    except SystemExit:
        raised = True
    check(raised, "a control whose flag is absent REFUSES - a phase never invents a flag")
    prod = pathlib.Path(f"{mw.CLONE}/tools/dflash_cache.py").read_text()
    check('"--conversation-cache-min-tokens", "1"' in prod and '"--conversation-cache-park-gain", "0"' in prod,
          "the same two controls are exactly what dflash_cache.py already forces for its own arms "
          "(its :47-48) - no new idea, same test shape")
    gen = pathlib.Path(f"{mw.CLONE}/src/program/generate.cpp").read_text()
    check('a == "--conversation-cache-min-tokens"' in gen and 'a == "--conversation-cache-park-gain"' in gen
          and "conversations.wants((int64_t) live.size())" in gen
          and "o.conversation_cache_park_gain <= 0" in gen
          and "0 (the default) parks every turn" in gen,
          "source confirms the gates: the size gate is conversations.wants (9516-9526) and park-gain 0 "
          "parks every turn (12879-12887), so the two controls are SUFFICIENT - no engine change, no "
          "longer fixture needed")


def test_cancel_tool_source():
    cp = pathlib.Path(f"{mw.CLONE}/tools/cancel_park_test.py").read_text()
    printed = [ln for ln in cp.splitlines() if "print(" in ln]
    check(not any("(A is parked now)" in ln for ln in printed),
          "the tool never PRINTS the unproved 'A is parked now' claim the 101959 window exposed (the "
          "docstring may name it as the old false claim; the code must not print it)")
    check("PRECONDITION FAIL" in cp and cp.count("sys.exit(3)") == 2,
          "the tool stops with an explicit PRECONDITION FAIL and exit 3 - once when nothing was parked, "
          "once when nothing was restored")
    check('"conversation cache: " + word' in cp and 'cache_lines(engine_lines(), "parked")' in cp
          and 'cache_lines(engine_lines(), "restored")' in cp
          and 'cache_lines(engine_lines(), "skip parking")' in cp,
          "the preconditions read the engine's OWN lines (parked 9691, restored 13075, skip parking "
          "9524) - no invented state")
    check("LOG0 = Path(LOG).stat().st_size" in cp and "fh.seek(LOG0)" in cp,
          "the log offset is pinned BEFORE the engine starts, so only the bytes the engine appended are read")
    check('ok = fin2 == "cancel" and reused3 >= want' in cp and "want = len(A1) + len(outA) - 3" in cp
          and 'sys.exit(0 if ok else 1)' in cp,
          "the real cancellation and retained-answer reuse thresholds are unchanged - the PASS is not weakened")
    check("def stop_engine" in cp and "eng.proc.stdin.close()" in cp and "eng.proc.wait(60)" in cp
          and "eng.proc.kill()" in cp,
          "every early stop closes the engine exactly the way the tool always did (stdin.close, wait(60), else kill)")
    check("min-tokens 1" in cp and "park-gain 0" in cp and "skip parking (1829 tokens" in cp,
          "the tool's own docstring records the measured failure and names the cache controls a run "
          "config needs for this fixture")


def test_phase_evidence_gate():
    rec, _n, signals, _c, _ch = phase_case("cancel-park", "ok", cache_markers=("restored",))
    ev = rec["phase_run"]
    check(ev["phase_ok"] is False and ev["evidence_required"] == ["parked", "restored"]
          and ev["evidence_seen"] == ["restored"]
          and any("no parked and restored event" in w for w in ev["phase_ok_why"]),
          f"a PASS substring with no park event in the engine's own log is REFUSED - the tool's words "
          f"never become coverage ({ev['phase_ok_why']})")
    rec2, _n2, _s2, _c2, _ch2 = phase_case("cancel-park", "ok", cache_markers=("skip parking",))
    ev2 = rec2["phase_run"]
    check(ev2["phase_ok"] is False and ev2["evidence_seen"] == []
          and ev2["engine_cache_markers"].get("skip parking") is True,
          "the engine's own 'skip parking' reason is recorded and the phase stays NOT ok - exactly the "
          "101959 shape")
    rec3, _n3, _s3, _c3, _ch3 = phase_case("rewind", "ok", cache_markers=())
    check(rec3["phase_run"]["phase_ok"] is True and rec3["phase_run"]["evidence_required"] == [],
          "the rewind lane keeps its own gate (its tool asserts its arms itself)")
    rec4, _n4, signals4, _c4, _ch4 = phase_case("cancel-park", "ok", cache_markers=("parked", "restored"))
    check(rec4["phase_run"]["phase_ok"] is True
          and rec4["phase_run"]["evidence_seen"] == ["parked", "restored"] and signals4 == [],
          "with both engine events present the lane passes and nothing is signalled (ownership unchanged)")


for fn in (test_derivation, test_allowlist_refusals, test_currency, test_source_support, test_identity,
           test_usage_proof, test_log_proof, test_fallback_exemption, test_write_config_mode,
           test_structured_drift, test_runner_success, test_runner_refusals, test_runner_guards_unchanged,
           test_lifecycle_order, test_restore_gate, test_constants, test_phase_dispatch,
           test_phase_window_guards, test_owned_run_ownership, test_phase_restore_refusal,
           test_phase_live_lineage, test_phase_split_gate, test_phase_child_env,
           test_phase_copy_session, test_phase_split_source, test_phase_static_surface,
           test_phase_controls, test_cancel_tool_source, test_phase_evidence_gate):
    print(f"--- {fn.__name__} ---")
    fn()

import shutil
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'ALL REGRESSION-HELPER FIXTURES PASS' if not FAILS else f'FAILURES ({len(FAILS)}): {FAILS}'}")
sys.exit(1 if FAILS else 0)

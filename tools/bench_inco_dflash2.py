#!/usr/bin/env python3
"""Safe plan/report harness for the Inco DFlash2 benchmark program (L9). PLAN-ONLY by default.

Modes (identical prompts/seeds/settings; the ONLY deltas are binary + drafter flags):
  prod_reference   the live production invocation (preserved verbatim - the baseline)
  new_target_only  the prototype binary with the same prod flags, NO drafter
  new_mtp          the prototype binary with --mtp (the current production reference drafter)
  new_dflash       the prototype binary + --dflash <BF16 artifact> (BF16 primary; no quantized
                   drafter claims until conversion/parity is validated)
Widths 1..7 -> --spec w+1 --dflash-block w (width=min(block,spec-1), generate.cpp:5500).
Contexts 2K/16K/64K/200K (+260K optional): token counts come from the ENGINE's reported
prompt token counts (strata_bench reads the serve response); text chars are NEVER asserted
as tokens.

Reuse, not duplication: prompt fixtures + metric names follow the engine's own bench tool
(tools/strata_bench.py: decode_tps, ttft_s, prompt_ms, decode_ms, generated_tokens); the
integration gates stay in tools/test_inco_dflash2_integration.py.  Metrics the engine does
not expose for a mode are recorded as null WITH A REASON - no fabricated zeros, no synthetic
speedups.  Medians/spread come only from recorded repetitions (--report DIR with run files);
a dry run NEVER reports results.

Correctness gate before any benchmark: greedy byte-compare new_target_only vs new_dflash
with the SAME expert-cache configuration and STRATA_IQ_MT_MIN=1 (bit-for-bit CPU/GPU expert
path, generate.cpp:14400 / native_expert_parity.cpp:804-808) - and the unchanged
old-binary-vs-new-binary target-only gate FIRST, so cache/displacement differences are
attributed before diagnosing DFlash.  Benchmark auto-cache deployment stays unchanged.

  python tools/bench_inco_dflash2.py --plan
  python tools/bench_inco_dflash2.py --hash        # sha256 binaries/model/checkpoint (CPU, read-only)
  python tools/bench_inco_dflash2.py --selfcheck   # CPU unit fixtures for the parsers/stats
  python tools/bench_inco_dflash2.py --report DIR   # medians/spread from recorded run JSONs
This harness NEVER touches a GPU, never stops/restarts production, and refuses --execute
semantics: it only prints parent-owned commands (prod-down + user approval required).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_inco_dflash2_integration as integ  # reuse: paths, prefixes, prod guard (no duplication)

WIDTHS = [1, 2, 3, 4, 5, 6, 7]
CONTEXTS = [2048, 16384, 65536, 200000]
CONTEXTS_OPTIONAL = [260000]
# Metric keys the engine bench tool exposes (tools/strata_bench.py); unsupported => null+reason.
METRICS = ["decode_tps", "ttft_s", "prompt_ms", "decode_ms", "generated_tokens"]
NO_METRIC_REASON = "engine does not expose this metric for the mode/run (recorded truthfully as null)"


def width_flags(w: int) -> str:
    if not 1 <= w <= 7:
        raise ValueError(f"width {w}: proposals run 1..7 (window <= kVerifyMaxT rows: spec=w+1<=8)")
    return f"--spec {w + 1} --dflash-block {w}"


def build_commands() -> dict[str, list[str]]:
    base = integ.PROD_SERVE_ARGS
    cmds: dict[str, list[str]] = {}
    cmds["prod_reference"] = [f"{integ.PROD_ENV_PREFIX} {integ.PROD_BINARY} {base}"]
    cmds["new_target_only"] = [f"{integ.PROD_ENV_PREFIX} {integ.PROTO_BINARY} {base}"]
    cmds["new_mtp"] = [f"{integ.PROD_ENV_PREFIX} {integ.PROTO_BINARY} {base} (parent supplies the "
                       f"production --mtp drafter flags; this harness does not invent them)"]
    for w in WIDTHS:
        cmds[f"new_dflash_w{w}"] = [
            f"{integ.PROD_ENV_PREFIX} {integ.PROTO_BINARY} {base} --dflash {integ.ARTIFACT} "
            f"--spec {w + 1} --dflash-block {w} --spec-min-p 0.5"]
    return cmds


def greedy_gate_commands() -> list[str]:
    pin = "STRATA_IQ_MT_MIN=1"
    same_cache = "--expert-cache auto --expert-profile " + integ.PROFILE
    return [
        f"[gate 1, unchanged target-only] old binary vs new binary, identical {integ.PROD_BINARY.split('/')[-1]}"
        f" flags, greedy fixed prompts, {pin} {same_cache}",
        f"[gate 2, drafter attribution] new binary target-only vs new binary --dflash w3, identical cache "
        f"({same_cache}), greedy byte-compare outputs, {pin}",
    ]


def summarize(values: list[float]) -> dict:
    if not values:
        raise ValueError("no recorded repetitions to summarize")
    return {"n": len(values), "median": statistics.median(values),
            "min": min(values), "max": max(values),
            "spread": (max(values) - min(values)) / max(statistics.median(values), 1e-9)}


def report_dir(d: pathlib.Path) -> int:
    """Medians/spread from recorded run JSON files (parent-produced). Nulls stay null."""
    rows = []
    for f in sorted(d.glob("*.json")):
        rec = json.loads(f.read_text())
        row = {"run": f.name}
        for m in METRICS:
            row[m] = rec.get(m)
            if row[m] is None:
                row[f"{m}_reason"] = rec.get(f"{m}_reason", NO_METRIC_REASON)
        rows.append(row)
    print(json.dumps(rows, indent=2))
    for m in METRICS:
        vals = [r[m] for r in rows if isinstance(r[m], (int, float))]
        print(f"{m}: " + (json.dumps(summarize(vals)) if vals else f"null - {NO_METRIC_REASON}"))
    return 0


def selfcheck() -> int:
    fails = 0

    def check(ok: bool, what: str) -> None:
        nonlocal fails
        print(f"{'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            fails += 1

    check(width_flags(3) == "--spec 4 --dflash-block 3", "width 3 maps to --spec 4 --dflash-block 3 (min(3,3)=3)")
    check(width_flags(7) == "--spec 8 --dflash-block 7", "width 7 maps to --spec 8 (kVerifyMaxT=8 bound)")
    try:
        width_flags(8)
        check(False, "width 8 accepted (must refuse: spec would exceed kVerifyMaxT)")
    except ValueError:
        pass
    s = summarize([2.0, 4.0, 3.0, 30.0])
    check(s["median"] == 3.0 and s["n"] == 4, "median from repetitions (order-insensitive)")
    check(abs(s["spread"] - 28.0 / 3.0) < 1e-9, "spread is (max-min)/median (reported, never smoothed)")
    try:
        summarize([])
        check(False, "empty repetitions accepted (must refuse)")
    except ValueError:
        pass
    check(all("--spec 4 --dflash-block 3" in c for c in build_commands()["new_dflash_w3"][0:1]),
          "dflash command carries the width flags")
    check("--dflash" not in build_commands()["new_target_only"][0], "target-only mode carries no drafter flag")
    cmds = build_commands()
    check(cmds["prod_reference"] == cmds["prod_reference"] and integ.PROD_BINARY in cmds["prod_reference"][0],
          "prod reference command preserved verbatim (baseline)")
    print(f"contexts: {CONTEXTS} (+ optional {CONTEXTS_OPTIONAL}); token counts are engine-reported, "
          "never char-count asserts")
    return 1 if fails else 0


def hashes() -> int:
    import hashlib

    def sha(p: str) -> str:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
        return h.hexdigest()

    out = {}
    for label, p in [("proto_binary", integ.PROTO_BINARY), ("prod_binary", integ.PROD_BINARY),
                     ("native_model", integ.NATIVE), ("artifact", integ.ARTIFACT),
                     ("checkpoint", "/mnt/user/dflash2/checkpoint/model.safetensors")]:
        try:
            out[label] = sha(p)
        except OSError as e:
            out[label] = None
            out[f"{label}_reason"] = str(e)
    print(json.dumps(out, indent=2))
    return 0 if all(v is not None for k, v in out.items() if not k.endswith("_reason")) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--hash", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--report", metavar="DIR")
    ap.add_argument("--execute", action="store_true", help="print the parent-owned execution manifest (guarded)")
    ap.add_argument("--expect-stopped", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        return selfcheck()
    if a.hash:
        return hashes()
    if a.report:
        return report_dir(pathlib.Path(a.report))
    plan = {"modes": build_commands(), "widths_semantics": "width w -> --spec w+1 --dflash-block w (5500)",
            "contexts": CONTEXTS, "contexts_optional": CONTEXTS_OPTIONAL,
            "metrics": METRICS, "metric_null_reason": NO_METRIC_REASON,
            "greedy_gate": greedy_gate_commands(),
            "prompts": "fixed prompts/seeds: reuse tools/strata_bench.py fixtures (parent runs it; "
                       "engine-reported token counts only)",
            "cache": "benchmark auto-cache deployment unchanged; greedy pins --expert-cache auto + "
                     "STRATA_IQ_MT_MIN=1",
            "drafter_precision": "BF16 primary; quantized drafter claims require conversion/parity validation first",
            "repetitions": "medians/spread only from recorded run JSONs via --report; dry runs report nothing"}
    print(json.dumps(plan, indent=2))
    if a.execute:
        if integ.prod_running() and not a.expect_stopped:
            print("REFUSED: production engine RUNNING; benchmarks need the user-approved prod-down window. "
                  "This harness never stops/restarts production and never runs GPU work.")
            return 2
        print("EXECUTION PLAN (parent-owned only): the parent runs the mode commands with repetitions and "
              "records per-run JSONs for --report. This harness executed NOTHING.")
    else:
        print("plan-only: no GPU, no engine started, no production mutation")
    return 0


if __name__ == "__main__":
    sys.exit(main())

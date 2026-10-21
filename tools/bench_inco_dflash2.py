#!/usr/bin/env python3
"""Inco DFlash2 benchmark program: PLAN + REAL grouped measurement against the ISOLATED
prototype server only (L9, parent executes everything; the fixer ran nothing).

Modes come from the ACTUAL production config clone (tools/test_inco_dflash2_integration.py
cloner - reused, not duplicated):
  prod_reference   OBSERVED TARGET-ONLY (prod args contain NO --mtp/--dflash; the
                   drafter-verify flags --spec/--spec-min-p are drafter-only per
                   generate.cpp:5445/9176 and inert without a drafter). No invented MTP.
  new_target_only  prototype binary, prod args verbatim (byte-identical baseline).
  new_mtp          NOTAVAILABLE: no MTP drafter artifact exists in the model dirs and the
                   production config has no --mtp - reported with the reason, never faked.
  new_dflash_wN    prototype + --dflash BF16 artifact, width w = min(--dflash-block, --spec-1)
                   (generate.cpp:5500); widths 1..7 (--spec w+1 <= kVerifyMaxT 8).
Contexts 2K/16K/64K/200K (+260K optional): token counts are the ENGINE-reported prompt_tokens
from responses/slots - character counts are never asserted as tokens.

Measurement (parent-run --execute against the isolated prototype URL, port 8019 ONLY):
  * guard: production OBSERVED ABSENT (detection failure = fail-closed; port 8009 refused;
    --confirm-execution required). Nothing here stops/restarts/probes production.
  * per-run JSONs recorded per (mode, context, width, prompt, seed, repeat) - summary is
    GROUPED by that configuration; modes/contexts are NEVER aggregated into one number.
  * metrics reuse the server/engine fields (decode_tps via generated/decode_ms, ttft_s,
    prompt_ms, decode_ms, generated_tokens, prompt_tokens); metrics the server does not
    expose for a run are null WITH A REASON - no fabricated zeros, no synthetic speedups.
  * non-finite/bool numerics are refused; failed runs are recorded and EXCLUDED explicitly.

Correctness vs performance (parent review): the greedy byte-compare gate PINS a caller-
supplied equal fixed expert-cache slot count (--cache-slots N -> --expert-cache N, parsed
at generate.cpp:2536 as an int slot value; 'auto' is NOT a fixed pin) on BOTH sides plus
STRATA_IQ_MT_MIN=1 (generate.cpp:14400; native_expert_parity sets it), old-binary-vs-new-
binary target-only FIRST, then target-only-vs-dflash on the same pinned config. Benchmark
mode keeps the production `--expert-cache auto` deployment unchanged.

Hashes: default is the cheap fingerprint preflight (sizes/mtimes + the durable L3/L4 shas);
full 76 GB model hashing only with --offline-guarded (prod-up full reads thrash the page
cache - issue #1691).

  python tools/bench_inco_dflash2.py --plan
  python tools/bench_inco_dflash2.py --selfcheck
  python tools/bench_inco_dflash2.py --preflight
  python tools/bench_inco_dflash2.py --hash --offline-guarded
  python tools/bench_inco_dflash2.py --execute --url http://127.0.0.1:8019 --mode dflash_w3 \
      --context 16384 --repeats 3 --confirm-execution        # parent only
  python tools/bench_inco_dflash2.py --report RUNS_DIR
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_inco_dflash2_integration as integ  # reuse cloner/guards/prod detection

WIDTHS = list(range(1, 8))
CONTEXTS = [2048, 16384, 65536, 200000]
CONTEXTS_OPTIONAL = [260000]
PROMPTS = {  # fixed fixture prompts; token counts are engine-reported, never char-asserted
    "short": "Explain, in two sentences, why a layer split keeps per-stage K/V pools.",
    "mid": "Write a short technical note describing sliding-window attention for draft layers.",
}
SEED = 20261021
METRICS = ["decode_tps", "ttft_s", "prompt_ms", "decode_ms", "generated_tokens", "prompt_tokens"]
NO_METRIC = "server/engine did not expose this metric for the run (recorded null with reason)"


def width_flags(w: int) -> list[str]:
    if not 1 <= w <= 7:
        raise ValueError(f"width {w}: proposals run 1..7 (--spec w+1 <= kVerifyMaxT = 8)")
    return ["--spec", str(w + 1), "--dflash-block", str(w)]


def plan_modes(prod: dict) -> dict:
    modes = {
        "prod_reference": {"kind": "OBSERVED TARGET-ONLY (no --mtp/--dflash in prod args)",
                            "server_config": "production (parent starts as-is, in the downtime window)"},
        "new_target_only": {"kind": "clone, prod args verbatim",
                             "server_config": str(integ.CONFIG_DIR / "proto-server-swift15-target_only.json")},
        "new_mtp": {"kind": "NOTAVAILABLE", "why": "no MTP drafter artifact in the model dirs; "
                    "no --mtp in production; flags would be invented"},
    }
    for w in WIDTHS:
        modes[f"new_dflash_w{w}"] = {
            "kind": "clone + --dflash + width flags",
            "flags": " ".join(width_flags(w)),
            "server_config_base": str(integ.CONFIG_DIR / "proto-server-swift15-dflash_w3.json"),
        }
    return modes


def prod_expert_profile(prod: dict) -> str:
    args = prod["args"]
    return args[args.index("--expert-profile") + 1]


def correctness_gate(cache_slots: int) -> list[dict]:
    if cache_slots <= 0:
        raise ValueError("correctness gate needs an explicit equal --cache-slots N (int slots, "
                         "generate.cpp:2536); 'auto' is not a fixed pin")
    prod = integ.load_prod_config()
    pin = f"--expert-cache {cache_slots} --expert-profile {prod_expert_profile(prod)}"
    env = "STRATA_IQ_MT_MIN=1 "
    return [
        {"gate": 1, "run": "old-binary target-only vs new-binary target-only, greedy, fixed prompts",
         "env": env, "pin": pin, "purpose": "binary/cache attribution BEFORE any DFlash diagnosis"},
        {"gate": 2, "run": "new-binary target-only vs new-binary --dflash w3, greedy, fixed prompts",
         "env": env, "pin": pin, "purpose": "drafter effect isolated on the identical pinned cache"},
    ]


def summarize(values: list[float]) -> dict:
    vals = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            raise ValueError(f"refused non-numeric/non-finite metric value {v!r}")
        vals.append(float(v))
    if not vals:
        raise ValueError("no recorded repetitions to summarize")
    med = statistics.median(vals)
    return {"n": len(vals), "median": med, "min": min(vals), "max": max(vals),
            "spread_over_median": (max(vals) - min(vals)) / max(med, 1e-9)}


def grouped_summary(runs_dir: pathlib.Path) -> int:
    groups: dict[tuple, list[dict]] = {}
    excluded = []
    for f in sorted(runs_dir.glob("*.json")):
        rec = json.loads(f.read_text())
        if rec.get("status") == "failed":
            excluded.append(f.name)
            continue
        key = (rec["mode"], rec["context"], rec["width"], rec["prompt_id"])
        groups.setdefault(key, []).append(rec)
    out = {}
    for key, recs in groups.items():
        g = {"n": len(recs)}
        for m in METRICS:
            vals = [r[m] for r in recs if r.get(m) is not None]
            g[m] = summarize(vals) if vals else None
            if not vals:
                g[f"{m}_reason"] = NO_METRIC
        out["/".join(map(str, key))] = g
    print(json.dumps({"groups": out, "excluded_failed_runs": excluded,
                      "aggregation": "NONE across modes/contexts/widths - groups stay separate"}, indent=2))
    return 0


def execute_measure(a) -> int:
    reason = integ.guard_execute(a.url, a.confirm_execution)
    if reason is not None:
        print(reason)
        return 2
    runs = pathlib.Path(a.runs)
    for rep in range(a.repeats):
        body = {"model": "swift-1.5-iq3_xxs",
                "messages": [{"role": "user", "content": PROMPTS[a.prompt]}],
                "max_tokens": a.max_tokens, "temperature": 0.0, "stream": False, "seed": SEED}
        metrics: dict = {}
        status = "ok"
        try:
            resp = integ.http_json(f"{a.url}/v1/chat/completions", body, timeout=600.0)
            usage = resp.get("usage") or {}
            slots = integ.http_json(f"{a.url}/slots")
            last = (slots.get("last") or {}) if isinstance(slots, dict) else {}
            gen = usage.get("completion_tokens")
            ptoks = usage.get("prompt_tokens")
            dms, pms = last.get("decode_ms"), last.get("prompt_ms")
            metrics["generated_tokens"] = gen
            metrics["prompt_tokens"] = ptoks
            metrics["decode_ms"] = dms
            metrics["prompt_ms"] = pms
            metrics["decode_tps"] = (gen * 1000.0 / dms) if gen and dms else None
            metrics["ttft_s"] = last.get("ttft_s")   # exposed when the server reports it
        except (OSError, ValueError, json.JSONDecodeError) as e:
            status = "failed"
            metrics = {m: None for m in METRICS}
            print(f"run failed (recorded, excluded from summary): {e}")
        runs.mkdir(parents=True, exist_ok=True)
        rec_file = runs / f"{a.mode}_c{a.context}_w{a.width}_{a.prompt}_r{rep}.json"
        rec_file.write_text(json.dumps({"mode": a.mode, "context": a.context, "width": a.width,
                                        "prompt_id": a.prompt, "seed": SEED, "repeat": rep,
                                        "status": status, **{m: metrics.get(m) for m in METRICS}}, indent=2) + "\n")
    print(f"recorded {a.repeats} runs under {runs} (per-run files; summarize with --report {runs}). "
          "Token counts are engine-reported; ttft is recorded only where the server exposes it.")
    return 0


def preflight() -> int:
    files = [("proto_binary", integ.PROTO_BINARY), ("prod_binary", integ.PROD_BINARY),
             ("artifact", integ.ARTIFACT), ("checkpoint", "/mnt/user/dflash2/checkpoint/model.safetensors")]
    out = {}
    for label, p in files:
        try:
            st = pathlib.Path(p).stat()
            out[label] = {"path": p, "size": st.st_size, "mtime_s": int(st.st_mtime)}
        except OSError as e:
            out[label] = {"path": p, "error": str(e)}
    out["note"] = ("fingerprint preflight only (no full reads - prod-up hashing of the 76 GB target "
                   "thrashes the page cache, #1691). Checkpoint sha d6723f2a..3186f and artifact sha "
                   "f9f8f5c0..29084 are already verified durably (L3/L4 reports). Full hashes: "
                   "--hash --offline-guarded only.")
    print(json.dumps(out, indent=2))
    return 0


def full_hashes() -> int:
    import hashlib

    def sha(p: str) -> str:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
        return h.hexdigest()

    out = {}
    for label, p in [("proto_binary", integ.PROTO_BINARY), ("prod_binary", integ.PROD_BINARY),
                     ("artifact", integ.ARTIFACT),
                     ("checkpoint", "/mnt/user/dflash2/checkpoint/model.safetensors")]:
        try:
            out[label] = sha(p)
        except OSError as e:
            out[label] = None
            out[f"{label}_reason"] = str(e)
    print(json.dumps(out, indent=2))
    return 0 if all(v is not None for k, v in out.items() if not k.endswith("_reason")) else 1


def selfcheck() -> int:
    fails = 0

    def check(ok: bool, what: str) -> None:
        nonlocal fails
        print(f"{'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            fails += 1

    check(width_flags(3) == ["--spec", "4", "--dflash-block", "3"], "width 3 -> --spec 4 --dflash-block 3")
    check(width_flags(7) == ["--spec", "8", "--dflash-block", "7"], "width 7 -> --spec 8 (kVerifyMaxT bound)")
    try:
        width_flags(8)
        check(False, "width 8 accepted (must refuse)")
    except ValueError:
        pass
    s = summarize([2.0, 4.0, 3.0, 30.0])
    check(s["median"] == 3.5, "median of [2,4,3,30] is 3.5 (the fixture math the parent caught)")
    check(abs(s["spread_over_median"] - 28.0 / 3.5) < 1e-9, "spread = (max-min)/median = 28/3.5 = 8.0")
    check(summarize([5.0])["n"] == 1, "single repetition recorded honestly")
    try:
        summarize([1.0, float("nan")])
        check(False, "NaN accepted (must refuse non-finite)")
    except ValueError:
        pass
    try:
        summarize([True, 2.0])
        check(False, "bool accepted (must refuse)")
    except ValueError:
        pass
    try:
        summarize([])
        check(False, "empty repetitions accepted (must refuse)")
    except ValueError:
        pass
    prod = integ.load_prod_config()
    modes = plan_modes(prod)
    check(modes["prod_reference"]["kind"].startswith("OBSERVED"), "prod_reference labeled observed target-only")
    check(modes["new_mtp"]["kind"] == "NOTAVAILABLE", "new_mtp NOTAVAILABLE with reason (no fake baseline)")
    gate = correctness_gate(8192)
    check(gate[0]["pin"].startswith("--expert-cache 8192") and "auto" not in gate[0]["pin"],
          "correctness gate pins an EXPLICIT equal --expert-cache slot count (auto is not a pin)")
    check(all("STRATA_IQ_MT_MIN=1" in g["env"] for g in gate), "greedy gates carry the bit-for-bit env pin")
    r = integ.guard_execute("http://127.0.0.1:8009", True)
    check(r is not None, "bench guard: production port refused outright")
    print(f"contexts {CONTEXTS} (+{CONTEXTS_OPTIONAL}); prompts fixed; seed {SEED}; token counts "
          "engine-reported; grouped summary never aggregates across configurations")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--hash", action="store_true")
    ap.add_argument("--offline-guarded", action="store_true")
    ap.add_argument("--report", metavar="DIR")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--url", default=integ.PROTO_URL_DEFAULT)
    ap.add_argument("--mode", default="new_dflash_w3")
    ap.add_argument("--width", type=int, default=3)
    ap.add_argument("--context", type=int, default=16384)
    ap.add_argument("--prompt", choices=list(PROMPTS), default="short")
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--runs", default="/mnt/user/dflash2/bench-runs")
    ap.add_argument("--cache-slots", type=int, default=0)
    ap.add_argument("--confirm-execution", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        return selfcheck()
    if a.preflight:
        return preflight()
    if a.hash:
        if not a.offline_guarded:
            print("REFUSED: full hashing needs --offline-guarded (prod-up full reads of the 76 GB "
                  "target thrash the page cache, #1691). Use --preflight for the safe fingerprint.")
            return 2
        return full_hashes()
    if a.report:
        return grouped_summary(pathlib.Path(a.report))
    if a.execute:
        return execute_measure(a)
    prod = integ.load_prod_config()
    plan = {"modes": plan_modes(prod), "widths": WIDTHS, "width_semantics": "w = min(--dflash-block, --spec-1) (5500)",
            "contexts": CONTEXTS, "contexts_optional": CONTEXTS_OPTIONAL, "prompts": list(PROMPTS),
            "seed": SEED, "metrics": METRICS, "metric_null": NO_METRIC,
            "grouping": "per (mode, context, width, prompt, seed); NEVER one aggregate number",
            "correctness_gate": correctness_gate(a.cache_slots) if a.cache_slots > 0
            else [{"need": "explicit --cache-slots N (equal fixed --expert-cache slots) for the greedy gate; "
                   "benchmark mode keeps --expert-cache auto unchanged"}],
            "repetitions": "medians/spread via --report DIR from recorded per-run JSONs; dry plans report no results"}
    print(json.dumps(plan, indent=2))
    print("plan-only: no GPU, no server touched, no production mutation")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Inco DFlash2 benchmark program: PLAN + REAL grouped measurement against the ISOLATED
prototype server only (L9, parent executes everything; the fixer ran nothing - NO HTTP by the
author, no GPU, no production).

Modes come from the ACTUAL production config clone (tools/test_inco_dflash2_integration.py
cloner - reused, not duplicated). BASELINE TRUTH (parent-verified source + runtime logs,
2026-10-22): production is a SUFFIX-LOOKUP baseline - suffix_draft DEFAULT 3
(generate.cpp:778; sfx_on = suffix_draft>0 || lookup_chain>0 at 14338/16711), prod logs
show "suffix drafts: N windows, X of Y accepted"; MTP is OFF (use_mtp=!o.mtp.empty(),
8344; no --mtp in args). The earlier "TARGET-ONLY" label was WRONG.
  prod_reference   SUFFIX LOOKUP baseline (prod config/args/env verbatim).
  new_suffix_lookup new binary, prod args verbatim (byte-identical suffix-lookup baseline).
  new_target_only  PURE autoregressive: --suffix-draft 0 --lookup-chain 0 --spec 2 (native-pack floor).
  old_target_only  PURE autoregressive on the OLD prod binary (old-vs-new correctness gate).
  new_mtp          READY-RT (config valid; runtime NOTRUN): loader input
                   /home/user/dev/Strata-data/mtp-bf16/rt/ exists (dense.txt 29 lines,
                   dense.bin ~116 MB, experts.bin ~675 MiB, draft_vocab.bin ~415 KiB;
                   tools/mtp_rt.py contract; MtpDrafter consumes it directly, no copies).
                   Args --mtp <rt> --spec 6 --spec-min-p 0.7 --suffix-draft 0 (pure MTP).
                   Geometry compatible STATICALLY; ACTUAL GPU load + Swift acceptance
                   UNVALIDATED - never "pack validated". Lama GGUFs under ai/ai are not
                   Strata-loadable.
  new_dflash_wN    prototype + --dflash BF16 artifact, --spec w+1 --dflash-block w
                   --suffix-draft 0; width w = min(--dflash-block, --spec-1)
                   (generate.cpp:5500); widths 1..7 (--spec w+1 <= kVerifyMaxT 8).
Contexts 2K/16K/64K/200K (+260K optional). CONTEXT INTEGRITY (parent review): every record
stores the ENGINE-OBSERVED prompt_tokens as observed_context_tokens; the REQUESTED context is
a separate label. A run whose observed prompt is below --context minus --context-tolerance is
recorded as context-mismatch, EXCLUDED from statistics, and makes --execute exit NONZERO.
Long contexts need a real long fixture via --prompt-file - a 20-token prompt can NEVER claim
a 200K benchmark.

Measurement reuses the existing client (tools/strata_bench.py: chat() streams, digest()
extracts the engine's own numbers - decode_tps with source label, ttft_s, total_s,
prompt_tps, prompt_tokens, generated_tokens, reused_tokens, read_tokens, drafts_offered,
drafts_accepted). The draft pair is the engine's own draft_n/draft_n_accepted
(serve/server.py:5611), which the engine reports ONLY when a drafter ran: a no-drafter arm
(target-only) records null WITH REASON, never a zeroed acceptance rate. No invented
metric fields: /slots has only {id,n_ctx,is_processing,n_prompt_tokens}; /props has
model_alias/model_path/build_info - these are used for IDENTITY, not speed.
  * guard: production OBSERVED ABSENT (detection failure = fail-closed; port 8009 refused;
    --confirm-execution required). Nothing here stops/restarts/probes production.
  * identity: --identity-config is REQUIRED for --execute; the mode/width LABEL must match
    the config's EFFECTIVE flags (--dflash once, --spec = width+1, --dflash-block = width,
    no --mtp for dflash modes; no drafter flags for target-only); the config file's sha256
    is recorded per run and is part of the GROUP KEY so different configs never mix. The
    /props identity snapshot (model_alias, model_path, build_info) is taken BEFORE and
    AFTER the run set and must be identical - a mid-set config change fails the batch.
  * per-run JSONs under --runs; --report groups medians/spread per (mode, observed_context,
    width, prompt, seed, config_sha); modes/contexts are NEVER aggregated into one number.
  * non-finite/bool numerics are refused; failed/mismatched runs are recorded, excluded,
    and --execute returns nonzero when any run failed.

Correctness vs performance (parent review): the greedy byte-compare gate PINS a caller-
supplied equal fixed expert-cache slot count (--cache-slots N -> --expert-cache N, parsed
at generate.cpp:2536 as an int slot value; 'auto' is NOT a fixed pin) on BOTH sides plus
STRATA_IQ_MT_MIN=1 (generate.cpp:14400), old-binary-vs-new-binary target-only FIRST, then
target-only-vs-dflash on the same pinned config. Benchmark mode keeps the production
`--expert-cache auto` deployment unchanged.

Hashes: default is the cheap fingerprint preflight (sizes/mtimes + the durable L3/L4 shas);
full 76 GB model hashing only with --offline-guarded (prod-up full reads thrash the page
cache - issue #1691).

  python tools/bench_inco_dflash2.py --plan
  python tools/bench_inco_dflash2.py --selfcheck
  python tools/bench_inco_dflash2.py --preflight
  python tools/bench_inco_dflash2.py --hash --offline-guarded
  python tools/bench_inco_dflash2.py --execute --url http://127.0.0.1:8019 \
      --identity-config /mnt/user/dflash2/config/proto-server-swift15-dflash_w3.json \
      --mode dflash_w3 --width 3 --context 16384 --prompt-file LONG.txt \
      --repeats 3 --confirm-execution                                  # parent only
  python tools/bench_inco_dflash2.py --report RUNS_DIR
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, "/mnt/user/Strata/tools")          # the EXISTING client implementation
import test_inco_dflash2_integration as integ          # reuse cloner/guards/prod detection
import strata_bench                                      # chat()/digest()/try_json() - reused

WIDTHS = list(range(1, 8))
CONTEXTS = [2048, 16384, 65536, 200000]
CONTEXTS_OPTIONAL = [260000]
PROMPTS = {
    "short": "Explain, in two sentences, why a layer split keeps per-stage K/V pools.",
    "mid": "Write a short technical note describing sliding-window attention for draft layers.",
}
SEED = 20261021
METRICS = ["decode_tps", "ttft_s", "total_s", "prompt_tps", "generated_tokens",
           "prompt_tokens", "reused_tokens", "read_tokens", "drafts_offered", "drafts_accepted"]
NO_METRIC = "engine did not report this metric for the run (digest left it null, with source)"


def width_flags(w: int) -> list[str]:
    if not 1 <= w <= 7:
        raise ValueError(f"width {w}: proposals run 1..7 (--spec w+1 <= kVerifyMaxT = 8)")
    return ["--spec", str(w + 1), "--dflash-block", str(w)]


def flag_value(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def validate_mode_flags(mode: str, width: int, args: list[str]) -> str | None:
    """Label integrity: the mode/width label must match the config's EFFECTIVE flags.
    Baseline truth (generate.cpp:778 suffix_draft default 3; sfx_on 14338/16711;
    use_mtp 8344): suffix lookup is ON unless --suffix-draft 0 --lookup-chain 0; prod's
    verbatim args ARE a suffix-lookup baseline, never target-only. Returns the refusal
    reason, None when the label is honest."""
    suffix_off = flag_value(args, "--suffix-draft") == "0"
    chain_off = flag_value(args, "--lookup-chain") in (None, "0")
    if mode in ("prod_reference", "suffix_lookup"):
        if "--mtp" in args or "--dflash" in args:
            return f"mode {mode}: labeled suffix-lookup baseline but the config carries drafter flags"
        if suffix_off and chain_off:
            return f"mode {mode}: config is PURE target-only (suffix+chain off) - label it target_only"
        return None
    if mode in ("target_only", "old_target_only"):
        if "--mtp" in args or "--dflash" in args:
            return f"mode {mode}: pure-AR label carries a drafter flag"
        if not suffix_off or not chain_off or flag_value(args, "--spec") != "2":
            return (f"mode {mode}: pure AR requires --suffix-draft 0 --lookup-chain 0 --spec 2 "
                    "(spec 1 is refused for a native IQ pack, generate.cpp:3685; verbatim prod args "
                    "are suffix-lookup, not AR)")
        return None
    if mode == "mtp":
        if flag_value(args, "--mtp") != integ.RT_DIR:
            return f"mode mtp: --mtp must point at the ready loader dir {integ.RT_DIR} (no copies, no invented paths)"
        if "--dflash" in args:
            return "mode mtp: --dflash beside --mtp is not the pure-MTP measurement"
        if not suffix_off:
            return "mode mtp: requires --suffix-draft 0 (pure MTP, not mixed suffix)"
        return None
    if mode.startswith("dflash_w"):
        if args.count("--dflash") != 1:
            return f"mode {mode}: config must carry exactly one --dflash"
        if flag_value(args, "--spec") != str(width + 1):
            return f"mode {mode}: --spec must be {width + 1} (width = min(block, spec-1), 5500)"
        if flag_value(args, "--dflash-block") != str(width):
            return f"mode {mode}: --dflash-block must be {width}"
        if not suffix_off:
            return f"mode {mode}: requires --suffix-draft 0 to isolate the drafter effect"
        return None
    return f"mode {mode}: unknown label"


def plan_modes(prod: dict) -> dict:
    modes = {
        "prod_reference": {"kind": "SUFFIX LOOKUP baseline (observed: suffix_draft default 3, "
                                    "generate.cpp:778; runtime logs 'suffix drafts: N windows, X of Y "
                                    "accepted') - NOT target-only, NOT MTP",
                           "server_config": "production (parent starts as-is, in the downtime window)"},
        "new_suffix_lookup": {"kind": "clone, prod args verbatim = new-binary suffix-lookup baseline",
                              "server_config": str(integ.CONFIG_DIR / "proto-server-swift15-suffix_lookup.json")},
        "new_target_only": {"kind": "PURE AR: --suffix-draft 0 --lookup-chain 0 --spec 2 "
                            "(native-pack floor; no-drafter rows are inert oracle/0 fill)",
                            "server_config": str(integ.CONFIG_DIR / "proto-server-swift15-target_only.json")},
        "old_target_only": {"kind": "PURE AR on the OLD prod binary (old-vs-new correctness gate)",
                            "server_config": str(integ.CONFIG_DIR / "proto-server-swift15-old_target_only.json")},
        "new_mtp": {"kind": "READY-RT (config valid; runtime NOTRUN)", "why": "loader input "
                    f"{integ.RT_DIR} exists (dense.txt/dense.bin/experts.bin/draft_vocab.bin, "
                    "tools/mtp_rt.py contract; metadata preflight PASS at parent run); args "
                    "--mtp <rt> --spec 6 --spec-min-p 0.7 --suffix-draft 0. Geometry compatible "
                    "statically; ACTUAL GPU load + Swift acceptance UNVALIDATED - never 'pack validated'. "
                    "Lama-format GGUFs under ai/ai are NOT Strata-loadable."},
    }
    for w in WIDTHS:
        modes[f"new_dflash_w{w}"] = {
            "kind": f"clone + --dflash + --spec {w + 1} --dflash-block {w} --suffix-draft 0",
            "server_config": str(integ.CONFIG_DIR / f"proto-server-swift15-dflash_w{w}.json"),
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


def config_sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def identity_snapshot(url: str) -> dict:
    """/props identity: model_alias, model_path (when the engine reports it), build_info.
    Server-side identity only - /props has no exe/args; flags are verified from the config."""
    props = strata_bench.try_json(url, "/props", key="", timeout=30) or {}
    return {"model_alias": props.get("model_alias"), "model_path": props.get("model_path"),
            "build_info": props.get("build_info")}


def execute_measure(a) -> int:
    reason = integ.guard_execute(a.url, a.confirm_execution)
    if reason is not None:
        print(reason)
        return 2
    cfg_path = pathlib.Path(a.identity_config)
    cfg = json.loads(cfg_path.read_text())
    reason = validate_mode_flags(a.mode, a.width, cfg["args"])
    if reason is not None:
        print(f"REFUSED (label integrity): {reason}")
        return 2
    sha = config_sha(cfg_path)
    prompt = pathlib.Path(a.prompt_file).read_text() if a.prompt_file else PROMPTS[a.prompt]
    if a.prompt_file is None and a.context > 4096:
        print(f"REFUSED: context {a.context} requested with the built-in short fixture - a ~20-token "
              "prompt cannot claim a long-context benchmark. Provide --prompt-file with a fixture "
              "whose engine-reported prompt_tokens meet the requested context.")
        return 2
    props_before = identity_snapshot(a.url)
    runs = pathlib.Path(a.runs)
    runs.mkdir(parents=True, exist_ok=True)
    failures = 0
    for rep in range(a.repeats):
        r = strata_bench.chat(a.url, "", [{"role": "user", "content": prompt}], a.max_tokens)
        d = strata_bench.digest(r)
        observed = d.get("prompt_tokens")
        status = "ok"
        if not d.get("ok"):
            status = "failed"
        elif observed is None:
            status = "context-unknown"
        elif observed < a.context - a.context_tolerance:
            status = "context-mismatch"
        rec = {"mode": a.mode, "requested_context": a.context, "observed_context_tokens": observed,
               "width": a.width, "prompt_id": a.prompt, "seed": SEED, "repeat": rep,
               "status": status, "config_sha": sha, "identity": props_before,
               "decode_tps_source": d.get("decode_tps_source"), "prompt_tps_source": d.get("prompt_tps_source")}
        for m in METRICS:
            v = d.get(m)
            if isinstance(v, bool) or (v is not None and (isinstance(v, (int, float)) and not math.isfinite(float(v)))):
                rec[m], rec[f"{m}_reason"] = None, f"non-finite/bool refused ({v!r})"
            elif v is None:
                rec[m], rec[f"{m}_reason"] = None, NO_METRIC
            else:
                rec[m] = float(v)
        (runs / f"{a.mode}_obs{observed}_w{a.width}_{a.prompt}_r{rep}.json").write_text(
            json.dumps(rec, indent=2) + "\n")
        if status != "ok":
            failures += 1
            print(f"run {rep}: {status} (recorded, EXCLUDED from statistics) observed_prompt_tokens={observed}")
    props_after = identity_snapshot(a.url)
    if props_after != props_before:
        print(f"FAIL: /props identity changed mid-batch: {props_before} -> {props_after}")
        failures += 1
    print(f"recorded {a.repeats} runs under {runs}; grouped summary NEVER mixes configs "
          f"(config_sha {sha} is part of the group key); {failures} run(s) excluded.")
    return 1 if failures else 0


def grouped_summary(runs_dir: pathlib.Path) -> int:
    groups: dict[tuple, list[dict]] = {}
    excluded = []
    for f in sorted(runs_dir.glob("*.json")):
        rec = json.loads(f.read_text())
        if rec.get("status") != "ok":
            excluded.append({"file": f.name, "status": rec.get("status")})
            continue
        key = (rec["mode"], rec["observed_context_tokens"], rec["width"], rec["prompt_id"],
               rec["seed"], rec.get("config_sha"))
        groups.setdefault(key, []).append(rec)
    out = {}
    for key, recs in groups.items():
        g = {"n": len(recs)}
        for m in METRICS:
            vals = [r[m] for r in recs if r.get(m) is not None]
            g[m] = summarize(vals) if vals else None
            if not vals:
                g[f"{m}_reason"] = NO_METRIC
        out["/".join(str(x) for x in key)] = g
    print(json.dumps({"groups": out, "excluded_runs": excluded,
                      "aggregation": "NONE across modes/observed-contexts/widths/configs - "
                                     "groups stay separate"}, indent=2))
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
    for bad in ([1.0, float("nan")], [True, 2.0], []):
        try:
            summarize(bad)
            check(False, f"summarize accepted {bad!r} (must refuse)")
        except ValueError:
            pass
    dflash_ok = ["--dflash", "A.gguf", "--spec", "4", "--dflash-block", "3", "--suffix-draft", "0", "--expert-cache", "auto"]
    check(validate_mode_flags("dflash_w3", 3, dflash_ok) is None, "label dflash_w3 matches effective flags (suffix off)")
    check(validate_mode_flags("dflash_w3", 3, ["--dflash", "A.gguf", "--spec", "4", "--dflash-block", "3"]) is not None,
          "dflash_w3 REFUSED when suffix lookup is still ON (drafter effect not isolated)")
    check(validate_mode_flags("dflash_w3", 3, ["--dflash", "A.gguf", "--spec", "6", "--dflash-block", "3", "--suffix-draft", "0"]) is not None,
          "dflash_w3 REFUSED when --spec is 6 (spec-1 != width)")
    prod_like = ["--spec", "6", "--spec-min-p", "0.7", "--expert-cache", "auto"]   # verbatim-style prod args
    check(validate_mode_flags("suffix_lookup", 3, prod_like) is None,
          "suffix_lookup label = verbatim prod args (suffix_draft DEFAULT 3 is ACTIVE - generate.cpp:778)")
    check(validate_mode_flags("target_only", 3, prod_like) is not None,
          "target_only REFUSED for verbatim prod args - they are a suffix-LOOKUP baseline, not pure AR (the mislabel fix)")
    ar_ok = ["--spec", "2", "--suffix-draft", "0", "--lookup-chain", "0", "--expert-cache", "auto"]
    check(validate_mode_flags("target_only", 3, ar_ok) is None and validate_mode_flags("old_target_only", 3, ar_ok) is None,
          "pure-AR labels require --suffix-draft 0 --lookup-chain 0 --spec 2 (native-pack floor)")
    mtp_ok = ["--mtp", integ.RT_DIR, "--spec", "6", "--spec-min-p", "0.7", "--suffix-draft", "0"]
    check(validate_mode_flags("mtp", 3, mtp_ok) is None, "mtp label verified against the READY-RT dir + pure-MTP flags")
    check(validate_mode_flags("mtp", 3, ["--mtp", "/home/user/ai/ai/mtp-Qwen3.8-Flash-Next-Q4_K_M.gguf",
                                         "--spec", "6", "--suffix-draft", "0"]) is not None,
          "mtp REFUSED for the lama-format GGUF (not Strata-loadable) / invented paths")
    check(validate_mode_flags("mtp", 3, ["--mtp", integ.RT_DIR, "--spec", "6", "--suffix-draft", "3"]) is not None,
          "mtp REFUSED when suffix is mixed in (--suffix-draft 3)")
    check(validate_mode_flags("suffix_lookup", 3, ["--spec", "1", "--suffix-draft", "0", "--lookup-chain", "0"]) is not None,
          "suffix_lookup REFUSED for a suffix-off config (that config is pure AR)")
    check(validate_mode_flags("prod_reference", 3, ["--expert-cache", "auto", "--dflash", "A.gguf"]) is not None,
          "prod_reference label verified against a drafter-free prod config")
    prod = integ.load_prod_config()
    modes = plan_modes(prod)
    check(modes["prod_reference"]["kind"].startswith("SUFFIX LOOKUP"),
          "prod_reference labeled SUFFIX LOOKUP (observed source + runtime logs), not target-only, not MTP")
    check(modes["new_mtp"]["kind"] == "READY-RT (config valid; runtime NOTRUN)",
          "new_mtp = READY-RT (real loader dir; metadata-validated; GPU load/Swift acceptance NOTRUN)")
    gate = correctness_gate(8192)
    check(gate[0]["pin"].startswith("--expert-cache 8192") and "auto" not in gate[0]["pin"],
          "correctness gate pins an EXPLICIT equal --expert-cache slot count (auto is not a pin)")
    check(all("STRATA_IQ_MT_MIN=1" in g["env"] for g in gate), "greedy gates carry the bit-for-bit env pin")
    r = integ.guard_execute("http://127.0.0.1:8009", True)
    check(r is not None, "bench guard: production port refused outright")
    print(f"contexts {CONTEXTS} (+{CONTEXTS_OPTIONAL}); context acceptance = engine-OBSERVED "
          f"prompt_tokens >= requested - tol (default 256); a short prompt with context>4096 is "
          f"REFUSED; seed {SEED}; group key includes seed + config sha. Metrics from strata_bench.digest "
          "with source labels; long contexts require --prompt-file.")
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
    ap.add_argument("--identity-config", default=None,
                    help="REQUIRED for --execute: the server config whose flags the label claims")
    ap.add_argument("--mode", default="dflash_w3")
    ap.add_argument("--width", type=int, default=3)
    ap.add_argument("--context", type=int, default=16384)
    ap.add_argument("--context-tolerance", type=int, default=256)
    ap.add_argument("--prompt", choices=list(PROMPTS), default="short")
    ap.add_argument("--prompt-file", default=None, help="long fixture for long contexts")
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
        if a.identity_config is None:
            print("REFUSED: --execute requires --identity-config (the mode/width label must be "
                  "verified against the server config's effective flags, and its sha256 joins the group key)")
            return 2
        return execute_measure(a)
    prod = integ.load_prod_config()
    plan = {"modes": plan_modes(prod), "widths": WIDTHS, "width_semantics": "w = min(--dflash-block, --spec-1) (5500)",
            "contexts": CONTEXTS, "contexts_optional": CONTEXTS_OPTIONAL, "prompts": list(PROMPTS),
            "seed": SEED, "metrics": METRICS, "metric_source": "strata_bench.chat()+digest() - engine numbers with source labels",
            "metric_null": NO_METRIC,
            "context_integrity": "records store engine-OBSERVED prompt_tokens; requested context is a label; "
                                 "observed < requested - tolerance -> context-mismatch, excluded, nonzero exit; "
                                 "context>4096 without --prompt-file is REFUSED",
            "identity": "--identity-config REQUIRED; label vs effective flags verified; config sha256 in the "
                        "group key; /props snapshot (model_alias/model_path/build_info) equal before and after",
            "grouping": "per (mode, observed_context, width, prompt, seed, config_sha); NEVER one aggregate number",
            "correctness_gate": correctness_gate(a.cache_slots) if a.cache_slots > 0
            else [{"need": "explicit --cache-slots N (equal fixed --expert-cache slots) for the greedy gate; "
                           "benchmark mode keeps --expert-cache auto unchanged"}],
            "repetitions": "medians/spread via --report DIR from recorded per-run JSONs; dry plans report no results"}
    print(json.dumps(plan, indent=2))
    print("plan-only: no GPU, no server touched, no production mutation")
    return 0


if __name__ == "__main__":
    sys.exit(main())

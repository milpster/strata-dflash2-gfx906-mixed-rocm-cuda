#!/usr/bin/env python3
"""Read-only final audit for capture gpu-residency-watch-20261118-235841-1238437.

Re-runs the production parser bench/ab-v0210/rpcprof/champions_log.parse_window over
the sealed engine-increment.log and prints the 26 completed-request rows plus the
ranges the closeout records. It mutates nothing: it only reads the raw capture and
prints. Exit 0 only when every recorded claim reproduces.

Usage (from repo root):
  python3 bench/ab-v0210/rpcprof/out/gpu-residency-watch-20261118-235841-1238437/audit_final.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RPCPROF = HERE.parent.parent  # bench/ab-v0210/rpcprof
sys.path.insert(0, str(RPCPROF))

from champions_log import parse_window  # noqa: E402

# Ranges recorded by the reviewed closeout; audit fails loudly on any drift.
EXPECT = {
    "rows": 26,
    "tg": (10.7, 25.4),
    "pp": (9.6, 233.5),
    "generated": (116, 2908),
    "generated_total": 23041,
    "context": (18645, 73198),
    "reused": (0, 70051),
    "read": (17, 18645),
}

# Source byte window the raw increment must reproduce.
SRC_FROM, SRC_TO = 4470351, 4507573
TARGET_PCI = ("0000:04:00.0", "0000:09:00.0", "0000:0a:00.0")


def check_samples() -> int:
    """Verify the recorded sample-level coverage and gauge claims against samples.jsonl."""
    import json

    samples = [json.loads(l) for l in (HERE / "samples.jsonl").read_text().splitlines() if l.strip()]
    print(f"\n== samples.jsonl ==")
    print(f"n_samples={len(samples)}")

    elapsed = [s["elapsed_s"] for s in samples]
    deltas = [round(b - a, 3) for a, b in zip(elapsed, elapsed[1:])]
    print(f"elapsed_s first={elapsed[0]} last={elapsed[-1]}")
    print(f"tick_deltas min={min(deltas)} max={max(deltas)} count={len(deltas)}")

    # Canonical identity retained throughout.
    initial = samples[0]["identity"]
    ids = {json.dumps(s["identity_after"], sort_keys=True) for s in samples}
    valid_all = all(s["identity_valid"] and s["identity_error"] is None for s in samples)
    print(f"distinct_identity_after={len(ids)} matches_initial={ids == {json.dumps(initial, sort_keys=True)}} "
          f"identity_valid_all={valid_all} invalid_count={sum(1 for s in samples if not s['identity_valid'])}")

    # Per-target-client gauges.
    client_stats: dict[str, dict] = {}
    nonzero_evicted = 0
    null_gauge_entries = 0
    client_entries = 0
    for s in samples:
        for c in s["drm"]["clients"]:
            if c["pci"] not in TARGET_PCI:
                continue
            client_entries += 1
            st = client_stats.setdefault(c["pci"], {"rvram": set(), "rgtt": set(), "ev": set(),
                                                     "req_eq_res": True, "client_id": c["client_id"],
                                                     "null": 0})
            for key in ("requested_vram_bytes", "evicted_bytes", "resident_vram_bytes", "resident_gtt_bytes"):
                if c[key] is None:
                    st["null"] += 1
                    null_gauge_entries += 1
            if None not in (c["requested_vram_bytes"], c["resident_vram_bytes"]) and c["requested_vram_bytes"] != c["resident_vram_bytes"]:
                st["req_eq_res"] = False
            if c["resident_vram_bytes"] is not None:
                st["rvram"].add(c["resident_vram_bytes"])
            if c["resident_gtt_bytes"] is not None:
                st["rgtt"].add(c["resident_gtt_bytes"])
            if c["evicted_bytes"]:
                nonzero_evicted += 1
            st["ev"].add(c["evicted_bytes"])
    print(f"target_client_entries={client_entries} (expected {len(samples)*len(TARGET_PCI)})")
    print(f"nonzero_evicted_entries={nonzero_evicted} null_target_gauge_entries={null_gauge_entries}")
    for pci in TARGET_PCI:
        st = client_stats[pci]
        rv = sorted(st["rvram"])
        print(f"  client {st['client_id']} {pci}: client_id={st['client_id']} "
              f"req==res={st['req_eq_res']} resident_vram={rv[0]}..{rv[-1]} "
              f"resident_gtt_values={sorted(st['rgtt'])} evicted_values={sorted(st['ev'])} null={st['null']}")

    # Per-target device gauges from the gpu list.
    dev_stats: dict[str, dict] = {}
    for s in samples:
        for g in s["gpu"]:
            if g["pci"] not in TARGET_PCI:
                continue
            st = dev_stats.setdefault(g["pci"], {"vram": set(), "gtt": set()})
            st["vram"].add(g["vram_used_bytes"])
            st["gtt"].add(g["gtt_used_bytes"])
    for pci in TARGET_PCI:
        st = dev_stats[pci]
        vv = sorted(x for x in st["vram"] if x is not None)
        print(f"  device {pci}: vram_used={vv[0]}..{vv[-1]} gtt_used_values={sorted(x for x in st['gtt'] if x is not None)}")

    # Error counters: the recorded 20 "unavailable other-node" entries per sample.
    err_counts = {len(s["gpu_errors"]) for s in samples}
    target_reffing = [e for s in samples for e in s["gpu_errors"]
                      if any(p in e for p in TARGET_PCI) or any(c in e for c in ("card2/", "card3/", "card4/"))]
    prefixes = sorted({e.split(":")[0] for s in samples for e in s["gpu_errors"]})
    print(f"gpu_errors_per_sample={sorted(err_counts)} count_referencing_targets={len(target_reffing)}")
    print(f"distinct_error_prefixes={len(prefixes)}")

    # Log capture chain.
    chain_ok = True
    new_sum = 0
    backlogs = []
    resets = []
    log_errors = []
    prompt_total = 0
    prev_to = None
    prev_cap = None
    for s in samples:
        lg = s["log"]
        new_sum += lg["new_bytes"]
        backlogs.append(lg["backlog_bytes"])
        resets.append(lg["reset"])
        if lg["error"]:
            log_errors.append(lg["error"])
        prompt_total += len(lg["prompt_lines"])
        if prev_to is not None and lg["from"] != prev_to:
            chain_ok = False
        if prev_cap is not None and lg["capture_from"] != prev_cap:
            chain_ok = False
        prev_to = lg["to"]
        prev_cap = lg["capture_to"]
    first = samples[0]["log"]
    last = samples[-1]["log"]
    print(f"log_chain_contiguous={chain_ok} src_from={first['from']} src_to={last['to']} "
          f"capture_from={first['capture_from']} capture_to={last['capture_to']}")
    print(f"new_bytes_sum={new_sum} backlog_max={max(backlogs)} resets_nonnull={sum(1 for r in resets if r is not None)} "
          f"log_errors={len(log_errors)} prompt_lines_total={prompt_total}")

    checks = [
        ("n_samples_121", len(samples) == 121),
        ("elapsed_starts_0", elapsed[0] == 0.0),
        ("elapsed_reaches_3600", abs(elapsed[-1] - 3600.0) < 1e-3),
        ("ticks_30s", all(29.999 <= d <= 30.001 for d in deltas)),
        ("no_dropped_ticks", len(deltas) == 120),
        ("identity_unique", ids == {json.dumps(initial, sort_keys=True)}),
        ("identity_all_valid", valid_all),
        ("client_entries_363", client_entries == 363),
        ("no_nonzero_evicted", nonzero_evicted == 0),
        ("no_null_target_gauge", null_gauge_entries == 0),
        ("c752_req_eq_res", client_stats["0000:04:00.0"]["req_eq_res"]),
        ("c752_rvram", min(client_stats["0000:04:00.0"]["rvram"]) == 17143697408
            and max(client_stats["0000:04:00.0"]["rvram"]) == 17144008704),
        ("c752_gtt_2166784", client_stats["0000:04:00.0"]["rgtt"] == {2166784}),
        ("c753_rvram", min(client_stats["0000:09:00.0"]["rvram"]) == 16857034752
            and max(client_stats["0000:09:00.0"]["rvram"]) == 16857456640),
        ("c753_gtt_2166784", client_stats["0000:09:00.0"]["rgtt"] == {2166784}),
        ("c754_resident_618496", client_stats["0000:0a:00.0"]["rvram"] == {618496}),
        ("c754_gtt_2138112", client_stats["0000:0a:00.0"]["rgtt"] == {2138112}),
        ("dev04_vram", min(dev_stats["0000:04:00.0"]["vram"]) == 17154547712
            and max(dev_stats["0000:04:00.0"]["vram"]) == 17154859008),
        ("dev04_gtt_13737984", dev_stats["0000:04:00.0"]["gtt"] == {13737984}),
        ("dev09_vram", min(dev_stats["0000:09:00.0"]["vram"]) == 16972861440
            and max(dev_stats["0000:09:00.0"]["vram"]) == 16973283328),
        ("dev09_gtt_13606912", dev_stats["0000:09:00.0"]["gtt"] == {13606912}),
        ("errors_20_per_sample", err_counts == {20}),
        ("errors_no_target_ref", len(target_reffing) == 0),
        ("src_range_matches", (first["from"], last["to"]) == (SRC_FROM, SRC_TO)),
        ("capture_to_37222", last["capture_to"] == 37222),
        ("new_sum_37222", new_sum == 37222),
        ("no_backlog", max(backlogs) == 0),
        ("no_resets", sum(1 for r in resets if r is not None) == 0),
        ("no_log_errors", len(log_errors) == 0),
        ("prompt_total_26", prompt_total == 26),
    ]
    failed = [name for name, ok in checks if not ok]
    print(f"\nSAMPLE_CHECKS={len(checks) - len(failed)}/{len(checks)}")
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if failed:
        print(f"SAMPLE_MISMATCH={','.join(failed)}")
        return 1
    print("SAMPLE_AUDIT=OK")
    return 0


def main() -> int:
    blob = (HERE / "engine-increment.log").read_bytes()
    passes = parse_window(blob)
    print(f"parsed_prompt_rows={len(passes)} (expected {EXPECT['rows']})")

    tg = [p.tg_tps for p in passes]
    pp = [p.pp_tps for p in passes]
    gen = [p.generated for p in passes]
    ctx = [p.tokens for p in passes]
    reused = [p.reused for p in passes]
    read = [p.read for p in passes]
    timeouts = sum(p.timeouts for p in passes)
    fallbacks = sum(p.fallbacks for p in passes)
    zero_gen = sum(1 for g in gen if g == 0)

    print(f"TG_tok_s min={min(tg):.1f} max={max(tg):.1f} unweighted_mean_of_row_rates={sum(tg)/len(tg):.1f}")
    print(f"PP_tok_s min={min(pp):.1f} max={max(pp):.1f} unweighted_mean_of_row_rates={sum(pp)/len(pp):.1f}")
    print(f"generated min={min(gen)} max={max(gen)} total={sum(gen)}")
    print(f"context_tokens min={min(ctx)} max={max(ctx)}")
    print(f"reused min={min(reused)} max={max(reused)}")
    print(f"read min={min(read)} max={max(read)}")
    print(f"timeouts_sum={timeouts} fallbacks_sum={fallbacks} zero_generated_rows={zero_gen}")

    print("\n-- per-row (PPtok/s  TGtok/s  ctx  reused  read  gen) --")
    for p in passes:
        print(f"  pp={p.pp_tps:6.1f} tg={p.tg_tps:5.1f} ctx={p.tokens:6d} "
              f"reused={p.reused:6d} read={p.read:6d} gen={p.generated:5d} "
              f"to={p.timeouts} fb={p.fallbacks}")

    checks = [
        ("rows", len(passes) == EXPECT["rows"]),
        ("tg_min", round(min(tg), 1) == EXPECT["tg"][0]),
        ("tg_max", round(max(tg), 1) == EXPECT["tg"][1]),
        ("pp_min", round(min(pp), 1) == EXPECT["pp"][0]),
        ("pp_max", round(max(pp), 1) == EXPECT["pp"][1]),
        ("gen_min", min(gen) == EXPECT["generated"][0]),
        ("gen_max", max(gen) == EXPECT["generated"][1]),
        ("gen_total", sum(gen) == EXPECT["generated_total"]),
        ("ctx_min", min(ctx) == EXPECT["context"][0]),
        ("ctx_max", max(ctx) == EXPECT["context"][1]),
        ("reused_min", min(reused) == EXPECT["reused"][0]),
        ("reused_max", max(reused) == EXPECT["reused"][1]),
        ("read_min", min(read) == EXPECT["read"][0]),
        ("read_max", max(read) == EXPECT["read"][1]),
        ("no_timeouts", timeouts == 0),
        ("no_fallbacks", fallbacks == 0),
        ("no_zero_generated", zero_gen == 0),
    ]
    failed = [name for name, ok in checks if not ok]
    print(f"\nCHECKS={len(checks) - len(failed)}/{len(checks)}")
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if failed:
        print(f"MISMATCH={','.join(failed)}")
    rate_ok = not failed
    sample_ok = check_samples() == 0
    if rate_ok and sample_ok:
        print("\nAUDIT=OK")
        return 0
    print("\nAUDIT=FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Claim-path breakdown from STRATA_RPC_PROFILE helper logs - ranks the transport levers.

Parses `rpcprofile: serve tier=p layer=N rows=R queue_us=Q lock_us=L reply_us=T rows_kb=K`
lines from ab9 helper logs and aggregates per prompt (leg), then prices each lever:

  batching   - the per-claim fixed overhead x claims (what fewer round-trips save)
  wire       - sum(rows_kb) / the measured stream rate (the floor the wire lever hides)
  overlap    - the reply/lock spans that streaming overlap could hide
  begin      - begin-phase spans (engine log) for the begin-overlap lever

Usage: claim_breakdown.py <helper.log> [<helper.log> ...] [--gbps 4.42]
"""
import argparse
import re
import statistics as st

SERVE = re.compile(r"rpcprofile: serve tier=(\w) layer=(\d+) rows=(\d+) queue_us=(\d+) "
                   r"lock_us=(\d+) reply_us=(\d+) rows_kb=(\d+)")
BEGIN = re.compile(r"rpcprofile: begin .*?us=(\d+)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--gbps", type=float, default=4.42, help="measured stream rate")
    a = ap.parse_args()

    claims = []  # (lock_us, reply_us, queue_us, rows_kb)
    begins = []
    for path in a.logs:
        with open(path, errors="replace") as f:
            for line in f:
                m = SERVE.search(line)
                if m:
                    tier, _layer, rows, q, lock, reply, kb = m.groups()
                    if tier == "p":   # prompt-tier claims only
                        claims.append((int(lock), int(reply), int(q), int(kb)))
                    continue
                m = BEGIN.search(line)
                if m:
                    begins.append(int(m.group(1)))

    if not claims:
        print("no prompt-tier rpcprofile claims found")
        return 1
    locks = [c[0] for c in claims]
    replies = [c[1] for c in claims]
    kbs = [c[3] for c in claims]
    n = len(claims)
    wire_us = sum(kbs) / (a.gbps * 1024 * 1024) * 1e6
    med_lock = st.median(locks)
    # fixed overhead per claim ~ the span floor: claims with the least work show it best
    floor = st.median(sorted(locks)[: max(1, n // 10)])
    print(f"claims={n}  rows_kb_sum={sum(kbs)/1024:.0f} MiB")
    print(f"lock_us  : sum={sum(locks)/1e6:.3f}s med={med_lock:.0f} p10-floor={floor:.0f}")
    print(f"reply_us : sum={sum(replies)/1e6:.3f}s med={st.median(replies):.0f}")
    print(f"queue_us : sum={sum(c[2] for c in claims)/1e6:.3f}s")
    print(f"wire est : {wire_us/1e6:.3f}s @ {a.gbps} GB/s ({100*wire_us/sum(locks):.0f}% of lock)")
    if begins:
        print(f"begin_us : n={len(begins)} sum={sum(begins)/1e6:.3f}s med={st.median(begins):.0f}")
    print("--- lever prizes (per prompt = sum/legs; assumes 1 prompt per leg) ---")
    # batching prize = each ELIMINATED round-trip saves one fixed floor (per-row work
    # stays, just packed into bigger claims): (n - exchanges) x floor, upper bound
    print(f"batching x8 : <= {(n-8)*floor/1e6:.3f}s saved (({n}-8) x floor {floor:.0f}us; total fixed {n*floor/1e6:.2f}s)")
    print(f"wire overlap: ~{wire_us/1e6:.3f}s max (hide wire behind compute)")
    print(f"begin overlap: ~{sum(begins)/1e6:.3f}s max")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

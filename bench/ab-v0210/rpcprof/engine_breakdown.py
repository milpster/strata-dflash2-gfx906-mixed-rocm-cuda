#!/usr/bin/env python3
"""Engine-side per-layer span breakdown from STRATA_ENGINE_SPANS engine logs.

Reads the engine's per-claim lines and the engine's span line:

  rpcprofile: claim layer=N rows=R sel=S d2h_us=D begin_us=B reply_us=T img_kb=I reply_kb=K fallback=F
  rpcprofile: claim layer=N rows=R sel=S d2h_us=D begin_us=B reply_us=T img_kb=I reply_kb=K fallback=F img_fmt=G
  rpcprofile: span layer=N pipe=P layer_us=L attn_us=A router_us=C claim_us=M expert_us=E \
              wait_us=W apply_us=Y helper_us=H rest_us=Z

The segments (attn, router, claim, expert, wait, apply, rest) do not overlap and sum to layer_us -
the engine's own wall time for that MoE layer of the batched walk.  helper_us is an OVERLAY, the
round trip the layer feels (send complete -> the claimed block staged), so it is not part of the
sum.  `wait` is the engine blocked on the helper: the TRUE critical-path exposure, not busy time.
`img_kb` is the image the frame CARRIED (img_fmt 0 = the engine's q8_1 blocks, 1 = the fp32 rows,
2 = Q8_K), so the wire estimate is right for every format; logs without img_fmt are read as before.
`pipe=1` names the M6 worker lane (claim = the publish, apply = the event chain, wait = the
worker's outcome wait).  A log without span lines is summarized from the claim lines alone, so the
script also works on the windows already measured.

Usage: engine_breakdown.py <engine.log> [<engine.log> ...] [--gbps 4.42]
"""
import argparse
import re
import statistics as st

CLAIM = re.compile(r"rpcprofile: claim layer=(\d+) rows=(\d+) sel=(\d+) d2h_us=(\d+) begin_us=(\d+) "
                   r"reply_us=(\d+) img_kb=(\d+) reply_kb=(\d+) fallback=(\d+)")
FMT = re.compile(r"img_fmt=(\d+)")   # 0 engine q8_1, 1 fp32, 2 Q8_K - absent on pre-img_fmt logs
SPAN = re.compile(r"rpcprofile: span layer=(\d+) pipe=(\d+) layer_us=(\d+) attn_us=(\d+) "
                  r"router_us=(\d+) claim_us=(\d+) expert_us=(\d+) wait_us=(\d+) apply_us=(\d+) "
                  r"helper_us=(\d+) rest_us=(\d+)")
# the span tuple is [layer_us, attn, router, claim, expert, wait, apply, helper, rest] - the seven
# non-overlapping segments plus the helper overlay, in the line's own order
SPAN_TUPLE = ["layer", "attn", "router", "claim", "expert", "wait", "apply", "helper", "rest"]
SEG = [1, 2, 3, 4, 5, 6, 8]   # the tuple indices of the non-overlapping segments (helper is an overlay)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--gbps", type=float, default=4.42, help="measured stream rate for the wire estimate")
    a = ap.parse_args()

    claims, spans = [], []   # claim: d2h,begin,reply,img_kb,reply_kb,fallback,rows,sel; span: see SPAN_NAMES
    for path in a.logs:
        with open(path, errors="replace") as f:
            for line in f:
                m = SPAN.search(line)
                if m:
                    spans.append([int(v) for v in m.groups()[2:]])
                    continue
                c = CLAIM.search(line)
                if c:
                    g = [int(v) for v in c.groups()]
                    f = FMT.search(line)
                    claims.append(g + [int(f.group(1)) if f else -1])   # last = img_fmt, -1 when absent

    print(f"claim lines={len(claims)}  span lines={len(spans)}")
    if not claims and not spans:
        return

    if claims:
        d2h = [c[3] for c in claims]
        beg = [c[4] for c in claims]
        rep = [c[5] for c in claims]
        img = sum(c[6] for c in claims)
        rk = sum(c[7] for c in claims)
        fb = sum(c[8] for c in claims)
        print(f"d2h_us   : sum={sum(d2h)/1e6:.3f}s med={st.median(d2h):.0f}us")
        print(f"begin_us : sum={sum(beg)/1e6:.3f}s med={st.median(beg):.0f}us")
        print(f"reply_us : sum={sum(rep)/1e6:.3f}s med={st.median(rep):.0f}us")
        print(f"wire     : image {img/1024:.1f} MiB reply {rk/1024:.1f} MiB "
              f"(send {img*1024/a.gbps/1e9:.3f}s + reply {rk*1024/a.gbps/1e9:.3f}s @ {a.gbps} GB/s)")
        print(f"fallbacks: {fb} of {len(claims)} claims")
        fmts = [c[9] for c in claims if len(c) > 9 and c[9] >= 0]
        if fmts:   # img_kb is the image the frame CARRIED, so the wire math is right per format
            print("img_fmt    : " + ", ".join(f"{n}x{sum(1 for f in fmts if f == n)}"
                                             for n in sorted(set(fmts))) +
                  " (0 engine q8_1, 1 fp32, 2 Q8_K)")

    if not spans:
        print("(no span lines: rerun the window with STRATA_ENGINE_SPANS=1; the claim-line fields above are it)")
        return

    layer = [s[0] for s in spans]
    tot = sum(layer)
    print(f"--- engine per-layer spans (n={len(spans)} claimed MoE layers; % of layer_us) ---")
    for i in range(1, len(SPAN_TUPLE)):
        vals = [s[i] for s in spans]
        tag = " (overlay, not in the sum)" if SPAN_TUPLE[i] == "helper" else ""
        print(f"{SPAN_TUPLE[i]:7s}: sum={sum(vals)/1e6:6.3f}s med={st.median(vals):8.0f}us "
              f"share={100*sum(vals)/tot if tot else 0:5.1f}%{tag}")
    seg_sum = sum(sum(s[i] for i in SEG) for s in spans)
    wait_sum = sum(s[SPAN_TUPLE.index("wait")] for s in spans)
    rt_sum = sum(s[SPAN_TUPLE.index("helper")] for s in spans)
    print(f"layer_us : sum={tot/1e6:.3f}s med={st.median(layer):.0f}us; segments sum {seg_sum/1e6:.3f}s "
          f"(gap {100*(tot-seg_sum)/tot if tot else 0:.1f}% = the layer work outside the named spans)")
    print(f"helper WAIT share of the layer cycle: {100*wait_sum/tot:.1f}% (the engine blocked on the helper); "
          f"the round trip covers {100*rt_sum/tot:.1f}% of it, most of that overlapping the primary's own "
          f"expert work")
    print("(pipe=1 = the M6 worker lane: claim is the publish, apply is the event chain, "
          "wait is the worker's outcome wait)")


if __name__ == "__main__":
    main()

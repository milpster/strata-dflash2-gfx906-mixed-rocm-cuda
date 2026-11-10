#!/usr/bin/env python3
"""Engine-side per-layer span breakdown from STRATA_ENGINE_SPANS engine logs.

Reads the engine's per-claim lines and the engine's span line:

  rpcprofile: claim layer=N rows=R sel=S d2h_us=D begin_us=B reply_us=T img_kb=I reply_kb=K fallback=F img_fmt=G
  rpcprofile: span layer=N pipe=P layer_us=L attn_us=A router_us=C claim_us=M expert_us=E \
              wait_us=W apply_us=Y helper_us=H rest_us=Z [gate_us=Ga ids_us=I barrier_us=B group_us=Gr sel_us=Se]

The segments (attn, router, claim, expert, wait, apply, rest) do not overlap and sum to layer_us -
the engine's own wall time for that MoE layer of the batched walk.  helper_us is an OVERLAY, the
round trip the layer feels (send complete -> the claimed block staged), so it is not part of the
sum.  `wait` is the engine blocked on the helper: the TRUE critical-path exposure, not busy time.
`img_kb` is the image the frame CARRIED (img_fmt 0 = the engine's q8_1 blocks, 1 = the fp32 rows,
2 = Q8_K), so the wire estimate is right for every format; logs without img_fmt are read as before.
`pipe=1` names the M6 worker lane (claim = the publish, apply = the event chain, wait = the
worker's outcome wait).  A log without span lines is summarized from the claim lines alone, so the
script also works on the windows already measured.

The router span is a GPU drain point, not the router's math, so it is split further when the
appended sub-fields are present (they sum to router_us): gate = the gate GEMM + route kernel
ENQUEUES; ids = the shared-expert enqueues when inline + the routed ids' D2H; barrier = the
compute-stream sync (#579) where this layer's attention/gate/shared and the previous layer's
queued tails drain; group = the CPU grouping of T*k pairs, the CPU-share probes and the deferred
shared-expert enqueue; sel = the claim plan build.  Chunk groups (ascending layer runs) are
printed separately, which is the token-count scaling.

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
# the router span's own decomposition, appended after rest_us on newer logs (they sum to router_us)
SUB = re.compile(r"gate_us=(\d+) ids_us=(\d+) barrier_us=(\d+) group_us=(\d+) sel_us=(\d+)")
SUB_NAMES = ["gate", "ids", "barrier", "group", "sel"]
ROUTER_I, SUB_BASE = 2, 9   # router_us in the span tuple; the sub-spans start at tuple index 9


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--gbps", type=float, default=4.42, help="measured stream rate for the wire estimate")
    a = ap.parse_args()

    claims, spans, layers = [], [], []   # claim: d2h,begin,reply,img_kb,reply_kb,fallback,rows,sel; span: see SPAN_NAMES
    for path in a.logs:
        with open(path, errors="replace") as f:
            for line in f:
                m = SPAN.search(line)
                if m:
                    row = [int(v) for v in m.groups()[2:]]
                    sp = SUB.search(line)
                    spans.append(row + ([int(v) for v in sp.groups()] if sp else []))
                    layers.append(int(m.group(1)))
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
    subs_idx = [i for i, s in enumerate(spans) if len(s) >= SUB_BASE + len(SUB_NAMES)]
    subs = [spans[i] for i in subs_idx]
    if subs:
        tot_r = sum(s[ROUTER_I] for s in subs)
        print(f"--- router span decomposition (n={len(subs)}; % of router_us) ---")
        for j, name in enumerate(SUB_NAMES):
            vals = [s[SUB_BASE + j] for s in subs]
            print(f"{name:8s}: sum={sum(vals)/1e6:6.3f}s med={st.median(vals):8.0f}us "
                  f"share={100*sum(vals)/tot_r if tot_r else 0:5.1f}% of router "
                  f"({100*sum(vals)/tot if tot else 0:4.1f}% of layer)")
        print(f"(gate = the gate GEMM + route kernel enqueues; ids = shared-expert enqueues when inline "
              f"+ the routed ids' D2H; barrier = the compute-stream sync #579 where the GPU queue drains; "
              f"group = the CPU grouping + probes + deferred shared enqueue; sel = the claim plan build)")
        # chunk groups: ascending layer runs = one prompt chunk each -> the token-count scaling
        runs, cur = [], []
        for i in subs_idx:
            lay, s = layers[i], spans[i]
            if cur and lay <= cur[-1][0]:
                runs.append(cur)
                cur = []
            cur.append((lay, s))
        runs.append(cur)
        print(f"--- per chunk group (ascending layer runs; n={len(runs)}): medians in ms ---")
        for k, run in enumerate(runs):
            ss = [s for _, s in run]
            med = lambda xs: st.median(xs) / 1e3
            print(f"chunk{k}: n={len(ss):3d} layer={med([s[0] for s in ss]):7.1f} router={med([s[ROUTER_I] for s in ss]):6.1f} "
                  + " ".join(f"{SUB_NAMES[j]}={med([s[SUB_BASE + j] for s in ss]):5.1f}" for j in range(len(SUB_NAMES)))
                  + f" attn={med([s[1] for s in ss]):5.1f} expert={med([s[4] for s in ss]):5.1f} "
                  f"wait={med([s[5] for s in ss]):5.1f} claim={med([s[3] for s in ss]):5.1f}")
    print("(pipe=1 = the M6 worker lane: claim is the publish, apply is the event chain, "
          "wait is the worker's outcome wait)")


if __name__ == "__main__":
    main()

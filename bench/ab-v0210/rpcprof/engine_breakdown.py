#!/usr/bin/env python3
"""Engine-side per-layer / per-chunk span breakdown from STRATA_ENGINE_SPANS engine logs.

Lines read (a log may be missing any of the newer ones - it is summarized from what is there):

  rpcprofile: claim layer=N rows=R sel=S d2h_us=D begin_us=B reply_us=T img_kb=I reply_kb=K fallback=F img_fmt=G
  rpcprofile: span layer=N pipe=P layer_us=L attn_us=A router_us=C claim_us=M expert_us=E \
              wait_us=W apply_us=Y helper_us=H rest_us=Z [gate_us Ga ids_us Ib barrier_us Br group_us Gg sel_us Se] \
              [gpu=V claim=Q attn=X post_us=Po]
  rpcprofile: chunk gpu=V chunk=K tokens=T intake_us=I walk_us=W tail_us=Ta chunk_us=C
  rpcprofile: pass gpu=V tokens=T layers=L chunks=K pass_us=P

Layer spans: the segments (attn, router, claim, expert, wait, apply, rest) do not overlap and sum to
layer_us - the engine's own wall time for that MoE layer.  helper_us is an OVERLAY (the round trip
the layer feels), not part of the sum.  `wait` is the engine blocked on the helper.  The five
sub-spans (gate, ids, barrier, group, sel) sum to router_us; the barrier is the compute-stream sync
where the GPU queue drains.

L5 fields (appended, absent on older logs): every walked layer now emits a line, so
  claim=0 lines = the layers that never claim.  In a layer split these are the peer-served half
  (layers 32..47 with `--gpu 0,1 --split 32`), tagged with their attention class (`attn=qsa|gdn`).
  For them router_us runs from the router mark to the claim-plan start and `post_us` is the layer's
  whole remainder (its expert compute, the combine, the write-back) - that is the outside-layer gap,
  measured.  `gpu` separates the two passes' lines.
Chunk lines split one chunk's wall into intake (embeddings, PLE rows, the stream plan), the layer
walk, and the tail (issuer join, PLE land, hand-off).  The pass line is the whole run's wall.

Usage: engine_breakdown.py <engine.log> [<engine.log> ...] [--gbps 4.42] [--wall MS [--wall MS ...]]
  --wall repeats the leg walls (ms) to turn the totals into a gap-attribution table.
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
SUB = re.compile(r"gate_us=(\d+) ids_us=(\d+) barrier_us=(\d+) group_us=(\d+) sel_us=(\d+)")
META = re.compile(r"gpu=(\d+) claim=(\d+) attn=(\w+) post_us=(\d+)")
CHUNK = re.compile(r"rpcprofile: chunk gpu=(\d+) chunk=(\d+) tokens=(\d+) intake_us=(\d+) "
                   r"walk_us=(\d+) tail_us=(\d+) chunk_us=(\d+)")
PASS = re.compile(r"rpcprofile: pass gpu=(\d+) tokens=(\d+) layers=(\d+) chunks=(\d+) pass_us=(\d+)")
# the span tuple is [layer_us, attn, router, claim, expert, wait, apply, helper, rest] - the seven
# non-overlapping segments plus the helper overlay, in the line's own order
SPAN_TUPLE = ["layer", "attn", "router", "claim", "expert", "wait", "apply", "helper", "rest"]
SEG = [1, 2, 3, 4, 5, 6, 8]   # the tuple indices of the non-overlapping segments (helper is an overlay)
SUB_NAMES = ["gate", "ids", "barrier", "group", "sel"]
ROUTER_I, SUB_BASE = 2, 9   # router_us in the span tuple; the sub-spans start at tuple index 9


def table(rows, idx, tot, label):
    for i in idx:
        vals = [r[i] for r in rows]
        tag = " (overlay, not in the sum)" if SPAN_TUPLE[i] == "helper" else ""
        print(f"{SPAN_TUPLE[i]:7s}: sum={sum(vals)/1e6:6.3f}s med={st.median(vals):8.0f}us "
              f"share={100*sum(vals)/tot if tot else 0:5.1f}%{tag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--gbps", type=float, default=4.42, help="measured stream rate for the wire estimate")
    ap.add_argument("--wall", type=float, action="append", default=[],
                    help="leg wall in ms (repeat per leg) for the gap attribution")
    a = ap.parse_args()

    claims, spans, layers, metas, chunks, passes = [], [], [], [], [], []
    s_ly = s_walk = s_int = s_tail = s_ck = 0   # sums the gap table needs, whatever lines exist
    for path in a.logs:
        with open(path, errors="replace") as f:
            for line in f:
                m = SPAN.search(line)
                if m:
                    row = [int(v) for v in m.groups()[2:]]
                    sp = SUB.search(line)
                    spans.append(row + ([int(v) for v in sp.groups()] if sp else []))
                    layers.append(int(m.group(1)))
                    mt = META.search(line)
                    metas.append([int(mt.group(1)), int(mt.group(2)), mt.group(3), int(mt.group(4))] if mt else None)
                    continue
                c = CLAIM.search(line)
                if c:
                    g = [int(v) for v in c.groups()]
                    f2 = FMT.search(line)
                    claims.append(g + [int(f2.group(1)) if f2 else -1])
                    continue
                ck = CHUNK.search(line)
                if ck:
                    chunks.append([int(v) for v in ck.groups()])
                    continue
                ps = PASS.search(line)
                if ps:
                    passes.append([int(v) for v in ps.groups()])

    print(f"claim lines={len(claims)}  span lines={len(spans)}  chunk lines={len(chunks)}  pass lines={len(passes)}")
    if not claims and not spans and not chunks and not passes:
        return

    if claims:
        d2h = [c[3] for c in claims]
        beg = [c[4] for c in claims]
        rep = [c[5] for c in claims]
        img, rk, fb = sum(c[6] for c in claims), sum(c[7] for c in claims), sum(c[8] for c in claims)
        print(f"d2h_us   : sum={sum(d2h)/1e6:.3f}s med={st.median(d2h):.0f}us")
        print(f"begin_us : sum={sum(beg)/1e6:.3f}s med={st.median(beg):.0f}us")
        print(f"reply_us : sum={sum(rep)/1e6:.3f}s med={st.median(rep):.0f}us")
        print(f"wire     : image {img/1024:.1f} MiB reply {rk/1024:.1f} MiB "
              f"(send {img*1024/a.gbps/1e9:.3f}s + reply {rk*1024/a.gbps/1e9:.3f}s @ {a.gbps} GB/s)")
        print(f"fallbacks: {fb} of {len(claims)} claims")
        fmts = [c[9] for c in claims if len(c) > 9 and c[9] >= 0]
        if fmts:
            print("img_fmt    : " + ", ".join(f"{n}x{sum(1 for f in fmts if f == n)}"
                                             for n in sorted(set(fmts))) + " (0 engine q8_1, 1 fp32, 2 Q8_K)")

    if spans:
        claimed = [(i, s) for i, s in enumerate(spans) if metas[i] and metas[i][1] == 1]
        unclaimed = [(i, s) for i, s in enumerate(spans) if metas[i] and metas[i][1] == 0]
        legacy = [(i, s) for i, s in enumerate(spans) if metas[i] is None]
        for name, rows, is_unclaimed in (
                ("claiming layers (helper claims)", claimed, False),
                ("NON-claiming layers (the gap: the peer-served half of a split)", unclaimed, True),
                ("pre-L5 span lines", legacy, False)):
            if not rows:
                continue
            rs = [s for _, s in rows]
            tot = sum(r[0] for r in rs)
            print(f"--- {name}: n={len(rs)}; % of their layer_us ---")
            if is_unclaimed:
                post = [metas[i][3] for i, _ in rows]
                print(f"post_us  : sum={sum(post)/1e6:6.3f}s med={st.median(post):.0f}us "
                      f"share={100*sum(post)/tot if tot else 0:.1f}%  (the non-claiming layer's whole "
                      f"remainder: its expert compute + the combine + the write-back)")
            table(rs, list(range(1, len(SPAN_TUPLE))), tot, name)
            for c in sorted({metas[i][2] for i, _ in rows}):
                sub = [s for i, s in rows if metas[i][2] == c]
                print(f"  attn={c:4s}: n={len(sub):3d} layer med={st.median([r[0] for r in sub])/1e3:7.1f}ms "
                      f"router med={st.median([r[ROUTER_I] for r in sub])/1e3:6.1f}ms "
                      f"post med={st.median([metas[i][3] for i, s in rows if metas[i][2] == c])/1e3:6.1f}ms")
        subs = [s for s in spans if len(s) >= SUB_BASE + len(SUB_NAMES)]
        if subs:
            tot_r = sum(s[ROUTER_I] for s in subs)
            tot_l = sum(s[0] for s in subs)
            print(f"--- router span decomposition (n={len(subs)}; % of router_us / of layer_us) ---")
            for j, name in enumerate(SUB_NAMES):
                vals = [s[SUB_BASE + j] for s in subs]
                print(f"{name:8s}: sum={sum(vals)/1e6:6.3f}s med={st.median(vals):8.0f}us "
                      f"share={100*sum(vals)/tot_r if tot_r else 0:5.1f}% of router "
                      f"({100*sum(vals)/tot_l if tot_l else 0:4.1f}% of layer)")
        layer_all = [s[0] for s in spans]
        tot_all = sum(layer_all)
        s_ly = tot_all
        print(f"all span lines: n={len(spans)} layer_us sum={tot_all/1e6:.3f}s "
              f"med={st.median(layer_all)/1e3:.1f}ms")

    if chunks:
        print("--- chunk walls (intake = embeddings/PLE/stream plan; walk = the layer loop; "
              "tail = issuer join/PLE land/hand-off) ---")
        for c in chunks:
            gpu, k, tok, i, w, t, wall = c
            print(f"gpu{gpu} chunk{k} tokens={tok:6d}: chunk_us={wall/1e6:6.3f}s "
                  f"intake={i/1e3:7.1f}ms ({100*i/wall:4.1f}%) walk={w/1e6:5.2f}s ({100*w/wall:5.1f}%) "
                  f"tail={t/1e3:7.1f}ms ({100*t/wall:4.1f}%)")
        s_int = sum(c[3] for c in chunks)
        s_walk = sum(c[4] for c in chunks)
        s_tail = sum(c[5] for c in chunks)
        s_ck = sum(c[6] for c in chunks)
        s_ly = sum(s[0] for s in spans)
        print(f"chunk sums: intake={s_int/1e6:.3f}s walk={s_walk/1e6:.3f}s tail={s_tail/1e6:.3f}s "
              f"chunk_us={s_ck/1e6:.3f}s | layer_us sum={s_ly/1e6:.3f}s "
              f"(walk minus layers = {(s_walk-s_ly)/1e6:.3f}s: the layers' own tails, the prints)")

    if passes:
        # a layer split nests: the first stage's run_impl calls the next stage's run_impl per chunk
        # (prefill.cpp:6899), so the OUTER pass_us covers the nested ones - the largest is the whole
        # prompt processing; summing the pass lines would count the peer's wall twice.
        s_outer = max(p[4] for p in passes)
        by_gpu = {}
        for c in chunks:
            by_gpu[c[0]] = by_gpu.get(c[0], 0) + c[6]
        for p in passes:
            own = by_gpu.get(p[0], 0)
            print(f"pass gpu{p[0]}: tokens={p[1]:6d} layers={p[2]:3d} chunks={p[3]} "
                  f"pass_us={p[4]/1e6:6.3f}s own_chunks={own/1e6:6.3f}s setup/nested={(p[4]-own)/1e6:6.3f}s"
                  f"{'  (outer: nests the next stages)' if p[4] == s_outer else ''}")
        if a.wall:
            wall = sum(a.wall) * 1e3
            print(f"--- gap attribution vs the leg walls ({wall/1e6:.2f}s total) ---")
            for name, us in (("claimed+unclaimed layer_us", s_ly), ("chunk walk", s_walk),
                             ("chunk intake", s_int), ("chunk tail", s_tail),
                             ("chunk_us (all chunks, both stages)", s_ck),
                             ("pass_us (outer run, nests the stages)", s_outer)):
                print(f"{name:34s}: {us/1e6:6.3f}s = {100*us/wall:5.1f}% of wall")
            un = wall - s_outer
            print(f"{'unattributed (wall - outer pass)':34s}: {un/1e6:6.3f}s = {100*un/wall:5.1f}% of wall "
                  f"(the serve/sample/head residue: chunk walls start at pf_w0, pass starts at run_impl)")

    if not spans and not chunks:
        print("(no span/chunk lines: rerun the window with STRATA_ENGINE_SPANS=1)")


if __name__ == "__main__":
    main()

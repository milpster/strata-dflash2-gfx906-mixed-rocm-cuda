#!/usr/bin/env python3
# r10-verify-curve.py - round-10 drafter-collapse forensics (analysis only).
# Parses the byte-windowed engine-log records in rpcprof/out/*, extracts every
# "prompt ... N generated in ... ms, drafts accepted A of B" line, and derives
# per-gen spec-decode stats: verify steps = N - A, tok/verify = N/steps,
# prop/verify = B/steps, acceptance = A/B, read ms/tok, ms/verify = gen_ms/steps.
# Prints per arm/leg/gen with the drafter-state events (pair restore / cold rewind)
# seen in the same window. No GPU, no builds: pure log reading.
import json, re, os, sys

OUT = "/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out"

# Round-11 mode: STRATA_SPEC_TRACE serve traces (the serve loop writes
# "# request prompt N ..." at each request start and "R p T a sfx ms k match | probs(7)
# | drafts(8) | lookup k | committed a+1" per verify round - generate.cpp 21369/22180/22869).
# The curves the round-10 aggregates could not see: acceptance and ms/verify as a function of
# the verify index WITHIN a generation (cold-start recovery vs flat depression).
#   usage: r10-verify-curve.py --trace FILE [FILE ...]
# One request = one generation (one panel leg call), so requests map 1:1 to the gen rows above.
if len(sys.argv) > 1 and sys.argv[1] == "--trace":
    REQ = re.compile(r"^# request prompt (\d+) df2 (\d+) spec (\d+)")
    RND = re.compile(r"^R (\d+) (\d+) (\d+) (\d) ([\d.]+) (\d+) (\d+) \|")
    BUCKETS = [(1, 10), (11, 25), (26, 50), (51, 100), (101, 10 ** 9)]
    for path in sys.argv[2:]:
        print(f"== trace {path}")
        reqs = [[]]  # list of (idx, p, T, a, ms); a request begins at each "# request" line
        prompts = []
        with open(path, "r", errors="replace") as f:
            for line in f:
                m = REQ.match(line)
                if m:
                    prompts.append(int(m.group(1)))
                    if reqs[-1]:
                        reqs.append([])
                    continue
                m = RND.match(line)
                if m:
                    p, T, a, sfx, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), float(m.group(5))
                    reqs[-1].append((len(reqs[-1]) + 1, p, T, a, ms))
        print(f"{'req':>4} {'prompt':>7} {'rounds':>6} {'tok':>6} {'acc/off':>9} {'acc%':>5} "
              f"{'tok/V':>5} {'prop/V':>6} {'ms/V':>6} | per-bucket acc% (V) ms/V tok/V")
        for ri, rs in enumerate(reqs):
            if not rs:
                continue
            n_tok = sum(a + 1 for _, _, _, a, _ in rs)
            off = sum(T - 1 for _, _, T, _, _ in rs)
            acc = sum(a for _, _, _, a, _ in rs)
            ms = sum(ms for _, _, _, _, ms in rs)
            cur = []
            for lo, hi in BUCKETS:
                b = [r for r in rs if lo <= r[0] <= hi]
                if not b:
                    continue
                bo = sum(T - 1 for _, _, T, _, _ in b)
                ba = sum(a for _, _, _, a, _ in b)
                bm = sum(ms for _, _, _, _, ms in b)
                cur.append(f"{lo}-{hi if hi < 10**9 else '+'}: "
                           f"{100*ba/max(bo,1):.1f}({len(b)}) {bm/len(b):.1f} {(ba+len(b))/max(len(b),1):.2f}")
            print(f"{ri:>4} {prompts[ri] if ri < len(prompts) else 0:>7} {len(rs):>6} {n_tok:>6} "
                  f"{acc:>4}/{off:<4} {100*acc/max(off,1):>5.1f} {n_tok/len(rs):>5.2f} "
                  f"{off/len(rs):>6.2f} {ms/len(rs):>6.1f} | " + "  ".join(cur))
        print()
    sys.exit(0)

DIRS = sys.argv[1:] or ["ab9-v0227-iq3s-20261112", "ab9-v0226-iq3s-20261112",
                        "ab9-v0225-int8-20261112", "ab9-v0228-clean"]

GEN = re.compile(
    r"prompt (\d+) tokens = (\d+) reused \+ (\d+) read in (\d+) ms \(([\d.]+) tok/s\), "
    r"(\d+) generated in (\d+) ms \(([\d.]+) tok/s\), drafts accepted (\d+) of (\d+)")
EVENT = re.compile(r"(the draft ring no longer retains a rewind to (\d+)|"
                   r"the draft ring for the rewind to (\d+) restored from its pair|"
                   r"restored (\d+) tokens from disk|restored (\d+) tokens \((live|checkpoint)\))")

def window_text(path, a, b):
    with open(path, "rb") as f:
        f.seek(a)
        return f.read(b - a).replace(b"\0", b"").decode("utf-8", "replace")

def records(d):
    root = os.path.join(OUT, d)
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        cands = []
        if os.path.isdir(p):
            inner = os.path.join(p, name)
            if os.path.isfile(inner):
                cands.append(inner)
        elif name.endswith(".json"):
            cands.append(p)
        for c in cands:
            try:
                with open(c) as f:
                    j = json.load(f)
                if "elog" in j and "elog_start" in j:
                    yield j
            except Exception:
                pass

for d in DIRS:
    print(f"== {d}")
    print(f"{'arm':<12} {'leg':<22} {'gen':>3} {'reused':>7} {'read':>5} {'rd ms/tok':>9} "
          f"{'gen':>4} {'dec s':>6} {'t/s':>5} {'acc/off':>9} {'acc%':>5} {'steps':>5} "
          f"{'tok/V':>5} {'prop/V':>6} {'ms/V':>6}")
    for j in records(d):
        arm, leg = j.get("arm", "?"), j.get("leg", "?")
        txt = window_text(j["elog"], j["elog_start"], j["elog_end"])
        evs = []
        for m in EVENT.finditer(txt):
            if m.group(2): evs.append(f"cold-rewind@{m.group(2)}")
            elif m.group(3): evs.append(f"pair-restore@{m.group(3)}")
            elif m.group(4): evs.append(f"disk-restore@{m.group(4)}")
        gi = 0
        for m in GEN.finditer(txt):
            gi += 1
            reused, read = int(m.group(2)), int(m.group(3))
            rd_ms, rd_tps = int(m.group(4)), float(m.group(5))
            n, gen_ms, tps = int(m.group(6)), int(m.group(7)), float(m.group(8))
            a, off = int(m.group(9)), int(m.group(10))
            steps = n - a
            rd_mt = rd_ms / max(read, 1)
            row = (f"{arm:<12} {leg:<22} {gi:>3} {reused:>7} {read:>5} {rd_mt:>9.1f} "
                   f"{n:>4} {gen_ms/1000:>6.1f} {tps:>5.1f} {a:>4}/{off:<4} {100*a/max(off,1):>5.1f} "
                   f"{steps:>5} {n/max(steps,1):>5.2f} {off/max(steps,1):>6.2f} {gen_ms/max(steps,1):>6.1f}")
            print(row)
        if evs:
            print(f"    events[{arm}/{leg}]: " + ", ".join(evs))
    print()

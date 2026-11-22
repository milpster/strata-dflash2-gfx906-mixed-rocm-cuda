#!/usr/bin/env python3
"""Split the fork-vs-upstream difference of one file into per-feature patches, additions apart from edits.

Why the two kinds are kept apart: the two trees diverged in both directions. A hunk that carries a piece
of the fork's behaviour often also deletes a line upstream added after the fork's branch point - an
include, a feature the fork never had. Applying such a hunk with fuzz is how a port quietly loses
upstream features and the build then breaks somewhere far away. So:

  <feature>.add.patch  - hunks that only ADD lines. Their context is verified by patch(1); applying them
                         cannot remove anything upstream has.
  <feature>.edits.txt  - hunks that also REMOVE lines, printed whole, to be applied by a reader who can
                         see which side of the removal belongs to the fork's behaviour and which is
                         upstream's own code that must stay.

Usage: python3 tools-port/split-diff.py UPSTREAM FORK --out DIR
"""
import argparse
import collections
import os
import re

FEATURES = [
    ("expert_helper", r"expert_helper|helper_tier|helper_holds|howned|hprof|hblob|hhad|RemoteExperts|kBeginP|helper_frame|df_wire_rows|relief|proxy_residency|helper_slots|helper_mib|helper_reserve|helper_timeout|helper_"),
    ("cpu_pool", r"pool_expert|ram_pool|Pool::|pool_plan|bounded_direct|PoolExpert|pool_store|pool_cache|swapin|read_once|ram_relief"),
    ("vram_clamp", r"vram_guard|cache_spill|spill_tier|VramGuard|vram_guard_note|STRATA_VRAM_GUARD|kVramSafeMib"),
    ("dflash_guards", r"Dflash2Drafter|HAVE_DFLASH2|defer_df2|draft_df2"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("upstream")
    ap.add_argument("fork")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    txt = os.popen('diff -u --label a --label b "%s" "%s"' % (a.upstream, a.fork)).read()
    hunks, cur = [], []
    for ln in txt.split("\n"):
        if ln.startswith("@@"):
            if cur:
                hunks.append(cur)
            cur = [ln]
        elif cur:
            cur.append(ln)
    if cur:
        hunks.append(cur)

    buckets = collections.defaultdict(lambda: {"add": [], "edit": []})
    for h in hunks:
        added = "\n".join(l for l in h[1:] if l.startswith("+"))
        removed = [l for l in h[1:] if l.startswith("-") and len(l) > 1]
        feats = [n for n, p in FEATURES if re.search(p, added, re.I) or re.search(p, "\n".join(removed), re.I)]
        key = "+".join(feats) if feats else "other"
        buckets[key]["edit" if removed else "add"].append(h)

    for key, b in sorted(buckets.items()):
        base = key.replace("+", "__")
        for kind, hs in b.items():
            if not hs:
                continue
            fn = os.path.join(a.out, "%s.%s" % (base, "patch" if kind == "add" else "txt"))
            mode = "w" if kind == "edit" else "w"
            with open(fn, mode) as f:
                for h in hs:
                    f.write("\n".join(h).rstrip("\n") + "\n")
                    if kind == "edit":
                        f.write("\n")
        print("%-34s add:%-3d edit:%-3d" % (key, len(b["add"]), len(b["edit"])))


main()

#!/usr/bin/env python3
"""Resolve the shared-file conflicts in src/prefill/prefill.cpp (the fork's prompt-helper tier
against upstream's refactored prompt loop), with checks.

Each pick is asserted: a decision that dropped a name the rest of the file already uses fails
here rather than at link time. Read the reason for each hunk in PORT-NOTES.md.

Usage: python3 tools-port/resolve-prefill.py
"""
import pathlib
import sys

P = pathlib.Path(__file__).resolve().parent.parent / "engine/src/prefill/prefill.cpp"
OPEN, MID, CLOSE = "<<<<<<< ours\n", "\n=======\n", "\n>>>>>>> theirs\n"

# hunk order after the first seven were resolved (the eight that were left)
RULES = {
    1: "CPU_AT+HELPER",   # upstream's cpu_at counter + the fork's helper-prompt declarations
    2: "SHARE_NT_TRANSIENT",  # upstream's STRATA_PREFILL_CPU_SHARE_NT bound, the fork's transient rule
    3: "ROWS_LOCAL+SORT",     # the fork's helper block leaves the local one; upstream's tier sort
    4: "HELPER_ORDER",        # the fork's helper block placement, inside upstream's peer-kind loop
    5: "ON_HELPER",           # upstream's order_peer_res split, plus the fork's helper exclusion
    6: "SORT_ELIGIBLE",       # upstream's richer eligibility call, extended with on_helper.empty()
    7: "THEIRS",              # the fork's CPU job chunking (cpu_chunk is declared outside this hunk)
    8: "LM_LAMBDA",           # upstream's lm_streamed_experts() refactor, with the transient rule back in
}

MUST = {
    1: ["std::vector<char> on_helper;", "int64_t rows_helper = 0;", "cpu_at = 0"],
    2: ["if (c <= cpu_share_nt() && !m.src->transient(l, e))"],
    3: ["rows_local -= rows_helper;", "const bool tier_sort = detail::mmq_tier_sort_requested();"],
    4: ["!on_helper.empty(); ++e", "!on_cpu.empty() && !cpu_mid"],
    5: ["(on_helper.empty() || !on_helper[(size_t) e])", "if (order_peer_res.empty()) order_peer.push_back(e);"],
    6: ["on_helper.empty()", "layer_pin"],
    7: ["for (int b = 0; b < nt; b += cpu_chunk)"],
    8: ["lm_streamed_experts", "&& !m.src->transient(ll, ee)"],
}


def pick(rule, ours, theirs):
    if rule == "THEIRS":
        return theirs
    if rule == "CPU_AT+HELPER":
        return ours.split("\n")[0] + "\n" + theirs[theirs.index("\n") + 1:]
    if rule == "SHARE_NT_TRANSIENT":
        head = ("                                // a blob the CPU reads from RAM: page-locked, or any other that is not assembled into a\n"
                "                                // short-lived buffer (the arena's unpinned part, the mapped experts.bin's pages).\n"
                "                                // CS-T: a transient pair is excluded even when it is also pinned - the managed pool answers\n"
                "                                // BOTH (its memory is mlocked AND it copies on demand), and this share holds its pointer for\n"
                "                                // the whole layer, which a pool loan must never be.  For a non-transient source this is the\n"
                "                                // old test unchanged: `pinned || !transient` is `!transient`.\n"
                "                                if (c <= cpu_share_nt() && !m.src->transient(l, e))\n")
        return head
    if rule == "ROWS_LOCAL+SORT":
        return theirs + ours
    if rule == "HELPER_ORDER":
        up = ours.rstrip("\n").split("\n")
        i = next(k for k, l in enumerate(up) if "the CPU's block" in l or "!on_cpu.empty() && !cpu_mid" in l)
        helper = ("                            for (int32_t e = 0; e < m.g->n_expert && !on_helper.empty(); ++e)   // M2: the helper's block\n"
                  "                                if (on_helper[(size_t) e]) { m.off[(size_t) e] = r; r += m.cnt[(size_t) e]; }\n")
        return "\n".join(up[:i]) + "\n" + helper + "\n".join(up[i:]) + "\n"
    if rule == "ON_HELPER":
        first = ours.split("\n")[0].rstrip().rstrip("{").rstrip()
        if "(on_cpu.empty() || !on_cpu[(size_t) e])" not in first:
            sys.exit("ON_HELPER: anchor moved")
        first = first.replace("(on_cpu.empty() || !on_cpu[(size_t) e])",
                              "(on_cpu.empty() || !on_cpu[(size_t) e]) && (on_helper.empty() || !on_helper[(size_t) e])")
        return first + " {\n" + "\n".join(ours.split("\n")[2:])
    if rule == "SORT_ELIGIBLE":
        if "((sort_all || sort_full) && on_peer.empty()" not in ours:
            sys.exit("SORT_ELIGIBLE: anchor moved")
        return ours.replace("((sort_all || sort_full) && on_peer.empty()",
                            "((sort_all || sort_full) && on_peer.empty() && on_helper.empty()")
    if rule == "LM_LAMBDA":
        if "return m.src->pinned(ll, ee); }" not in ours:
            sys.exit("LM_LAMBDA: anchor moved")
        return ours.replace("return m.src->pinned(ll, ee); }",
                            "return m.src->pinned(ll, ee) && !m.src->transient(ll, ee); }")
    sys.exit("unknown rule " + rule)


def main():
    s = P.read_text(encoding="utf-8")
    if OPEN not in s:
        print("prefill.cpp: already resolved")
        return
    out, i, n = [], 0, 0
    while True:
        a = s.find(OPEN, i)
        if a < 0:
            out.append(s[i:])
            break
        out.append(s[i:a])
        m = s.index(MID, a)
        b = s.index(CLOSE, m)
        ours, theirs = s[a + len(OPEN):m + 1], s[m + len(MID):b + 1]
        n += 1
        rule = RULES[n]
        p = pick(rule, ours, theirs)
        for line in MUST[n]:
            if line not in p:
                sys.exit(f"hunk {n} ({rule}): expected to keep {line!r}")
        out.append(p)
        i = b + len(CLOSE)
    new = "".join(out)
    if "<<<<<<<" in new or ">>>>>>> theirs" in new:
        sys.exit("markers left")
    if n != len(RULES):
        sys.exit(f"expected {len(RULES)} hunks, found {n}")
    P.write_text(new, encoding="utf-8")
    print(f"prefill.cpp: {n} hunk(s) resolved, {s.count(chr(10)) + 1} -> {new.count(chr(10)) + 1} lines")


main()

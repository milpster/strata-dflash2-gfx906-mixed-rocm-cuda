#!/usr/bin/env python3
"""Resolve the six small shared-file conflicts left by the fork's body series, with checks.

Each pick is asserted: the lines the decision says it kept must be present in the result, so a
wrong slice fails here instead of silently dropping code (one earlier pass of mine cut a
`src_->note_async_read(...)` call this way and nobody would have seen it until runtime).

Usage: python3 tools-port/resolve-bodies-small.py
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OPEN = "<<<<<<< ours\n"
MID = "\n=======\n"
CLOSE = "\n>>>>>>> theirs\n"

THP_KEEP = [
    'const char* no_thp = std::getenv("STRATA_NO_ARENA_THP");',
    'const bool collapse = std::getenv("STRATA_ARENA_COLLAPSE") != nullptr;',
    'arena_skip_thp_request(no_thp, thp_mode == "always" && !collapse,',
]

THP_BODY = (
    "    // STRATA_NO_ARENA_THP unset = the engine decides: an integrated gfx11 GPU under Linux asks for none (arena_no_thp_flag, set\n"
    "    // from the device before the arena is made; a huge-page move inside the registered arena resets that GPU in a prompt), every other\n"
    "    // device as before.  =0 asks for them there too (the user's choice; #2126's free-block check below still applies); any other value\n"
    "    // skips the request on any device (as before).\n"
    '    const char* no_thp = std::getenv("STRATA_NO_ARENA_THP");\n'
    '    // W2a (opt-in, STRATA_ARENA_COLLAPSE=1): with THP "always" the mapping is huge-backed where the kernel can get a 2 MiB\n'
    "    // block, and MADV_HUGEPAGE is skipped because it adds reclaim and compaction on every fault.  The post-fill collapse sweep\n"
    "    // (expert_source.cpp, the arena's own loader) asks for those blocks ONCE, after the bytes are written, where there is no\n"
    '    // fault cost - so the request is worth making even under "always", and only then.  Unset: today\'s rule, byte for byte.\n'
    '    const bool collapse = std::getenv("STRATA_ARENA_COLLAPSE") != nullptr;\n'
    '    const bool skip_thp_request = arena_skip_thp_request(no_thp, thp_mode == "always" && !collapse,\n'
    "                                                          arena_no_thp_flag().load(std::memory_order_relaxed));\n")

# file -> list of (rule, lines the result must contain) in hunk order
PLAN = {
    "engine/src/core/mtp.cpp": [("BOTH", ['per_device_flag.hpp', 'vram_guard.hpp'])],
    "engine/src/core/peer_experts.cpp": [("PEER", ['(*gate_has_)[(size_t) s.layer] = 1;',
                                                   'src_->note_async_read(b, refill_);'])],
    "engine/src/core/pinned.cu": [("THP", THP_KEEP)],
    "engine/src/qwen35/q36_engine.cu": [("OURS", ['moe_fused_q36.hpp']),
                                        # the fork's block is byte-identical to upstream's just below it: OURS keeps one copy
                                        ("OURS", [])],
    "engine/src/kernels/cuda/iq_kernels.cu": [("BOTH", ['STRATA_SHARED_ALIGN16(T, name, ...)',
                                                        'STRATA_SHARED_ALIGN16_I4(name, ...)']),
                                              ("OURS", []), ("OURS", [])],
    "engine/src/core/conversation_cache_test.cpp": [("SCOPES", ['struct FakePool final : SegmentPoolBudget',
                                                                'check(cache.at(0) == nullptr && !cache.drop(0)'])],
}


def main():
    bad = 0
    for rel, plan in PLAN.items():
        path = ROOT / rel
        s = path.read_text(encoding="utf-8")
        if OPEN not in s:
            print(f"{rel}: already resolved")
            continue
        out, i, n = [], 0, 0
        while True:
            a = s.find(OPEN, i)
            if a < 0:
                out.append(s[i:])
                break
            out.append(s[i:a])
            m = s.index(MID, a)
            b = s.index(CLOSE, m)
            ours = s[a + len(OPEN):m + 1]
            theirs = s[m + len(MID):b + 1]
            rule, must = plan[n]
            n += 1
            if rule == "OURS":
                pick = ours
            elif rule == "THEIRS":
                pick = theirs
            elif rule == "BOTH":
                pick = ours + theirs
            elif rule == "SCOPES":      # two independent test bodies: give each its own scope
                pick = "        {\n" + ours + "        }\n        {\n" + theirs + "        }\n"
            elif rule == "PEER":        # theirs' first line closes the copy's if-block; then both sides' work
                pick = theirs.split("\n")[0] + "\n" + ours + "\n".join(theirs.split("\n")[1:])
            elif rule == "THP":
                pick = THP_BODY
            else:
                sys.exit(f"unknown rule {rule}")
            for line in must:
                if line not in pick:
                    print(f"MISSING in {rel} hunk {n}: {line!r}")
                    bad += 1
            out.append(pick)
            i = b + len(CLOSE)
        new = "".join(out)
        if "<<<<<<<" in new or ">>>>>>> theirs" in new:
            print(f"MARKERS LEFT in {rel}")
            bad += 1
        if n != len(plan):
            print(f"{rel}: expected {len(plan)} hunks, found {n}")
            bad += 1
        path.write_text(new, encoding="utf-8")
        print(f"{rel}: {n} hunk(s), {s.count(chr(10)) + 1} -> {new.count(chr(10)) + 1} lines")
    return 1 if bad else 0


sys.exit(main())

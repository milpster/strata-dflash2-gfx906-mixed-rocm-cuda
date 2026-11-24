#!/usr/bin/env python3
"""Derive the relief-OFF trial config from the promoted port-0218 tuple.

Removes ONLY the four flags that enable --expert-helper-ram-relief and its
dependent levers, keeping every other args token, env key and top-level value
byte-identical to prod-config-port0218-nohuge.json. Preserves key order.

Usage: python3 tools/derive_norelief_config.py [--check]
"""
import collections
import json
import sys

SRC = "prod-config-port0218-nohuge.json"
DST = "prod-config-port0218-norelief.json"

# value-taking flags that depend on the relief
REMOVE_WITH_VALUE = {
    "--expert-helper-relief-hot-mib",
    "--expert-helper-relief-arena-cap-mib",
    "--expert-helper-relief-arena-cap-decode-mib",
}
# bare (boolean) flags that depend on the relief
REMOVE_BARE = {
    "--expert-helper-ram-relief",
    "--expert-helper-ram-relief-packs",
    "--expert-helper-relief-arena-cap-rerank",
}

NOTE = (
    "Trial 2026-11-24. NOT PROMOTED. The promoted tuple with the expert-helper RAM relief "
    "turned fully OFF, to test whether the relief/re-rank unregister+re-register path revokes "
    "the GPU translation inside the 32 GiB pool VMA (that VMA has 0 huge pages and is mlocked, "
    "yet the amdgpu no-retry permission fault recurs under park/restore churn). ONLY the four "
    "relief flags moved (--expert-helper-ram-relief, --expert-helper-ram-relief-packs, "
    "--expert-helper-relief-hot-mib 2048, --expert-helper-relief-arena-cap-mib 4096); nothing "
    "else in args, env, exe, ports or paths changed. WARNING, read before spending a soak on "
    "it: this switch is structurally inert for this tuple - with STRATA_RAM_BUDGET_MIB set the "
    "expert source is the pool adapter (generate.cpp:9437/9550), never ArenaExpertSource, so the "
    "arena - and with it set_unfilled/register_complement - is never opened; and "
    "--expert-helper-relief-arena-cap-rerank is absent, so reorder_arena_by_heat, the only "
    "caller of release_pair/refill_pair, is never invoked (generate.cpp:26770). The pool VMA "
    "carries no cudaHostRegister at all (ram_pool.cpp:create: mmap + mlock only)."
)


def derive(src_args):
    out, removed, i = [], [], 0
    while i < len(src_args):
        tok = src_args[i]
        if tok in REMOVE_WITH_VALUE:
            removed.append((tok, src_args[i + 1]))
            i += 2
            continue
        if tok in REMOVE_BARE:
            removed.append((tok, None))
            i += 1
            continue
        out.append(tok)
        i += 1
    return out, removed


def main():
    src = json.load(open(SRC), object_pairs_hook=collections.OrderedDict)
    new_args, removed = derive(list(src["args"]))

    dst = collections.OrderedDict(src)
    dst["args"] = new_args
    dst["derived_from"] = SRC
    dst["derived_note"] = NOTE

    if "--check" in sys.argv:
        cur = json.load(open(DST), object_pairs_hook=collections.OrderedDict)
        ok = cur == dst
        print(f"removed: {removed}")
        print(f"args {len(src['args'])} -> {len(new_args)}")
        print(f"{DST} matches the derivation: {ok}")
        return 0 if ok else 1

    with open(DST, "w") as f:
        json.dump(dst, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"removed: {removed}")
    print(f"args {len(src['args'])} -> {len(new_args)}; wrote {DST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""tools/mixed_gpu/p0_verify_cells.py - CPU-safe validation of the P0 token-id cell files.

Checks, with the pack's own tokenizer (the engine reads the same pack):
  * every id parses as an integer in [0, vocab_size) - the engine's --tokens-file parse
    (src/program/generate.cpp:2524-2535, parse_i64_list) and its vocabulary bound;
  * the exact token count per cell (decode the ids to text, re-encode, compare lists - a round-trip,
    the same check `strata bench` does with its frozen counts);
  * the file digests against cells.sha256.

CPU-only: tokenizer + files, no engine, no GPU, no network.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from build_standard_corpus import Counter   # noqa: E402  (tokenizer loader only)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tokenizer", required=True, type=Path)
    ap.add_argument("--cells-dir", required=True, type=Path)
    a = ap.parse_args(argv)

    counter = Counter(a.tokenizer, a.tokenizer / "chat_template.jinja")
    vocab = len(counter.tok.tokens)
    fails = 0
    manifest = (a.cells_dir / "cells.sha256").read_text(encoding="utf-8").split("\n")
    want = {line.split("  ")[1]: line.split("  ")[0] for line in manifest if "  " in line}
    for name in sorted(want):
        p = a.cells_dir / name
        data = p.read_text(encoding="utf-8")
        got = hashlib.sha256(data.encode("utf-8")).hexdigest()
        ids = [int(x) for x in data.replace("\n", " ").replace(",", " ").split()]
        oob = [i for i in ids if i < 0 or i >= vocab]
        re_ids = counter.tok.encode(counter.tok.decode(ids), parse_special=True)
        ok_hash = got == want[name]
        ok_bound = not oob
        ok_round = re_ids == ids
        ok_count = len(ids)
        status = "ok" if (ok_hash and ok_bound and ok_round) else "FAIL"
        if status == "FAIL":
            fails += 1
        print(f"{name}: {ok_count} tokens, vocab {vocab}, hash {'match' if ok_hash else 'MISMATCH'}, "
              f"ids-in-range {'yes' if ok_bound else f'NO ({len(oob)} out)'}, "
              f"decode/encode round-trip {'exact' if ok_round else 'DIFFERS'}: {status}")
    print(f"p0_verify_cells: {'PASS' if fails == 0 else 'FAIL'} ({fails} failing file(s))")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

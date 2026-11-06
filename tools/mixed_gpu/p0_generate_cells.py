#!/usr/bin/env python3
"""tools/mixed_gpu/p0_generate_cells.py - the deterministic token-id inputs for the P0 headroom cells.

CPU-only: this loads the pack's tokenizer (tools/strata_tokenizer.py) and the pack's chat template
(serve.frontend.ChatTemplate) - the same pair the server renders with - and writes token-id files the
engine's `--tokens-file` (src/program/generate.cpp:2524-2535, `parse_i64_list`) reads directly.  No engine
is run, no GPU, no network, no install.

Content: the SAME record corpus the frozen standard benchmark builds (tools/build_standard_corpus.py: the
`recordNNNNNN: alpha beta gamma delta epsilon zeta eta theta.` lines and the summary question), rendered as
one user message with the pack template and the thinking kwargs the server uses for a thinking request.
The builder fixes 4,096 and 16,384-token prompts; the P0 cells need 4k / 32k / 128k with EXACT counts, so
this writes `r` record lines (numbered 1..r, the builder's own format) chosen so the rendered request is
exactly the target, and - when the whole-record steps overshoot by a few tokens - fills the gap with whole
copies of the builder's own frozen instruction/tool/insert lines (measured 9..32 rendered tokens each,
combined by an exact subset search).  Every token of every cell comes from a frozen builder string: a
labelled repeated record corpus, never random tokens, never invented ids.

Each cell is written as comma-separated token ids with a trailing newline, beside a `cells.sha256`
manifest and a `cells.json` record (exact token count, record count, fill lines, builder provenance,
tokenizer/template hashes).  Re-running writes byte-identical files.

    python3 tools/mixed_gpu/p0_generate_cells.py \
        --tokenizer /mnt/user/ai/iq3xxs-15/tokenizer \
        --template /mnt/user/ai/iq3xxs-15/tokenizer/chat_template.jinja \
        --out build-mixed-gpu/p0/cells
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]        # the worktree root
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from build_standard_corpus import (RECORD, SUMMARY_QUESTION, TEMPLATE_KWARGS, Counter,   # noqa: E402
                                   records_body, user_message)

# cell name -> target prompt tokens (4k, 32k, 128k; the 262144 cache budget stays in the engine args)
CELLS = {"4k": 4096, "32k": 32768, "128k": 131072}


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# The fine ladder: frozen builder strings only (the builder's task/instruction/tool/retrieval-insert
# lines).  Measured rendered-token deltas on this tokenizer (2026-10-27, the pack tokenizer + pack
# template): 9, 10, 11, 12, 13, 14, 18, 24, 27, 32.  Deltas are additive in the body, so an exact subset
# of them closes any record-overshoot gap.
POOL = [
    "access_code = ORBIT-A17",                                                    #  9
    "Use get_weather for Zagreb in celsius.",                                     # 11
    "storage_label = CEDAR-284", "archive_code = DELTA-906",                     # 10
    "What is archive_code? Reply only with its value.",                          # 12
    "Reply with exactly the word READY in uppercase and nothing else.",          # 13
    "Return only a JSON object containing north and south and their values.",    # 14
    "Explain how a hash table finds a value, including collisions and one small example.",   # 18
    "Return only a JSON object with ok set to true and count set to the integer 3. No other keys.",   # 24
    "Project: Orion. Build: 17. Return only a JSON object with keys project (string) and build (integer).",  # 27
    "Sort these integers in ascending order and remove duplicates: 9, 2, -4, 2, 1. Return only a JSON array.",  # 32
    "access_code = ORBIT-731", "north = N-124", "south = S-875",
] + [f"access_code = ORBIT-{731 + i}" for i in range(1, 40)]


def pick_exact_body(counter: Counter, cell: str, target: int) -> tuple[str, int, list[str]]:
    """The record corpus whose rendered request is EXACTLY `target` tokens.

    `r` record lines (the builder's numbering) plus fill lines from POOL.  The token count is linear in
    `r` (measured per-record delta), so the estimate lands within a few records of the target; the
    window is swept, and for each `r` the exact fill is found by 0/1 subset DP over POOL (greedy is not
    enough: 26 = 12 + 14, which the greedy walk misses).  The exact count is verified; an inexact
    landing is a refusal, never a silent approximate cell.  Returns (body, records, fill_lines)."""
    def tokens(body: str) -> int:
        return len(counter.ids(user_message(f"P0-{cell}", body), None))

    def body_of(r: int, fill: list[str]) -> str:
        lines = [RECORD.format(i + 1) for i in range(r)]
        lines.extend(fill)
        lines.append(SUMMARY_QUESTION)
        return "\n".join(lines)

    t0 = tokens(body_of(0, []))
    per = max(1.0, (tokens(body_of(8, [])) - t0) / 8.0)
    r_est = max(0, round((target - t0) / per))
    base = tokens(body_of(r_est, []))
    per = max(1.0, (tokens(body_of(r_est + 8, [])) - base) / 8.0)
    r_est = max(0, round((target - t0) / per))

    def fill_for(r: int) -> tuple[int, list[str]]:
        base_r = tokens(body_of(r, []))
        gap = target - base_r
        if gap <= 0:
            return base_r, []
        deltas = {line: tokens(body_of(r, [line])) - base_r for line in POOL}
        reach = [False] * (gap + 1)
        take: list[tuple[int, int] | None] = [None] * (gap + 1)
        reach[0] = True
        for idx, line in enumerate(POOL):
            d = deltas[line]
            if d <= 0 or d > gap:
                continue
            for s in range(gap, d - 1, -1):
                if reach[s - d] and not reach[s]:
                    reach[s] = True
                    take[s] = (idx, s - d)
        if not reach[gap]:
            return base_r, []
        fill: list[str] = []
        s = gap
        while s:
            idx, prev = take[s]
            fill.append(POOL[idx])
            s = prev
        fill.sort(key=POOL.index)                       # keep the frozen pool order
        return target, fill

    for r in range(max(0, r_est - 12), r_est + 13):
        got, fill = fill_for(r)
        if got == target:
            return body_of(r, fill), r, fill
    raise SystemExit(f"{cell}: cannot land exactly on {target} tokens near r={r_est}; "
                     "refusing to write an inexact cell")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tokenizer", required=True, type=Path,
                    help="the pack's tokenizer/ folder (vocab.json, merges.txt, token_type.json)")
    ap.add_argument("--template", required=True, type=Path,
                    help="the chat template the server renders with (the pack's tokenizer/chat_template.jinja)")
    ap.add_argument("--out", required=True, type=Path, help="output directory for the cell files")
    ap.add_argument("--cells", default=",".join(CELLS), help="comma-separated cell names (4k,32k,128k)")
    a = ap.parse_args(argv)

    counter = Counter(a.tokenizer, a.template)
    vocab_size = len(counter.tok.tokens)
    a.out.mkdir(parents=True, exist_ok=True)

    record = {"protocol_id": "strata-qwen38-thinking-v1-draft.1 record corpus, exact-count P0 cells v1",
              "tokenizer_dir": str(a.tokenizer), "template": str(a.template),
              "tokenizer_files": {name: hashlib.sha256((a.tokenizer / name).read_bytes()).hexdigest()
                                  for name in ("vocab.json", "merges.txt", "token_type.json")},
              "template_sha256": sha256_file(a.template),
              "template_kwargs": TEMPLATE_KWARGS,
              "record_format": RECORD, "summary_question": SUMMARY_QUESTION,
              "content_kind": "the frozen standard-benchmark record corpus (recordNNNNNN lines, builder "
                              "numbering 1..r) plus whole frozen builder instruction/tool lines as fill; "
                              "labelled repeated corpus, no random tokens, no invented ids",
              "cells": {}}

    for cell in [c.strip() for c in a.cells.split(",") if c.strip()]:
        if cell not in CELLS:
            raise SystemExit(f"unknown cell {cell!r} (known: {', '.join(CELLS)})")
        target = CELLS[cell]
        body, records, fill = pick_exact_body(counter, cell, target)
        ids = counter.ids(user_message(f"P0-{cell}", body), None)
        if len(ids) != target:
            raise SystemExit(f"{cell}: rendered {len(ids)} tokens, target {target}")
        if ids and max(ids) >= vocab_size:
            raise SystemExit(f"{cell}: token id {max(ids)} is outside the vocabulary ({vocab_size})")
        text = ",".join(str(i) for i in ids) + "\n"
        path = a.out / f"tokens-{cell}.txt"
        path.write_text(text, encoding="utf-8")
        record["cells"][cell] = {"target_tokens": target, "tokens": len(ids), "records": records,
                                 "fill_lines": fill, "file": path.name,
                                 "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                 "first20_ids": ids[:20], "last20_ids": ids[-20:]}
        print(f"{cell}: {len(ids)} tokens, {records} records, {len(fill)} fill lines, "
              f"sha256 {record['cells'][cell]['sha256']}")

    (a.out / "cells.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = "".join(f"{sha256_file(a.out / n)}  {n}\n"
                       for n in sorted(p.name for p in a.out.glob("tokens-*.txt")))
    (a.out / "cells.sha256").write_text(manifest, encoding="utf-8")
    print(f"cells.json + cells.sha256 written under {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

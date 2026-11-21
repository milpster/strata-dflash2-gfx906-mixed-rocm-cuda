#!/usr/bin/env python3
"""Build a long synthetic prefill prompt in the shape the managed-pool legs use.

One "Section NNNN" per record: the filler sentence repeated four times, then the
record line. 642 sections = 341,345 chars = 74,635 tokens on the UD pack
(4.57 chars/token), so sections scale almost linearly with the token target.

    make-fill-prompt.py --sections 1977 --out prompt-230k.txt

The closing instruction asks for a long, gradeable report, so a 10k-token
generation leg has something to do and its correctness can be checked later
(counts per reserve value are decidable from the file).
"""
import argparse
import hashlib
import pathlib

FILLER = ("The serving stack keeps expert weights in a bounded host pool and streams them "
          "to the graphics cards as the router asks for them. ")
RESERVES = (192, 256, 384, 512, 704, 896, 1024, 1280)
WINDOWS = 64


def build(sections: int) -> tuple[str, list[tuple[int, int, int]]]:
    records = []
    out = []
    for i in range(sections):
        # deterministic, spread over windows and reserves, no dependence on the clock
        window = (i * 7 + 3) % WINDOWS
        reserve = RESERVES[(i * 5 + 1) % len(RESERVES)]
        records.append((i, window, reserve))
        out.append(f"Section {i:04d}. {FILLER * 4}"
                   f"Line {i:04d} records measurement window {window} with reserve {reserve} MiB. \n\n")
    counts = {r: sum(1 for _, _, rr in records if rr == r) for r in RESERVES}
    asked = ", ".join(f"{r} MiB: {n}" for r, n in sorted(counts.items()))
    out.append(
        "Records above, by reserve: " + asked + ".\n\n"
        "Using only the sections above, write a long technical report in several parts. "
        "Part 1: restate how many records carry each reserve value and quote the first and the "
        "last record exactly. Part 2: for every reserve value, list the records that use it, in "
        "order, with their measurement window. Part 3: describe the distribution of measurement "
        "windows and which windows appear most often. Part 4: discuss what a serving stack would "
        "learn from a table shaped like this one, and end with a short summary. Do not invent "
        "records that are not above, and keep writing until the report is complete.\n")
    return "".join(out), records


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sections", type=int, default=1977)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    text, records = build(a.sections)
    path = pathlib.Path(a.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(text.encode()).hexdigest()
    chars = len(text)
    print(f"{path}: {a.sections} records, {chars:,} chars, ~{chars / 4.57:,.0f} tokens "
          f"at the measured 4.57 chars/token, sha256 {digest}")


if __name__ == "__main__":
    main()

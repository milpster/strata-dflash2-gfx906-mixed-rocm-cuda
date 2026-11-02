#!/usr/bin/env python3
"""Independent cross-check of the P2 harness's fixture content pins.

Reads the fixture texts straight out of p2_measure.cpp (the kXxx string-literal blocks), decodes the
C string escapes, computes FNV-1a/64 (offset basis 0xcbf29ce484222325, prime 0x100000001b3) over each
text's bytes, and prints the same "id,len,fnv;" list the harness prints.  This is a SECOND
implementation, in a different language, of the pin: the harness's own pins are only trusted when this
script's list matches them byte for byte.

Usage: python3 check_fixture_pins.py [path/to/p2_measure.cpp]
"""
import re
import sys

OFFSET = 0xcbf29ce484222325
PRIME = 1099511628211
MASK = (1 << 64) - 1

# the fixture ids in the order fixtures() lists them, with the constant each one points at
FIXTURES = [
    ("pinned-sample", "kPinnedSample"),
    ("lighthouse-prefill", "kLighthouseRepetitive"),
    ("decode-list", "kDecodeList"),
    ("code-question-short", "kCodeShort"),
    ("doc-rewrite-mid", "kDocRewrite"),
    ("json-extract", "kJsonExtract"),
    ("code-review-long", "kCodeReview"),
    ("prose-analysis-long", "kProseLong"),
]


def c_string_literals(src, start):
    """Every C string literal in src[start:] up to the ';' that ends the declaration."""
    out, i, n = [], start, len(src)
    while i < n:
        ch = src[i]
        if ch == ";":
            return out, i
        if ch == '"':
            j, buf = i + 1, []
            while j < n and src[j] != '"':
                if src[j] == "\\":
                    nxt = src[j + 1]
                    if nxt in "nrt0\"'\\?avf":
                        buf.append({"n": "\n", "r": "\r", "t": "\t", "0": "\0", '"': '"',
                                     "'": "'", "\\": "\\", "?": "?", "a": "\a", "v": "\v",
                                     "f": "\f"}[nxt])
                        j += 2
                        continue
                    m = re.match(r"([0-7]{1,3})", src[j + 1:])
                    if m:
                        buf.append(chr(int(m.group(1), 8)))
                        j += 1 + len(m.group(1))
                        continue
                    m = re.match(r"x([0-9a-fA-F]{1,2})", src[j + 1:])
                    if m:
                        buf.append(chr(int(m.group(1), 16)))
                        j += 2 + len(m.group(1))
                        continue
                    buf.append(nxt)
                    j += 2
                    continue
                buf.append(src[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
            continue
        i += 1
    return out, i


def fnv1a_64(data: bytes) -> int:
    h = OFFSET
    for b in data:
        h = ((h ^ b) * PRIME) & MASK
    return h


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "p2_measure.cpp"
    src = open(path, encoding="utf-8").read()
    parts = []
    for fid, sym in FIXTURES:
        m = re.search(r"\bconst char\* const %s\s*=" % sym, src)
        if not m:
            sys.exit("fixture constant %s not found in %s" % (sym, path))
        lits, _ = c_string_literals(src, src.index('"', m.end()))
        text = "".join(lits).encode("utf-8")
        parts.append("%s,%d,%016x" % (fid, len(text), fnv1a_64(text)))
    print(";".join(parts) + ";")


if __name__ == "__main__":
    main()

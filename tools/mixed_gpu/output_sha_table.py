#!/usr/bin/env python3
"""Temp-0 output-content SHA table over archived window evidence.

Scans <root>/run/evidence-*/sample-*/response.bin files, hashes the generated
CONTENT (choices[0].text or choices[0].message.content) — never the raw JSON,
whose timing fields differ every run — and prints one line per request plus a
grouped uniqueness summary. Same content hash across runs = reproducible
greedy output; differing hash = real divergence (nondeterminism or a numeric
path change). Stdlib only.

Usage: python3 tools/mixed_gpu/output_sha_table.py ROOT [ROOT ...]
"""
import hashlib
import json
import sys
from pathlib import Path


def content_of(response_path: Path):
    try:
        data = json.loads(response_path.read_text(errors="replace"))
    except (OSError, ValueError) as exc:
        return None, None, f"unreadable: {exc}"
    choices = data.get("choices") or []
    if not choices:
        return None, None, "no choices"
    ch = choices[0]
    text = ch.get("text")
    if text is None:
        msg = ch.get("message") or {}
        text = msg.get("content")
    if text is None:
        return None, None, "no text content"
    usage = data.get("usage") or {}
    return text, usage.get("completion_tokens"), None


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    rows = []
    for root in argv[1:]:
        base = Path(root)
        window = base.name
        for resp in sorted(base.glob("run/evidence-*/sample-*/response.bin")):
            label = resp.parent.parent.name.removeprefix("evidence-")
            text, tokens, err = content_of(resp)
            if err:
                sha = f"ERROR:{err}"
            else:
                sha = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
            rows.append((window, label, tokens, sha))
    if not rows:
        print("no response.bin evidence found under the given roots")
        return 1
    print(f"{'window':<24} {'label':<28} {'tok':>4}  sha256(content)")
    for window, label, tokens, sha in rows:
        print(f"{window:<24} {label:<28} {str(tokens):>4}  {sha}")
    print()
    print("== uniqueness groups (sha16, count, members) ==")
    groups = {}
    for window, label, tokens, sha in rows:
        groups.setdefault(sha[:16], []).append(f"{window}/{label.split('-', 1)[-1]}")
    for sha16, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"{sha16}  x{len(members):<2} {', '.join(sorted(members))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""Derive a boot config with an EMPTY conversation-cache directory, for a genuinely
cold prefill measurement.

Why this exists: the managed-pool trial config points --conversation-cache-dir at the
worktree's soak cache, which keeps a checkpoint of the 80k prompt on disk. A later
"cold" leg then logs `restored 74630 tokens from disk (checkpoint)` and
`prompt 74635 tokens = 74630 reused + 5 read`, so the request never embeds the prompt
and the measured time is a disk restore, not prefill throughput. Two windows measured
that way cannot be compared on prefill speed.

Pointing the cache at a fresh directory makes the boot cold without deleting or moving
another lane's cache files, and the same directory then holds the checkpoint that a
warm repeat in the same boot must reuse.

Usage: make-cold-config.py <base.json> <out.json> <fresh-cache-dir> [--log PATH] [--note TEXT]

--log matters too: every window must write its own engine log, or two windows' evidence
lands in one file and neither can be quoted.
Prints the derived cache dir and asserts the file parses.
"""
import json
import pathlib
import sys

base, out, cache_dir = sys.argv[1:4]
note = ""
if "--note" in sys.argv:
    note = sys.argv[sys.argv.index("--note") + 1]
log_path = sys.argv[sys.argv.index("--log") + 1] if "--log" in sys.argv else ""

d = json.loads(pathlib.Path(base).read_text())
args = d["args"]

if "--conversation-cache-dir" not in args:
    raise SystemExit("base config has no --conversation-cache-dir; inspect it first")
i = args.index("--conversation-cache-dir")
print(f"base cache dir: {args[i + 1]}")
args[i + 1] = cache_dir

if log_path:
    print(f"base log: {d.get('log')}")
    d["log"] = log_path

d["derived_from"] = str(pathlib.Path(base).resolve())
d["derived_note"] = ("empty conversation cache for a genuinely cold prefill"
                     + (f"; {note}" if note else ""))
pathlib.Path(out).write_text(json.dumps(d, indent=2) + "\n")

check = json.loads(pathlib.Path(out).read_text())
j = check["args"].index("--conversation-cache-dir")
print(f"derived cache dir: {check['args'][j + 1]}")
if log_path:
    print(f"derived log: {check.get('log')}")
print(f"wrote {out}")

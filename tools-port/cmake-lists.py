#!/usr/bin/env python3
"""
cmake-lists.py - the fork's GPU target wiring against upstream v0.2.18's.

Upstream 0.2.18 moved the CUDA/HIP target lists out of CMakeLists.txt into
engine/cmake/strata_gpu_engine.cmake. The fork's serving lane needs its own files in those
lists. This tool reads a target's source list from both files (balanced-paren scan) and shows
the difference; with --apply it inserts the missing files into the upstream list, marked with a
comment naming the port, and checks afterwards that every inserted name is there once.

Usage:
    python3 tools-port/cmake-lists.py diff <target> [<target> ...]
    python3 tools-port/cmake-lists.py apply <target> <file> [<file> ...]
"""
import re
import subprocess
import sys

FORK_CMAKE = "/mnt/srcds/Strata-wt-merge-ipc/CMakeLists.txt"
UP_CMAKE = "/mnt/srcds/Strata-wt-0218/engine/cmake/strata_gpu_engine.cmake"
MARK = "ported serving lane (PORT-MANIFEST.txt; upstream v0.2.18 has none of these)"
SKIP = {"STATIC", "SHARED", "OBJECT", "MODULE", "EXCLUDE_FROM_ALL", "IMPORTED", "INTERFACE", "ALIAS"}


def fork_path(p):
    """A fork source path -> where it lives in the port tree."""
    for old, new in (("src/", "engine/src/"), ("include/", "engine/include/"), ("sycl/", "engine/sycl/")):
        if p.startswith(old):
            return new + p[len(old):]
    return p


def strip_engine(p):
    """The GPU target lists live in engine/, so their paths are relative to engine/."""
    return p[len("engine/"):] if p.startswith("engine/") else p


def block(text, target):
    """The text of a target's source list, with its exact offsets (start, end) in `text`."""
    for kind in ("add_library", "add_executable"):
        m = re.search(r"^[ \t]*%s\(\s*%s\b" % (kind, re.escape(target)), text, re.M)
        if not m:
            continue
        open_at = text.index("(", m.start())
        depth, i = 0, open_at
        while i < len(text):
            c = text[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        inner = text[open_at + 1:i]
        inner_start = open_at + 1
        # drop the target name and the options
        off = inner.index(target) + len(target)
        return inner[off:], inner_start + off, i
    return None, None, None


def names(body, remap=None):
    out = []
    for line in body.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        for tok in re.split(r"\s+", line):
            if not tok or tok in SKIP or tok.startswith(("$", "-", "/")):
                continue
            out.append(remap(tok) if remap else tok)
    return out


def main():
    mode = sys.argv[1]
    targets = sys.argv[2:]
    fork_text = open(FORK_CMAKE, encoding="utf-8").read()
    if mode == "diff":
        for t in targets:
            fb, _, _ = block(fork_text, t)
            up_text = open(UP_CMAKE, encoding="utf-8").read()
            ub, _, _ = block(up_text, t)
            if fb is None or ub is None:
                print("%s: fork=%s upstream=%s" % (t, "yes" if fb else "NO", "yes" if ub else "NO"))
                continue
            have = set(names(ub))
            want = [strip_engine(x) for x in names(fb, fork_path) if not x.endswith("/") and x not in have]
            ported = set()
            for line in open("/mnt/srcds/Strata-wt-0218/PORT-MANIFEST.txt", encoding="utf-8"):
                line = line.strip()
                if line:
                    ported.add(strip_engine(fork_path(line.split("\t")[0])))
            missing = [w for w in want if w in ported]
            print("== %s: fork %d sources, upstream %d, ported files the upstream list lacks: %d"
                  % (t, len(names(fb)), len(ub and names(ub) or []), len(missing)))
            for w in missing:
                print("   +", w)
            other = [w for w in want if w not in ported]
            if other:
                print("   (not in the port: %s)" % " ".join(other))
        return
    if mode == "apply":
        t = targets[0]
        files = targets[1:]
        text = open(UP_CMAKE, encoding="utf-8").read()
        body, start, end = block(text, t)
        if body is None:
            sys.exit("no target %s in %s" % (t, UP_CMAKE))
        have = set(names(body))
        add = [f for f in files if f not in have]
        if not add:
            print("%s: already wired" % t)
            return
        ins = "\n    # --- %s ---\n" % MARK + "".join("    %s\n" % f for f in add)
        nl = text.rfind("\n", start, end)
        if nl == -1:
            sys.exit("no line break inside the list of %s" % t)
        text = text[:nl] + "\n" + ins.rstrip("\n") + text[nl:]
        open(UP_CMAKE, "w", encoding="utf-8").write(text)
        check = open(UP_CMAKE, encoding="utf-8").read()
        got = names(block(check, t)[0])
        for f in add:
            if got.count(f) != 1:
                sys.exit("post-condition failed: %s appears %d times in %s" % (t, got.count(f), f))
        print("%s: inserted %d sources (%d entries now)" % (t, len(add), len(got)))
        return
    sys.exit("mode must be diff or apply")


main()

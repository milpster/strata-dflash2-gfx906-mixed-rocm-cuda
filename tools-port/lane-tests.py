#!/usr/bin/env python3
"""
lane-tests.py - the test programs of the serving lane that the port's file checklist does not cover.

docs/PLAN-UPSTREAM-0218.md counts src/, include/, sycl/ and serve/. The lane's own test programs sit
under tests/, so they were in nobody's count: the files are in this tree's reach, upstream's CMake has
no idea they exist, and `ctest` on this port therefore cannot cover the ported features (PORT-NOTES.md,
"the lane's own tests are not wired").

This prints them as <fork path>\t<port path> lines - the same shape as PORT-MANIFEST.txt, so
`sh tools-port/apply-manifest.sh <fork> PORT-MANIFEST-TESTS.txt` copies them - limited to the ones the
fork's own CMakeLists.txt builds, so the list is the work that is actually owed. Their build blocks
(add_executable, the libraries each links, the add_test and its properties) still have to be
transplanted by hand from the fork's CMakeLists.txt: they name fixtures and link options per test, and
a generated guess at them would be worse than an honest gap.

Read-only. Usage: python3 tools-port/lane-tests.py > PORT-MANIFEST-TESTS.txt

--support prints the other half of the same question. A test's build block names only its .cpp files;
every header or fixture they include (tests/core/cuda_runtime.h, a shared fixture) is fork-only content
too, but no CMakeLists.txt in the world mentions it, so the list above silently omits it and the test
that needs it fails to compile. --support lists the fork-only files under the same roots that the main
list does not, so the wiring job starts with a complete tree instead of a compile error.

Read-only. Usage: python3 tools-port/lane-tests.py --support > PORT-MANIFEST-TESTSUP.txt
"""
import argparse
import os
import subprocess

FORK_CMAKE = "/mnt/srcds/Strata-wt-merge-ipc/CMakeLists.txt"
FORK_REF = "564e1dbbea"
UP_REF = "v0.2.18"
REPO = "/mnt/srcds/Strata"


def git(*args):
    return subprocess.run(["git", "-C", REPO] + list(args),
                          capture_output=True, text=True, check=True).stdout


def to_port(p):
    """Where a fork path lives in this tree.

    The fork moved the python test suite to the tree root (tests/terminal/...), while v0.2.18 keeps it
    under terminal/ (terminal/tests/terminal/...), and the C++ engine moved the other way (engine/). So
    the destination is not a rule: it is where a file with that path already sits. The first candidate
    whose parent directory exists wins; a file nothing in the tree resembles goes under engine/, which
    is where the lane's C++ lives."""
    cands = ["engine/" + p, "terminal/" + p, p] if p.startswith(("src/", "include/", "tests/", "tools/")) else [p]
    port = "/mnt/srcds/Strata-wt-0218/"
    for c in cands:
        if os.path.isdir(os.path.dirname(port + c)) or os.path.isdir(port + c.rstrip("/")):
            return c
    return cands[0]


def exists_anywhere(p):
    """True if this fork file is already in the port tree at any of its plausible destinations."""
    port = "/mnt/srcds/Strata-wt-0218/"
    cands = ["engine/" + p, "terminal/" + p, p] if p.startswith(("src/", "include/", "tests/", "tools/")) else [p]
    return any(os.path.exists(port + c) for c in cands)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--support", action="store_true",
                    help="fork-only files under tests/ and tools/mixed_gpu/ that the main list omits")
    a = ap.parse_args()
    upstream = set(git("ls-tree", "-r", "--name-only", UP_REF).split("\n"))
    built = set()
    for tok in subprocess.run(["grep", "-oE", r"tests/[A-Za-z0-9_/]+\.(cpp|py|sh|h|hpp)", FORK_CMAKE],
                              capture_output=True, text=True).stdout.split():
        built.add(to_port(tok))
    rows = []
    for root in ["tests/"]:
        for p in git("ls-tree", "-r", "--name-only", FORK_REF, "--", root).split("\n"):
            if not p:
                continue
            mapped = to_port(p)
            if mapped in upstream:
                continue          # upstream has it; nothing is owed for it
            if not a.support:
                if mapped not in built:
                    continue      # the fork does not build it either
            elif mapped in built:
                continue          # the main list already covers it
            rows.append((p, mapped))
    for a_, b in sorted(rows):
        print("%s\t%s" % (a_, b))
    cpp = sum(1 for _, b in rows if b.endswith(".cpp"))
    if a.support:
        print("# %d files the main list does not cover: headers, fixtures and the lane's own harness "
              "scripts the tests include or call" % len(rows))
    else:
        print("# %d files (%d test programs, the rest are their fixtures and stubs)" % (len(rows), cpp))


main()

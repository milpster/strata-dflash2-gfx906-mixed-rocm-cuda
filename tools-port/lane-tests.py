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
"""
import subprocess

FORK_CMAKE = "/mnt/srcds/Strata-wt-merge-ipc/CMakeLists.txt"
FORK_REF = "564e1dbbea"
UP_REF = "v0.2.18"
REPO = "/mnt/srcds/Strata"


def git(*args):
    return subprocess.run(["git", "-C", REPO] + list(args),
                          capture_output=True, text=True, check=True).stdout


def to_port(p):
    """Where a fork path lives in this tree."""
    return "engine/" + p if p.startswith(("src/", "include/", "tests/", "tools/")) else p


def main():
    fork_files = [p for p in git("ls-tree", "-r", "--name-only", FORK_REF, "--", "tests/").split("\n") if p]
    upstream = set(git("ls-tree", "-r", "--name-only", UP_REF).split("\n"))
    built = set()
    for tok in subprocess.run(["grep", "-oE", r"tests/[A-Za-z0-9_/]+\.(cpp|py|sh|h|hpp)", FORK_CMAKE],
                              capture_output=True, text=True).stdout.split():
        built.add("engine/" + tok)
    rows = []
    for p in fork_files:
        mapped = to_port(p)
        if mapped in upstream:
            continue          # upstream has it; nothing is owed for it
        if mapped not in built:
            continue          # the fork does not build it either
        rows.append((p, mapped))
    for a, b in sorted(rows):
        print("%s\t%s" % (a, b))
    cpp = sum(1 for a, b in rows if b.endswith(".cpp"))
    print("# %d files (%d test programs, the rest are their fixtures and stubs)" % (len(rows), cpp))


main()

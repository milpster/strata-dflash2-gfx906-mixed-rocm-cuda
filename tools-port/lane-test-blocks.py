#!/usr/bin/env python3
"""
lane-test-blocks.py - what each of the fork's test targets needs, and whether this tree can build it.

PORT-MANIFEST-TESTS.txt answers "which files came across". It does not answer "which test programs can
this tree build, under what name, with what link line, and does that name already exist upstream" - and
those four are exactly what a wiring block gets wrong when it is copied by eye. This prints them from
the two CMake trees.

For every add_executable in the fork's CMakeLists.txt it reports:
  the sources it compiles, and whether each exists in THIS tree (a missing one is a missing file, not a
    missing block, and copying the block will not help);
  the libraries it links, its link options (--wrap=...) and its defines - the parts that differ per test;
  the conditions the fork wraps it in (the option that has to be ON, the platform guard);
  whether this tree already defines the same target name - a duplicate add_executable fails the whole
  configure (CMP0002), so a collision has to be seen before the block is written, not after;
  whether the fork builds it at all under a gfx906 configure (a block inside if(STRATA_ENABLE_CUDA) will
    never be reached here).

Read-only. Usage: python3 tools-port/lane-test-blocks.py [--target NAME] [--only-missing]
"""
import argparse
import os
import re
import sys

FORK_CMAKE = "/mnt/srcds/Strata-wt-merge-ipc/CMakeLists.txt"
PORT_ENGINE = "/mnt/srcds/Strata-wt-0218/engine"

CMDS = ("add_executable", "add_library", "target_link_libraries", "target_link_options",
        "target_compile_definitions", "target_include_directories", "add_test",
        "set_tests_properties", "option")

# a fork path -> this tree's path (same mapping as tools-port/lane-tests.py)
def to_port(p):
    if p.startswith(("src/", "include/", "tests/", "tools/")):
        return os.path.join(PORT_ENGINE, p)
    return os.path.join("/mnt/srcds/Strata-wt-0218", p)


def strip_var(p):
    """Drop ${...} prefixes/suffixes so the rest of the path can be checked on disk."""
    p = p.replace("${CMAKE_CURRENT_SOURCE_DIR}/", "").replace("${CMAKE_SOURCE_DIR}/", "")
    if "${" in p:
        return None          # cannot be resolved here (build-dir paths, generator expressions)
    return p


def commands(text):
    """Yield (cmd, args_list, line) for every recognised CMake command in text."""
    for m in re.finditer(r"(?m)^(\s*)(%s)\s*\(" % "|".join(CMDS), text):
        start = m.end() - 1                      # at the '('
        depth, i = 0, start
        while i < len(text):
            c = text[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = text[start + 1:i]
        args = [a for a in re.split(r"\s+", body.replace("\n", " ").strip()) if a]
        yield m.group(2), args, text[:m.start()].count("\n") + 1


def conditions(text):
    """Line -> the chain of if()/option() conditions enclosing it. Approximate: it reads indentation,
    not a parsed CMake program, so it can be wrong about a condition inside a string."""
    stack, enc = [], {}
    for n, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        indent = len(line) - len(line.lstrip())
        if re.match(r"(?i)^if\s*\(", s):
            stack.append((indent, s))
        elif re.match(r"(?i)^else\b|^elseif", s) and stack:
            stack[-1] = (stack[-1][0], s)
        elif re.match(r"(?i)^endif", s):
            if stack:
                stack.pop()
        enc[n] = [c for _, c in stack]
    return enc


def collect(path, base_is_engine=True):
    text = open(path, encoding="utf-8", errors="replace").read()
    enc = conditions(text)
    targets = {}
    for cmd, args, line in commands(text):
        if cmd in ("add_executable", "add_library"):
            name = args[0]
            srcs = [a for a in args[1:] if a not in ("EXCLUDE_FROM_ALL", "WIN32", "STATIC", "SHARED", "IMPORTED", "INTERFACE", "ALIAS")]
            targets[name] = {"cmd": cmd, "line": line, "srcs": srcs, "libs": [], "opts": [],
                             "defs": [], "incdirs": [], "tests": [], "conds": enc[line]}
        elif cmd.startswith("target_") and args:
            t = targets.get(args[0])
            if not t:
                continue
            rest = [a for a in args[1:] if a not in ("PRIVATE", "PUBLIC", "INTERFACE")]
            key = {"target_link_libraries": "libs", "target_link_options": "opts",
                   "target_compile_definitions": "defs", "target_include_directories": "incdirs"}[cmd]
            t[key] += [a for a in rest if a not in ("BEFORE", "AFTER")]
        elif cmd == "add_test" and args:
            name = args[0]
            m = re.match(r"NAME\s+(\S+)", " ".join(args))
            if m and m.group(1) in targets:
                targets[m.group(1)]["tests"].append(" ".join(args[1:]))
    return targets


def port_targets():
    """Every target name this tree's CMake already defines, with the file that defines it."""
    found = {}
    for root, _, files in os.walk(PORT_ENGINE):
        if "/build" in root or "/third_party" in root:
            continue
        for f in files:
            if f.endswith(".cmake") or f == "CMakeLists.txt":
                p = os.path.join(root, f)
                for name, t in collect(p).items():
                    found.setdefault(name, []).append(os.path.relpath(p, PORT_ENGINE) + ":" + str(t["line"]))
    return found


def reachable(name, t):
    """Under a gfx906 configure, is this fork block ever reached? Only the guards this port knows about
    are judged; everything else is assumed reachable and said to be unchecked."""
    for c in t["conds"]:
        cl = c.lower()
        if "strata_enable_cuda" in cl and "hip" not in cl and "and" not in cl:
            return "no: inside " + c
        if "strata_build_dsv41" in cl:
            return "no: inside " + c
        if "not strata_hip_gfx906" in cl:
            return "no: inside " + c
        if "strata_enable_mmq" in cl or "strata_prefill_mmq and" in cl:
            return "no: inside " + c
    return "yes (unchecked)" if t["conds"] else "yes (unguarded)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target")
    ap.add_argument("--only-missing", action="store_true", help="only fork targets this tree does not build")
    ap.add_argument("--only-name-missing", action="store_true", help="skip fork targets whose name already exists here")
    a = ap.parse_args()

    fork = collect(FORK_CMAKE)
    port = port_targets()
    for name in sorted(fork):
        t = fork[name]
        if a.target and name != a.target:
            continue
        here = port.get(name)
        if here and a.only_name_missing:
            continue
        missing = [s for s in (strip_var(s) for s in t["srcs"])
                   if s and not s.startswith("$") and "/" in s and not os.path.exists(to_port(s))]
        if a.only_missing and not missing and here:
            continue
        print("%s  (fork CMakeLists.txt:%d)  %s" % (name, t["line"],
              "ALREADY DEFINED HERE: " + ", ".join(here) if here else "not built by this tree"))
        print("    fork guards: %s" % (" / ".join(t["conds"]) if t["conds"] else "(none)"))
        print("    reached on gfx906: %s" % reachable(name, t))
        for s in t["srcs"]:
            p = strip_var(s)
            if p and not p.startswith("$") and "/" in p:
                print("    src  %-64s %s" % (s, "ok" if os.path.exists(to_port(p)) else "MISSING IN THIS TREE"))
            else:
                print("    src  %s" % s)
        for key, label in (("libs", "link"), ("opts", "linkopt"), ("defs", "define"), ("incdirs", "incdir")):
            if t[key]:
                print("    %-7s %s" % (label, " ".join(t[key])))
        for x in t["tests"]:
            print("    add_test %s" % x)
        print()


main()

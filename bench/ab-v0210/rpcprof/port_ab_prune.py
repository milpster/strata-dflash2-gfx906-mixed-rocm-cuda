#!/usr/bin/env python3
"""port_ab_prune.py - retire the per-boot conversation caches this harness leaves behind.

Every Phase-2 boot parks its conversation spill in out/cache/<boot key>/. The boot itself
insists on an EMPTY cache dir (port_ab_boot_plan.py --record refuses a non-empty one), so a
cache left behind is never reused: it is pure waste. It is also large - 1.4 GiB for an
8-token prefill boot, 2.8 GiB for a 1,024-token boot - and 16 boots reached 25 GiB, at which
point prerequisite 0's 40 GiB disk gate refused a legitimate boot (2026-11-24).

Two callers, one implementation:
  * port_ab_stop.sh, once the 20 s quiet check has passed, calls --purge-key: the boot is
    over, its record and engine log are written, and its cache can go.
  * a person calls the listing mode to sweep a tree after a window.

The leg's evidence is its record (records/<key>/*.json) and its engine log, both OUTSIDE the
cache dir: nothing else is ever touched. A cache dir is only removed when its boot record
(boots/<key>.json) exists AND carries ended_epoch - i.e. the boot was stopped. A cache with
no record, or with a record that never ended, is an in-flight or failed boot and is KEPT
(reported) unless --all is given; with --all and no record the dir has to be untouched by a
live process for --min-age-s seconds as well.

Usage:
  port_ab_prune.py [--out-dir DIR] [--dry-run|--apply] [--all] [--min-age-s N]
  port_ab_prune.py --purge-key KEY [--out-dir DIR]     (one boot, called by port_ab_stop.sh)

--dry-run is the default: the listing mode changes nothing unless --apply is given.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import shutil
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import port_ab_configs as pc

OUT_DEFAULT = pc.OUT_DEFAULT


def human(nbytes):
    """The du -sh shape (binary units, one decimal below 10)."""
    value = float(nbytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0


def disk_usage(path):
    """Allocated bytes under path - what du reports (sum of st_blocks*512, not apparent size)."""
    total = 0
    files = 0
    for root_dir, _dirs, names in os.walk(path):
        for name in names:
            try:
                st = (pathlib.Path(root_dir) / name).lstat()
            except OSError:
                continue
            total += st.st_blocks * 512
            files += 1
    return total, files


def cache_root(out):
    """The one directory this tool may ever delete inside: <out>/cache."""
    return pathlib.Path(out) / "cache"


def is_inside(path, root):
    """True when path is a strict descendant of root after resolving both."""
    try:
        resolved = pathlib.Path(path).resolve()
        parent = pathlib.Path(root).resolve()
    except OSError:
        return False
    if resolved == parent:
        return False
    return parent in resolved.parents


def boot_record(out, key):
    path = pathlib.Path(out) / "boots" / (key + ".json")
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def live_boot(out, key):
    """Why this boot may still be alive, or None.

    port_ab_stop.sh writes the boot's end BEFORE its 20 s quiet check (the end bounds the
    kernel-journal slice and the first stop wins), so ended_epoch alone does not prove the
    pair is gone. A supervisor pid that is still a pair_supervisor, or either of the boot's
    sockets, does - and then nothing is removed.
    """
    state = pathlib.Path(out) / "run" / (key + ".supervisor.json")
    if state.is_file():
        try:
            data = json.loads(state.read_text())
        except (OSError, ValueError):
            data = {}
        pid = data.get("supervisor_pid") if isinstance(data, dict) else None
        if isinstance(pid, int) and pid > 0:
            cmdline = pathlib.Path("/proc/%d/cmdline" % pid)
            if cmdline.parent.is_dir():
                try:
                    if "pair_supervisor" in cmdline.read_bytes().replace(b"\0", b" ").decode():
                        return "supervisor pid %d is still running" % pid
                except OSError:
                    pass
    for suffix in (".sock", "-2.sock"):
        sock = pathlib.Path(out) / "run" / ("pa-" + key + suffix)
        if sock.exists() or sock.is_symlink():
            return "socket %s still present" % sock
    return None


def leg_record(out, key):
    """The leg records of this boot (records/<key>/*.json) - evidence, never touched here."""
    directory = pathlib.Path(out) / "records" / key
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.glob("*.json") if p.is_file())


def classify(out, cache_dir, quiet_proven=False):
    """Why a cache dir may (or may not) be removed: ('remove'|'keep', reason).

    quiet_proven skips the socket/pid liveness guard: port_ab_stop.sh passes it only after
    it watched 20 s with no engine, no supervisor and no socket for this boot.
    """
    key = cache_dir.name
    record = boot_record(out, key)
    if record is None:
        return "keep", "no boot record boots/%s.json (in-flight or failed boot)" % key
    if "ended_epoch" not in record:
        return "keep", "boot record has no ended_epoch (boot never stopped)"
    if not quiet_proven:
        alive = live_boot(out, key)
        if alive:
            return "keep", alive
    if not leg_record(out, key):
        # not fatal: a discarded warmup boot legitimately has no leg record, and its cache is
        # waste all the same. The boot record + its end is the evidence that it is over.
        return "remove", "boot ended %s, no leg record (discarded boot)" % _iso(record["ended_epoch"])
    return "remove", "boot ended %s, leg records kept" % _iso(record["ended_epoch"])


def _iso(epoch):
    try:
        return datetime.datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(epoch)


def scan(out):
    """Every cache dir under <out>/cache, classified, with its disk usage."""
    root = cache_root(out)
    rows = []
    if not root.is_dir():
        return rows
    for entry in sorted(root.iterdir()):
        if entry.is_symlink():
            rows.append({"path": entry, "key": entry.name, "action": "keep",
                         "reason": "symlink - never followed", "bytes": 0, "files": 0})
            continue
        if not entry.is_dir():
            rows.append({"path": entry, "key": entry.name, "action": "keep",
                         "reason": "not a directory", "bytes": 0, "files": 0})
            continue
        action, reason = classify(out, entry)
        used, files = disk_usage(entry)
        rows.append({"path": entry, "key": entry.name, "action": action, "reason": reason,
                     "bytes": used, "files": files})
    return rows


def remove_cache(out, cache_dir):
    """Remove one cache dir, refusing anything outside <out>/cache. Returns bytes freed."""
    root = cache_root(out)
    path = pathlib.Path(cache_dir)
    if path.is_symlink() or not path.is_dir():
        raise SystemExit("REFUSING: %s is not a plain directory" % path)
    if not is_inside(path, root):
        raise SystemExit("REFUSING: %s is not inside %s" % (path, root))
    used, _files = disk_usage(path)
    shutil.rmtree(path)
    return used


def purge_key(out, key, quiet_proven=False):
    """port_ab_stop.sh's one-boot mode: purge the cache of a boot that is over.

    quiet_proven skips the socket/pid liveness guard only: port_ab_stop.sh calls this AFTER
    its own 20 s quiet check has passed, and a stale socket file can outlive the pair.
    """
    out = pathlib.Path(out)
    root = cache_root(out)
    cache_dir = root / key
    if not cache_dir.exists():
        print("cache purge: no cache dir for %s (%s)" % (key, cache_dir))
        return 0
    action, reason = classify(out, cache_dir, quiet_proven=quiet_proven)
    if action != "remove":
        print("WARNING: leaving the cache dir of %s in place (%s): %s" % (key, reason, cache_dir),
              file=sys.stderr)
        return 1
    used, files = disk_usage(cache_dir)
    remove_cache(out, cache_dir)
    print("cache purge: freed %s (%d bytes, %d files) %s"
          % (human(used), used, files, cache_dir))
    return 0


def main():
    ap = argparse.ArgumentParser(description="list or remove the harness's per-boot conversation caches")
    ap.add_argument("--out-dir", type=pathlib.Path, default=OUT_DEFAULT)
    ap.add_argument("--purge-key", metavar="KEY",
                    help="purge ONE boot's cache (port_ab_stop.sh; requires a stopped boot)")
    ap.add_argument("--quiet-proven", action="store_true",
                    help="with --purge-key: the caller watched the pair go quiet (stop.sh)")
    ap.add_argument("--dry-run", action="store_true", help="list only (the default)")
    ap.add_argument("--apply", action="store_true", help="actually remove the removable caches")
    ap.add_argument("--all", action="store_true",
                    help="also remove cache dirs whose boot has no record at all")
    ap.add_argument("--min-age-s", type=int, default=3600,
                    help="with --all, a record-less cache must be untouched for this long")
    a = ap.parse_args()
    out = pathlib.Path(a.out_dir)
    if not out.is_dir():
        print("no such out-dir: %s" % out, file=sys.stderr)
        return 2

    if a.purge_key:
        return purge_key(out, a.purge_key, quiet_proven=a.quiet_proven)
    if a.apply and a.dry_run:
        ap.error("--apply and --dry-run contradict each other")

    rows = scan(out)
    if not rows:
        print("no per-boot cache dirs under %s" % cache_root(out))
        return 0

    # a cache with no usable record may only be touched with --all, and then only when nothing
    # has written to it for --min-age-s (an in-flight boot's supervisor is still writing).
    now = time.time()
    for row in rows:
        if row["action"] == "remove" or not a.all:
            continue
        record = boot_record(out, row["key"])
        if record is not None:
            continue          # a record without ended_epoch: a live boot, never by age
        newest = 0.0
        for root_dir, _dirs, names in os.walk(row["path"]):
            for name in names:
                try:
                    newest = max(newest, (pathlib.Path(root_dir) / name).lstat().st_mtime)
                except OSError:
                    continue
        age = now - newest
        if age >= a.min_age_s:
            row["action"] = "remove"
            row["reason"] = "no boot record, untouched for %ds (--all)" % int(age)
        else:
            row["reason"] += "; untouched %ds < %ds, still live" % (int(age), a.min_age_s)

    freed = 0
    removed = kept = 0
    for row in rows:
        if row["action"] == "remove" and a.apply:
            freed += remove_cache(out, row["path"])
            removed += 1
            print("removed  %-8s %6d files  %s" % (human(row["bytes"]), row["files"], row["path"]))
        elif row["action"] == "remove":
            freed += row["bytes"]
            removed += 1
            print("would rm %-8s %6d files  %s   (%s)"
                  % (human(row["bytes"]), row["files"], row["path"], row["reason"]))
        else:
            kept += 1
            print("kept     %-8s %6d files  %s   (%s)"
                  % (human(row["bytes"]), row["files"], row["path"], row["reason"]))
    print("%s: %d cache dir(s) %s, %d kept, %s %s%s"
          % (cache_root(out), removed, "removed" if a.apply else "to remove", kept,
             human(freed), "freed" if a.apply else "would be freed",
             "" if a.apply else " (dry run - pass --apply to remove)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

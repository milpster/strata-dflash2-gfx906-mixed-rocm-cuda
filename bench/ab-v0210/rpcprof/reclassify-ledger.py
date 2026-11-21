#!/usr/bin/env python3
"""reclassify-ledger.py - re-derive `failure_class` for entries the old classifier mislabelled.

The crash watcher used to call any `refilling a lent slot failed: No buffer space available`
a VRAM exhaustion.  It is not: "No buffer space available" is errno ENOBUFS, and the only code
that produces it is the RAM pool (`ram_pool.cpp` pool_alloc, `pool_expert_cache.cpp` LRU).  The
refill loop calls `g_pool_expert_store->acquire()` directly and prints `ec.message()`, so the
message means the host pool refused - a code problem, never a margin.

The previous class is kept in `failure_class_previous` so nothing is lost.  The file is
rewritten atomically; a timestamped .bak of the original is left beside it.

Usage: reclassify-ledger.py [path]   (default run/prod-crash-ledger.jsonl)
"""
import json
import pathlib
import shutil
import sys
import time


def klass_for(error: str) -> str:
    if "could not produce a blob" in error:
        return "host-pool-admission"
    if "No buffer space available" in error or "refilling a lent slot" in error:
        return "host-pool-refill"
    if "SIGABRT" in error or "Aborted" in error:
        return "engine-abort"
    return ""


def main() -> int:
    path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "run/prod-crash-ledger.jsonl")
    backup = path.with_suffix(path.suffix + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, backup)
    out, changed = [], 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        was = rec.get("failure_class")
        now = klass_for(rec.get("error") or "")
        if now and now != was:
            rec["failure_class"] = now
            rec["failure_class_previous"] = was
            changed += 1
        out.append(json.dumps(rec))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(out) + "\n")
    tmp.replace(path)
    print("%d entries reclassified, backup %s" % (changed, backup.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())

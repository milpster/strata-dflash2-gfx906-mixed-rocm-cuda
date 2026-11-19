#!/usr/bin/env python3
"""External expert-shard page locker (2026-11-19).

One-shot mode (trials): lock the CURRENTLY RESIDENT page-cache extents of the
CPU-side expert source shards, hold, release on hold-file removal/SIGTERM.

Standing prod mode (--refresh-s N): after the first pass, every N seconds
  - if MemAvailable < floor: release the NEWEST extents until it recovers to
    floor + 512 MiB (coexistence safety, e.g. the game starting later), and
  - otherwise lock newly resident expert pages, still floor-guarded.

Pages are shared page-cache pages (engine maps MAP_SHARED|PROT_READ), so locks
made here are unevictable for the engine too. Nothing is ever faulted in from
disk: only mincore-resident pages are locked. PLE, dflash and unmapped shards
are never touched. Release: munlockall() (own mappings only) + exit.
"""
import argparse
import ctypes
import os
import signal
import time
from pathlib import Path

PAGE = os.sysconf("SC_PAGE_SIZE")
libc = ctypes.CDLL(None, use_errno=True)
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte)]
libc.mlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
libc.munlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
PROT_READ, MAP_SHARED = 1, 0x01
QUIT = {"stop": False}


def meminfo(key):
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith(key + ":"):
                return int(line.split()[1]) // 1024
    return 0


def quit(*_a):
    QUIT["stop"] = True


class Mapping:
    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path)
        fd = os.open(path, os.O_RDONLY)
        self.addr = libc.mmap(None, self.size, PROT_READ, MAP_SHARED, fd, 0)
        os.close(fd)
        if not self.addr or self.addr == ctypes.c_void_p(-1).value:
            raise RuntimeError(f"mmap failed for {path}: {os.strerror(ctypes.get_errno())}")
        self.n_pages = (self.size + PAGE - 1) // PAGE
        self.locked = bytearray(self.n_pages)   # 1 = locked by us
        self.resident = bytearray(self.n_pages) # last mincore snapshot

    def mincore_runs(self):
        vec = (ctypes.c_ubyte * self.n_pages)()
        if libc.mincore(ctypes.c_void_p(self.addr), ctypes.c_size_t(self.size), vec) != 0:
            return []
        runs, start = [], None
        for i, bit in enumerate(vec):
            self.resident[i] = 1 if (bit & 1) else 0
            if bit & 1:
                if start is None:
                    start = i
            elif start is not None:
                runs.append((start, i - start))
                start = None
        if start is not None:
            runs.append((start, self.n_pages - start))
        return runs

    def lock(self, start_pg, n_pg):
        if libc.mlock(ctypes.c_void_p(self.addr + start_pg * PAGE), ctypes.c_size_t(n_pg * PAGE)) != 0:
            return False
        for i in range(start_pg, start_pg + n_pg):
            self.locked[i] = 1
        return True

    def unlock(self, start_pg, n_pg):
        libc.munlock(ctypes.c_void_p(self.addr + start_pg * PAGE), ctypes.c_size_t(n_pg * PAGE))
        for i in range(start_pg, start_pg + n_pg):
            self.locked[i] = 0


def unlocked_sub_runs(m, start_pg, n_pg, min_pages=2):
    out, s = [], None
    for i in range(start_pg, start_pg + n_pg):
        if m.resident[i] and not m.locked[i]:
            if s is None:
                s = i
        elif s is not None:
            if i - s >= min_pages:
                out.append((s, i - s))
            s = None
    if s is not None and start_pg + n_pg - s >= min_pages:
        out.append((s, start_pg + n_pg - s))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", required=True)
    ap.add_argument("--floor-mib", type=int, default=4096)
    ap.add_argument("--hold-file", required=True)
    ap.add_argument("--refresh-s", type=int, default=0,
                    help="0 = one-shot hold (trials); N>0 = standing mode: every N s lock new resident pages / release under floor pressure")
    ap.add_argument("--heartbeat-s", type=int, default=30)
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, quit)
    signal.signal(signal.SIGINT, quit)
    Path(args.hold_file).write_text(str(os.getpid()) + "\n")
    print(f"[locker] pid={os.getpid()} page={PAGE} floor={args.floor_mib} refresh_s={args.refresh_s} "
          f"MemAvailable={meminfo('MemAvailable')} Mlocked={meminfo('Mlocked')} MiB", flush=True)

    maps, extents, seq = [], [], [0]  # extents: [seq, map_idx, start_pg, n_pg]
    locked_mib = [0]

    def try_lock(m, mi, start_pg, n_pg):
        need = n_pg * PAGE / (1 << 20)
        if meminfo("MemAvailable") - need < args.floor_mib:
            return False
        if not m.lock(start_pg, n_pg):
            return False
        seq[0] += 1
        extents.append([seq[0], mi, start_pg, n_pg])
        locked_mib[0] += need
        return True

    try:
        for path in [p.strip() for p in args.files.split(",") if p.strip()]:
            m = Mapping(path)
            mi = len(maps)
            maps.append(m)
            runs = m.mincore_runs()
            res = sum(n for _, n in runs) * PAGE / (1 << 20)
            print(f"[locker] {os.path.basename(path)}: {m.size/2**30:.2f} GiB mapped, "
                  f"{len(runs)} resident extents = {res:.1f} MiB", flush=True)
            got, skipped = 0, False
            for start, n in sorted(runs, key=lambda r: -r[1]):
                if QUIT["stop"]:
                    break
                for s2, n2 in unlocked_sub_runs(m, start, n):
                    if try_lock(m, mi, s2, n2):
                        got += n2 * PAGE / (1 << 20)
                    else:
                        skipped = True
                        break
                if skipped:
                    print(f"[locker] FLOOR reached; partial on {os.path.basename(path)} "
                          f"(locked {got:.1f} MiB so far)", flush=True)
                    break
            print(f"[locker] {os.path.basename(path)}: LOCKED {got:.1f} MiB of {res:.1f} MiB resident", flush=True)
        print(f"[locker] LOCKED-SETTLED total={locked_mib[0]:.1f} MiB "
              f"MemAvailable={meminfo('MemAvailable')} Mlocked={meminfo('Mlocked')} MiB; "
              f"refresh_s={args.refresh_s}; holding until {args.hold_file} disappears", flush=True)

        while not QUIT["stop"] and Path(args.hold_file).exists():
            if args.refresh_s > 0:
                time.sleep(args.refresh_s)
                if QUIT["stop"] or not Path(args.hold_file).exists():
                    break
                avail = meminfo("MemAvailable")
                if avail < args.floor_mib:
                    need_release = args.floor_mib + 512 - avail
                    freed = 0
                    while extents and meminfo("MemAvailable") < args.floor_mib + 512:
                        s, mi, start_pg, n_pg = extents.pop()
                        maps[mi].unlock(start_pg, n_pg)
                        freed += n_pg * PAGE / (1 << 20)
                    print(f"[locker] PRESSURE-RELEASE avail {avail} < floor {args.floor_mib}: "
                          f"unlocked {freed:.1f} MiB (newest extents); "
                          f"avail now {meminfo('MemAvailable')} MiB", flush=True)
                else:
                    added = 0
                    for mi, m in enumerate(maps):
                        for start, n in m.mincore_runs():
                            for s2, n2 in unlocked_sub_runs(m, start, n):
                                if try_lock(m, mi, s2, n2):
                                    added += n2 * PAGE / (1 << 20)
                                else:
                                    break
                    print(f"[locker] refresh locked +{added:.1f} MiB; total={locked_mib[0]:.1f} MiB; "
                          f"MemAvailable={meminfo('MemAvailable')} Mlocked={meminfo('Mlocked')} MiB", flush=True)
            else:
                time.sleep(args.heartbeat_s)
                print(f"[locker] heartbeat MemAvailable={meminfo('MemAvailable')} "
                      f"Mlocked={meminfo('Mlocked')} MiB", flush=True)
    finally:
        libc.munlockall()
        for m in maps:
            libc.munmap(ctypes.c_void_p(m.addr), ctypes.c_size_t(m.size))
        print(f"[locker] released {locked_mib[0]:.1f} MiB; MemAvailable={meminfo('MemAvailable')} "
              f"Mlocked={meminfo('Mlocked')} MiB; exiting", flush=True)


if __name__ == "__main__":
    main()

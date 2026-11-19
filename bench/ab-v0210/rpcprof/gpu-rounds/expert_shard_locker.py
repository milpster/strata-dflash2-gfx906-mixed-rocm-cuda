#!/usr/bin/env python3
"""External expert-shard page locker for the live prod engine (2026-11-19).

Locks the CURRENTLY RESIDENT page-cache extents of the CPU-side expert source
GGUF shards (UD-IQ3_XXS 00002/00003 by default). The engine maps these files
MAP_SHARED|PROT_READ, so their pages are shared page-cache pages: mlock() from
THIS process makes them unevictable for the engine too - no engine change, no
downtime, no PLE, no dflash, nothing faulted in from disk (only pages mincore
reports resident are locked).

Releases with munlockall() (own mappings only) when the hold-file disappears
or SIGTERM/SIGINT arrives.
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


class QuitDuringLock(Exception):
    pass


def resident_extents(addr, size):
    n_pages = (size + PAGE - 1) // PAGE
    vec = (ctypes.c_ubyte * n_pages)()
    if libc.mincore(ctypes.c_void_p(addr), ctypes.c_size_t(size), vec) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    runs, start = [], None
    for i, bit in enumerate(vec):
        if bit & 1:
            if start is None:
                start = i
        elif start is not None:
            runs.append((start, i - start))
            start = None
    if start is not None:
        runs.append((start, n_pages - start))
    return runs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", required=True, help="comma-separated expert source files")
    ap.add_argument("--floor-mib", type=int, default=4096,
                    help="stop locking once MemAvailable would fall below this")
    ap.add_argument("--hold-file", required=True)
    ap.add_argument("--heartbeat-s", type=int, default=30)
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, quit)
    signal.signal(signal.SIGINT, quit)
    Path(args.hold_file).write_text(str(os.getpid()) + "\n")
    print(f"[locker] pid={os.getpid()} page={PAGE} floor={args.floor_mib} MiB "
          f"MemAvailable={meminfo('MemAvailable')} Mlocked={meminfo('Mlocked')} MiB", flush=True)

    maps, locked_total = [], 0
    try:
        for path in [p.strip() for p in args.files.split(",") if p.strip()]:
            size = os.path.getsize(path)
            fd = os.open(path, os.O_RDONLY)
            addr = libc.mmap(None, size, PROT_READ, MAP_SHARED, fd, 0)
            os.close(fd)
            if addr == ctypes.c_void_p(-1).value or not addr:
                print(f"[locker] ERROR mmap {path}: {os.strerror(ctypes.get_errno())}", flush=True)
                continue
            runs = resident_extents(addr, size)
            res_mib = sum(n for _, n in runs) * PAGE / (1 << 20)
            print(f"[locker] {os.path.basename(path)}: {size/2**30:.2f} GiB mapped, "
                  f"{len(runs)} resident extents = {res_mib:.1f} MiB", flush=True)
            file_locked = 0
            for idx, (start_pg, n_pg) in enumerate(sorted(runs, key=lambda r: -r[1])):
                if QUIT["stop"]:
                    raise QuitDuringLock()
                avail = meminfo("MemAvailable")
                need = n_pg * PAGE / (1 << 20)
                if avail - need < args.floor_mib:
                    print(f"[locker] FLOOR: MemAvailable {avail} MiB - next {need:.0f} "
                          f"MiB < floor {args.floor_mib}; stopping partial "
                          f"(file_locked={file_locked/2**20:.1f} MiB)", flush=True)
                    break
                off, n = start_pg * PAGE, n_pg * PAGE
                if libc.mlock(ctypes.c_void_p(addr + off), ctypes.c_size_t(n)) != 0:
                    e = ctypes.get_errno()
                    print(f"[locker] mlock failed ({os.strerror(e)}) extent #{idx} "
                          f"{n/2**20:.1f} MiB; skipping rest of this extent only", flush=True)
                    continue
                file_locked += n
                locked_total += n
                if file_locked % (512 * (1 << 20)) < n:
                    print(f"[locker] {os.path.basename(path)}: locked {file_locked/2**20:.0f}/"
                          f"{res_mib:.0f} MiB (MemAvailable {meminfo('MemAvailable')})", flush=True)
            print(f"[locker] {os.path.basename(path)}: LOCKED {file_locked/2**20:.1f} MiB "
                  f"of {res_mib:.1f} MiB resident", flush=True)
            maps.append((path, addr, size))
        print(f"[locker] LOCKED-SETTLED total={locked_total/2**20:.1f} MiB "
              f"MemAvailable={meminfo('MemAvailable')} Mlocked={meminfo('Mlocked')} MiB; "
              f"holding until {args.hold_file} disappears", flush=True)

        while not QUIT["stop"] and Path(args.hold_file).exists():
            time.sleep(args.heartbeat_s)
            print(f"[locker] heartbeat MemAvailable={meminfo('MemAvailable')} "
                  f"Mlocked={meminfo('Mlocked')} MiB", flush=True)
    except QuitDuringLock:
        print("[locker] interrupted during lock phase; releasing what is held", flush=True)
    finally:
        libc.munlockall()
        for path, addr, size in maps:
            libc.munmap(ctypes.c_void_p(addr), ctypes.c_size_t(size))
        print(f"[locker] released; MemAvailable={meminfo('MemAvailable')} "
              f"Mlocked={meminfo('Mlocked')} MiB; exiting", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""CPU-only, 2 MiB regression probe for an inherited process THP veto.

No root, GPU, or sysfs writes. This tests the kernel mechanism used by the
opt-in arena sweep; engine registration and performance need a separate run.
"""

import ctypes
import errno
import hashlib
import mmap
from pathlib import Path
import re
import sys


def main():
    if not sys.platform.startswith("linux"):
        print("SKIP: Linux-only THP process policy")
        return
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    libc.prctl.restype = ctypes.c_int
    libc.madvise.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    libc.madvise.restype = ctypes.c_int
    get_policy = lambda: libc.prctl(42, 0, 0, 0, 0)  # PR_GET_THP_DISABLE

    def set_policy(policy):
        if libc.prctl(41, policy & 1, policy & 2, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_THP_DISABLE")

    original = get_policy()
    if original < 0:
        raise OSError(ctypes.get_errno(), "PR_GET_THP_DISABLE")
    print(f"Inherited policy: {original}", flush=True)
    huge = 2 << 20
    try:
        # Fill while disabled: no fault-time huge-page compaction in the probe.
        set_policy(1)
        with mmap.mmap(-1, 2 * huge, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS) as arena:
            address = ctypes.addressof(ctypes.c_char.from_buffer(arena))
            base = (address + huge - 1) & ~(huge - 1)
            offset = base - address
            ctypes.memset(base, 0x5A, huge)
            before = hashlib.sha256(memoryview(arena)[offset:offset + huge]).hexdigest()
            if libc.madvise(base, huge, 14) != 0:  # MADV_HUGEPAGE, after filling
                raise OSError(ctypes.get_errno(), "MADV_HUGEPAGE")
            ctypes.set_errno(0)
            refused = libc.madvise(base, huge, 25)  # MADV_COLLAPSE
            refusal = ctypes.get_errno()
            if refused != -1 or refusal != errno.EINVAL:
                raise AssertionError(f"disabled collapse: return={refused}, errno={refusal}")
            print("Disabled policy 1: MADV_COLLAPSE -> EINVAL (expected)", flush=True)
            set_policy(0)
            if libc.madvise(base, huge, 25) != 0:
                raise OSError(ctypes.get_errno(), "MADV_COLLAPSE after clearing process veto")
            set_policy(1)
            if get_policy() != 1:
                raise AssertionError("disabled policy was not restored")
            after = hashlib.sha256(memoryview(arena)[offset:offset + huge]).hexdigest()
            if after != before:
                raise AssertionError("collapse changed the filled bytes")
            in_mapping = False
            anon_huge_kib = 0
            for line in Path("/proc/self/smaps").read_text().splitlines():
                header = re.match(r"^([0-9a-f]+)-([0-9a-f]+) ", line)
                if header:
                    in_mapping = int(header[1], 16) <= base < int(header[2], 16)
                elif in_mapping and line.startswith("AnonHugePages:"):
                    anon_huge_kib = int(line.split()[1])
                    break
            if anon_huge_kib < 2048:
                raise AssertionError(f"mapping has only {anon_huge_kib} KiB AnonHugePages")
            print(f"Cleared policy: collapse OK, AnonHugePages={anon_huge_kib} KiB", flush=True)
            print("Bytes unchanged; disabled policy 1 restored", flush=True)
    finally:
        set_policy(original)
        if get_policy() != original:
            raise AssertionError("original process policy was not restored")
        print(f"Original policy restored: {original}", flush=True)


if __name__ == "__main__":
    main()

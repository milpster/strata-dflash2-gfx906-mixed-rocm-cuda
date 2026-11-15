#!/usr/bin/env python3
"""gguf_tensor_types.py — list tensor names/dims/types from a GGUF (header parse, no deps).
Usage: gguf_tensor_types.py <file.gguf> [name-filter-substring]"""
import struct, sys

T = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1", 8: "Q8_0", 9: "Q8_1",
     10: "Q2_K", 11: "Q3_K", 12: "Q4_K", 13: "Q5_K", 14: "Q6_K", 15: "Q8_K", 30: "BF16"}

def read_str(f):
    (n,) = struct.unpack("<Q", f.read(8))
    return f.read(n).decode("utf-8", "replace")

def skip_val(f, t):
    if t in (0, 1, 8): f.read(1)
    elif t in (2, 3): f.read(2)
    elif t in (4, 5, 6): f.read(4)
    elif t in (7, 11, 12): f.read(8)
    elif t == 9: read_str(f)
    elif t == 10:
        et, n = struct.unpack("<IQ", f.read(12))
        for _ in range(n): skip_val(f, et)
    else:
        raise SystemExit(f"unknown metadata type {t}")

def main(path, filt=""):
    with open(path, "rb") as f:
        assert f.read(4) == b"GGUF", "not a GGUF"
        (ver,) = struct.unpack("<I", f.read(4))
        ntensor, nkv = struct.unpack("<QQ", f.read(16))
        for _ in range(nkv):
            read_str(f)
            (t,) = struct.unpack("<I", f.read(4))
            skip_val(f, t)
        for _ in range(ntensor):
            name = read_str(f)
            (nd,) = struct.unpack("<I", f.read(4))
            dims = struct.unpack(f"<{nd}Q", f.read(8 * nd))
            (typ,) = struct.unpack("<I", f.read(4))
            f.read(8)  # offset
            if filt and filt not in name:
                continue
            print(f"{T.get(typ, typ):5s} {name} {list(dims)}")
        print(f"(gguf v{ver}, {ntensor} tensors, filter={'all' if not filt else filt})")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")

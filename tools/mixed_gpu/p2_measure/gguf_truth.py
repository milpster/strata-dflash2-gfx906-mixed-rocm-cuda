#!/usr/bin/env python3
"""Read a native-expert pack's per-layer TRUTH from the model GGUF's own tensor metadata.

The harness and the engine resolve each layer's expert shapes from the pack's
native_experts.txt; tools/iq_pack.py writes that table from the GGUF.  This script
reads the GGUF header directly - tensor names, type ids, shapes, byte offsets -
and prints, per layer, the GGUF's truth for the three expert role tensors: the
numbers the pack table must agree with.  Byte sizes per expert come from this
build's ggml (ggml_row_size, the fork's type table), not from a table copied
here: the p2 harness's TRUTH gate prints the same numbers from ggml.

Framing (verified byte-for-byte against this pack's GGUF, Swift-1.5 GSQ-RCO):
scalar values follow their type word with no padding; strings are aligned to 32
after their length word; tensor offsets are aligned to 8 after the type word;
tensor dims follow their count word packed.
Raw os.read I/O: buffered file objects stall on this mount's big files.

Usage:  gguf_truth.py <model.gguf> [layer-spec]
  layer-spec: "0-15,32-34" (default "32-34")
Prints one "TRUTH L<n>" line per requested layer with each role tensor's GGUF
type id, dims, per-expert element count, byte offset (data_start + tensor offset),
plus the GGUF's declared expert keys, and the expert-role tensors in file order
(encounter order - what a packer scanning the file sees).
"""
import os
import struct
import sys

class R:
    def __init__(self, fd):
        self.fd = fd
        self.pos = 0

    def read(self, fmt):
        sz = struct.calcsize(fmt)
        b = os.read(self.fd, sz)
        if len(b) != sz:
            raise EOFError(f"short read at {self.pos}")
        self.pos += sz
        return struct.unpack(fmt, b)[0]

    def pad(self, align):
        r = (-self.pos) % align
        if r:
            os.read(self.fd, r)
            self.pos += r

def read_string(r):
    n = r.read("<Q")
    r.pad(32)
    s = os.read(r.fd, n).decode("utf-8", "replace")
    r.pos += n
    return s

SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f",
           7: "<B", 10: "<Q", 11: "<q", 12: "<d"}

def read_value(r, t):
    if t == 8:  # GGUF_TYPE_STRING
        return read_string(r)
    r.pad(4)   # scalars are written 4-byte aligned (verified byte-for-byte)
    v = r.read(SCALARS[t])
    return chr(v) if t == 7 else v

def main():
    path = sys.argv[1]
    spec = sys.argv[2] if len(sys.argv) > 2 else "32-34"
    layers = []
    for part in spec.split(","):
        a, b = part.split("-")
        layers += list(range(int(a), int(b) + 1))
    fd = os.open(path, os.O_RDONLY)
    try:
        r = R(fd)
        if r.read("<4s") != b"GGUF":
            print("not a GGUF")
            return 1
        ver = r.read("<I")
        n_tensors = r.read("<Q")
        n_kv = r.read("<Q")
        kv = {}
        for _ in range(n_kv):
            k = read_string(r)
            r.pad(4)
            t = r.read("<I")
            if t == 9:  # GGUF_TYPE_ARRAY
                at = r.read("<I")
                r.pad(8)   # the count is 8-byte aligned (verified)
                n = r.read("<Q")
                kv[k] = [read_value(r, at) for _ in range(n)]
            else:
                kv[k] = read_value(r, t)
        infos = {}
        order = []
        for _ in range(n_tensors):
            name = read_string(r)
            nd = r.read("<I")
            dims = [r.read("<Q") for _ in range(nd)]
            dt = r.read("<I")
            r.pad(8)
            off = r.read("<Q")
            infos[name] = (dims, dt, off)
            order.append((name, dt, off))
        align = kv.get("general.alignment", 32)
        r.pad(align)
        data_start = r.pos
    finally:
        os.close(fd)
    arch = kv.get("general.architecture", "?")
    n_exp = [f"{k}={v}" for k, v in kv.items() if "expert" in k.lower() and not isinstance(v, list)]
    print(f"# GGUF v{ver} tensors {n_tensors} kv {n_kv} data_start {data_start}")
    print(f"# arch {arch}  {n_exp}")
    for l in layers:
        parts = [f"TRUTH L{l}"]
        for role in ("gate", "up", "down"):
            name = f"blk.{l}.ffn_{role}_exps.weight"
            info = infos.get(name)
            if info is None:
                parts.append(f"{role}=MISSING")
                continue
            dims, dt, off = info
            per = 1
            for d in dims:
                per *= d
            parts.append(f"{role}=type{dt} dims{dims} elems/expert{per} off{data_start + off}")
        print("  ".join(parts))
    exps = [(n, d, o) for (n, d, o) in order if "_exps.weight" in n]
    print(f"# expert-role tensors in file (encounter) order: {len(exps)}")
    for n, d, o in exps[:6]:
        print(f"#   {n} type{d} off{o}")
    return 0

if __name__ == "__main__":
    sys.exit(main())

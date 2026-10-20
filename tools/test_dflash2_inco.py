#!/usr/bin/env python3
"""Independent checks for the official Inco DFlash2 checkpoint and its BF16 GGUF.

The validator here does NOT import the converter's mapping. The tensor-name and
shape tables below are written from the runtime contract in src/core/dflash.cpp
(loader tensor names + the {H, conv_kernel_size, 2} / {H, 2*K*(H/g)} shapes it
demands) and the element formulas in src/kernels/cuda/dflash2.cu. The GGUF and
safetensors headers are parsed with stdlib structs, not with tools/gguf_reader.py,
so a reader bug cannot hide a writer bug.

Big-file checks read per-tensor mmap views and compare each tensor whole
(np.array_equal over a mapping view); nothing materializes the 1.2 GB file.

    python3 tools/test_dflash2_inco.py

Real-artifact tests skip cleanly when the files are absent (set INCO_CKPT,
INCO_GGUF, INCO_CONFIG to override the /mnt/user/dflash2 defaults).
"""
from __future__ import annotations

import json
import math
import mmap
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

CKPT = Path(os.environ.get("INCO_CKPT", "/mnt/user/dflash2/checkpoint/model.safetensors"))
GGUF = Path(os.environ.get("INCO_GGUF", "/mnt/user/dflash2/artifacts/inco-dflash2-bf16.gguf"))
CONFIG = Path(os.environ.get("INCO_CONFIG", "/mnt/user/dflash2/checkpoint/config.json"))
PINNED_SHA = "d6723f2a359eaf33ec0c95f22ae8edda896774020c552296a85e1913b4d3186f"

# --------------------------------------------------------------- stdlib readers


def safetensors_read(path: Path):
    with path.open("rb") as fh:
        (n,) = struct.unpack("<Q", fh.read(8))
        header = json.loads(fh.read(n))
    base = 8 + n
    tensors = {k: (v["dtype"], list(v["shape"]), base + v["data_offsets"][0])
               for k, v in header.items() if k != "__metadata__"}
    return tensors, path.stat().st_size - base


_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?",
            10: "<Q", 11: "<q", 12: "<d"}


def gguf_read(path: Path):
    """Independent GGUF v3 header parse with geometry checks. Returns (metadata, tensors, data_start, size)."""
    size = path.stat().st_size
    meta: dict[str, object] = {}
    tensors: list[dict] = []
    with path.open("rb") as fh:
        if struct.unpack("<I", fh.read(4))[0] != 0x46554747:
            raise ValueError("not a GGUF v3 file")
        version, n_tensors, n_kv = struct.unpack("<IQQ", fh.read(20))
        if version != 3:
            raise ValueError(f"GGUF v{version}, expected v3")
        for _ in range(n_kv):
            key = _str(fh)
            (tid,) = struct.unpack("<I", fh.read(4))
            if tid in _SCALARS:
                meta[key] = struct.unpack(_SCALARS[tid], fh.read(struct.calcsize(_SCALARS[tid])))[0]
            elif tid == 8:
                meta[key] = _str(fh)
            elif tid == 9:
                (etype,) = struct.unpack("<I", fh.read(4))
                (count,) = struct.unpack("<Q", fh.read(8))
                fmt = _SCALARS[etype]
                meta[key] = [struct.unpack(fmt, fh.read(struct.calcsize(fmt)))[0] for _ in range(count)]
            else:
                raise ValueError(f"metadata type {tid} of {key}")
        for _ in range(n_tensors):
            name = _str(fh)
            (n_dims,) = struct.unpack("<I", fh.read(4))
            shape = list(struct.unpack(f"<{n_dims}Q", fh.read(8 * n_dims)))
            type_id, offset = struct.unpack("<IQ", fh.read(12))
            tensors.append({"name": name, "shape": shape, "type": type_id, "offset": offset})
        pos = fh.tell()
    alignment = meta.get("general.alignment")
    alignment = alignment if isinstance(alignment, int) and alignment else 32
    data_start = (pos + alignment - 1) // alignment * alignment

    # Geometry: every tensor is BF16 (type 30), dims positive, offset aligned,
    # byte ranges inside the data section, and strictly ordered without overlap.
    seen_end = 0
    for t in tensors:
        if t["type"] != 30:
            raise ValueError(f"{t['name']}: not BF16 (type {t['type']})")
        if any(d == 0 for d in t["shape"]):
            raise ValueError(f"{t['name']}: zero dimension")
        if t["offset"] % alignment:
            raise ValueError(f"{t['name']}: offset {t['offset']} not {alignment}-aligned")
        nbytes = math.prod(t["shape"]) * 2
        if t["offset"] < seen_end:
            raise ValueError(f"{t['name']}: overlapping payload")
        if data_start + t["offset"] + nbytes > size:
            raise ValueError(f"{t['name']}: payload past the file end")
        t["bytes"] = nbytes
        seen_end = t["offset"] + nbytes
        seen_end = (seen_end + alignment - 1) // alignment * alignment
    return meta, tensors, data_start, size


def _str(fh) -> str:
    (n,) = struct.unpack("<Q", fh.read(8))
    return fh.read(n).decode("utf-8")


def _f32(u16: np.ndarray) -> np.ndarray:
    """BF16 bits -> float32 (exact widening: exponent+mantissa into the top bits)."""
    return (u16.astype("<u4") << np.uint32(16)).view("<f4")


# ------------------------------------------------- runtime contract tables (from dflash.cpp / dflash2.cu)
def names_and_shapes(cfg: dict) -> dict[str, tuple[str, list[int], list[int]]]:
    """torch name -> (GGUF name, torch shape, GGUF shape). GGUF = torch dims reversed
    (dim 0 varies fastest), row-major bytes untouched - the llama.cpp convention."""
    H = cfg["hidden_size"]
    I = cfg["intermediate_size"]
    D = cfg["head_dim"]
    Q = cfg["num_attention_heads"] * D
    KV = cfg["num_key_value_heads"] * D
    V = cfg["vocab_size"]
    L = cfg["num_hidden_layers"]
    K = cfg["conv_kernel_size"]
    g = cfg["conv_group_size"]
    R = cfg["selector_rank"]
    F = H * len(cfg["target_layer_ids"])
    P = 2 * K * (H // g)
    table: dict[str, tuple[str, list[int], list[int]]] = {
        "fc.weight": ("fc.weight", [H, F], [F, H]),
        "hidden_norm.weight": ("enc.output_norm.weight", [H], [H]),
        "norm.weight": ("output_norm.weight", [H], [H]),
        "candidate_selector.predecessor_codebook": ("selector_predecessor.weight", [V, R], [R, V]),
        "candidate_selector.successor_codebook": ("selector_successor.weight", [V, R], [R, V]),
        "candidate_selector.hidden_projection.weight": ("selector_hidden.weight", [R, H], [H, R]),
    }
    per = {"input_layernorm.weight": ("attn_norm.weight", [H], [H]),
           "post_attention_layernorm.weight": ("ffn_norm.weight", [H], [H]),
           "self_attn.q_proj.weight": ("attn_q.weight", [Q, H], [H, Q]),
           "self_attn.k_proj.weight": ("attn_k.weight", [KV, H], [H, KV]),
           "self_attn.v_proj.weight": ("attn_v.weight", [KV, H], [H, KV]),
           "self_attn.o_proj.weight": ("attn_output.weight", [H, Q], [Q, H]),
           "self_attn.q_norm.weight": ("attn_q_norm.weight", [D], [D]),
           "self_attn.k_norm.weight": ("attn_k_norm.weight", [D], [D]),
           "mlp.gate_proj.weight": ("ffn_gate.weight", [I, H], [H, I]),
           "mlp.up_proj.weight": ("ffn_up.weight", [I, H], [H, I]),
           "mlp.down_proj.weight": ("ffn_down.weight", [H, I], [I, H])}
    for l in range(L):
        for hf, (gg, ts, gs) in per.items():
            table[f"layers.{l}.{hf}"] = (f"blk.{l}.{gg}", ts, gs)
        for hf, gg in (("attention_conv", "attn_conv"), ("mlp_conv", "ffn_conv")):
            table[f"layers.{l}.{hf}.base_kernel"] = (f"blk.{l}.{gg}_base", [2, K, H], [H, K, 2])
            table[f"layers.{l}.{hf}.kernel_projection.weight"] = (f"blk.{l}.{gg}_proj.weight", [P, H], [H, P])
    return table


# ------------------------------------------------------------- artifact checks
@unittest.skipUnless(GGUF.exists() and CKPT.exists(), "Inco checkpoint/artifact not present")
class IncoArtifact(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = json.loads(CONFIG.read_text())
        cls.cfg.update(cls.cfg["dflash_config"])
        cls.table = names_and_shapes(cls.cfg)
        cls.src, cls.src_span = safetensors_read(CKPT)
        cls.meta, cls.tensors, cls.data_start, cls.size = gguf_read(GGUF)
        cls.by = {t["name"]: t for t in cls.tensors}

    def _region(self, mm: mmap.mmap, off: int, nbytes: int) -> np.ndarray:
        return np.frombuffer(mm, dtype="<u2", count=nbytes // 2, offset=off)

    def test_tensor_set_and_mapping(self):
        self.assertEqual(len(self.tensors), len(self.table))
        self.assertEqual(sorted(self.by), sorted(g for g, _, _ in self.table.values()))
        self.assertEqual(sorted(self.src), sorted(self.table))

    def test_shapes_orientation(self):
        for torch_name, (gguf_name, ts, gs) in self.table.items():
            with self.subTest(name=gguf_name):
                self.assertEqual(list(self.src[torch_name][1]), ts)
                self.assertEqual(self.by[gguf_name]["shape"], gs)

    def test_exact_bf16_bytes_all_81(self):
        with CKPT.open("rb") as cs, GGUF.open("rb") as gs:
            sm, gm = mmap.mmap(cs.fileno(), 0, access=mmap.ACCESS_READ), mmap.mmap(gs.fileno(), 0, access=mmap.ACCESS_READ)
            for torch_name, (gguf_name, ts, _) in self.table.items():
                _, _, off = self.src[torch_name]
                nbytes = math.prod(ts) * 2
                a = self._region(sm, off, nbytes)
                b = self._region(gm, self.data_start + self.by[gguf_name]["offset"], nbytes)
                self.assertTrue(np.array_equal(a, b), f"{gguf_name}: raw BF16 bytes differ")
            del a, b          # last np.frombuffer views pin the mmaps
            sm.close(); gm.close()

    def test_conv_index_semantics(self):
        """conv_kernel reads base[(side*K + tap)*hidden + channel]. GGUF dims [H,K,2]
        put channel fastest, so the GGUF flat index of (channel,tap,side) is
        channel + tap*dims[0] + side*dims[0]*dims[1] - identical arithmetic.
        Spot-check every side/tap/channel corner through BOTH index formulas."""
        H, K = self.cfg["hidden_size"], self.cfg["conv_kernel_size"]
        samples = [(0, 0, 0), (0, K - 1, H - 1), (1, 0, 128), (1, 1, H // 2 - 1), (0, 1, 1279)]
        with CKPT.open("rb") as cs, GGUF.open("rb") as gs:
            sm, gm = mmap.mmap(cs.fileno(), 0, access=mmap.ACCESS_READ), mmap.mmap(gs.fileno(), 0, access=mmap.ACCESS_READ)
            for l in range(self.cfg["num_hidden_layers"]):
                for hf in ("attention_conv", "mlp_conv"):
                    _, ts, _ = self.table[f"layers.{l}.{hf}.base_kernel"]
                    gguf_name = self.table[f"layers.{l}.{hf}.base_kernel"][0]
                    dims = self.by[gguf_name]["shape"]
                    self.assertEqual(dims, [H, K, 2])
                    for side, tap, channel in samples:
                        kernel_idx = (side * K + tap) * H + channel
                        gguf_idx = channel + tap * dims[0] + side * dims[0] * dims[1]
                        self.assertEqual(kernel_idx, gguf_idx)
                        sv = bytes(sm[self.src[f"layers.{l}.{hf}.base_kernel"][2] + 2 * kernel_idx:
                                     self.src[f"layers.{l}.{hf}.base_kernel"][2] + 2 * kernel_idx + 2])
                        gv = bytes(gm[self.data_start + self.by[gguf_name]["offset"] + 2 * gguf_idx:
                                      self.data_start + self.by[gguf_name]["offset"] + 2 * gguf_idx + 2])
                        self.assertEqual(sv, gv, f"{gguf_name} ({side},{tap},{channel})")
            sm.close(); gm.close()

    def test_selector_geometry(self):
        V, R = self.cfg["vocab_size"], self.cfg["selector_rank"]
        for n in ("selector_predecessor.weight", "selector_successor.weight"):
            self.assertEqual(self.by[n]["shape"], [R, V])
        self.assertEqual(self.by["selector_hidden.weight"]["shape"], [self.cfg["hidden_size"], R])

    def test_metadata_contract(self):
        want = {
            "general.architecture": "dflash",
            "dflash.embedding_length": self.cfg["hidden_size"],
            "dflash.block_count": self.cfg["num_hidden_layers"],
            "dflash.attention.head_count": self.cfg["num_attention_heads"],
            "dflash.attention.head_count_kv": self.cfg["num_key_value_heads"],
            "dflash.attention.key_length": self.cfg["head_dim"],
            "dflash.feed_forward_length": self.cfg["intermediate_size"],
            "dflash.vocab_size": self.cfg["vocab_size"],
            "dflash.block_size": self.cfg["block_size"],
            "dflash.mask_token_id": self.cfg["mask_token_id"],
            "dflash.target_layers": [5, 15, 25, 35, 45],
            "dflash.rope.frequency_base": 10000.0,
            "dflash.sample_from_anchor": "false",
            "dflash.attention.causal": "false",
            "dflash.conv_kernel_size": self.cfg["conv_kernel_size"],
            "dflash.conv_group_size": self.cfg["conv_group_size"],
            "dflash.selector_rank": self.cfg["selector_rank"],
            "dflash.selector_top_k": self.cfg["selector_top_k"],
            "dflash.target_layer_inputs": "false",
            "dflash.attention.sliding_window": self.cfg["sliding_window"],
            "dflash.target_hc_mean": "true",
            "dflash.markov_rank": 0,
            "dflash.has_confidence_head": "false",
        }
        for key, value in want.items():
            with self.subTest(key=key):
                self.assertEqual(self.meta[key], value)

    def test_source_provenance(self):
        h = __import__("hashlib").sha256()
        with CKPT.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 24), b""):
                h.update(chunk)
        self.assertEqual(h.hexdigest(), PINNED_SHA)
        self.assertEqual(self.meta.get("dflash.source.sha256"), PINNED_SHA)

    def test_projection_parity(self):
        """Deterministic sampled projection parity: fixed FP32 inputs against sampled
        rows/columns of the artifact's weights and the checkpoint, widened BF16->FP32
        independently from raw bytes on both sides. This is artifact/weight parity only -
        not an independent runtime numerical forward."""
        rng = np.random.default_rng(42)
        plan = [("fc.weight", [0, 1000, 2559], 12800),
                ("layers.0.self_attn.q_proj.weight", [0, 2048, 4095], 2560),
                ("layers.0.self_attn.k_proj.weight", [0, 512, 1023], 2560),
                ("layers.0.self_attn.v_proj.weight", [0, 512, 1023], 2560),
                ("candidate_selector.hidden_projection.weight", [0, 127, 255], 2560)]
        checked = exact = 0
        worst = 0.0
        dots_exact = 0
        with CKPT.open("rb") as cs, GGUF.open("rb") as gs:
            sm, gm = mmap.mmap(cs.fileno(), 0, access=mmap.ACCESS_READ), mmap.mmap(gs.fileno(), 0, access=mmap.ACCESS_READ)
            for torch_name, rows, width in plan:
                gguf_name = self.table[torch_name][0]
                base_s = self.src[torch_name][2]
                base_g = self.data_start + self.by[gguf_name]["offset"]
                cols = np.sort(rng.choice(width, 64, replace=False))
                vec = rng.standard_normal(64, dtype=np.float32)
                for r in rows:
                    idx = r * width + cols
                    va = _f32(np.array([int.from_bytes(sm[base_s + 2 * i: base_s + 2 * i + 2], "little") for i in idx], dtype="<u2"))
                    vb = _f32(np.array([int.from_bytes(gm[base_g + 2 * i: base_g + 2 * i + 2], "little") for i in idx], dtype="<u2"))
                    checked += 64
                    exact += int(np.array_equal(va, vb))
                    worst = max(worst, float(np.max(np.abs(va - vb))))
                    dots_exact += int(np.dot(va, vec) == np.dot(vb, vec))
            sm.close(); gm.close()
        self.assertEqual(checked, 15 * 64)
        self.assertEqual(exact, 15)
        self.assertEqual(dots_exact, 15)
        self.assertEqual(worst, 0.0)
        self.assertTrue(np.allclose(va, vb, rtol=0.0, atol=0.0))
        self._parity_report = (checked, exact, worst)


# ------------------------------------------------------- converter refusals (synthetic)
MINI = dict(architectures=["DFlash2DraftModel"], hidden_size=32, intermediate_size=32,
            head_dim=8, num_attention_heads=4, num_key_value_heads=1, num_hidden_layers=2,
            vocab_size=64, rope_parameters={"rope_theta": 1e7}, rms_norm_eps=1e-6,
            dflash_config=dict(block_size=3, mask_token_id=7, target_layer_ids=[0, 1],
                               conv_kernel_size=2, conv_group_size=8, selector_rank=4,
                               selector_top_k=3, sample_from_anchor=False))


def _mini_source(root: Path, cfg: dict, drop: str | None = None, extra: str | None = None,
                 reshape: tuple[str, list[int]] | None = None, config: dict | None = None):
    import dflash_gguf as d
    want = d.expected(d.normalize_config(cfg))
    header, chunks, offset = {}, [], 0
    for name, shape in want.items():
        if drop and name.endswith(drop):
            continue
        if reshape and name == reshape[0]:
            shape = reshape[1]
        raw = (np.arange(math.prod(shape), dtype=np.uint16) % 16 + 0x3c00).astype("<u2").tobytes()
        header[name] = dict(dtype="BF16", shape=list(shape), data_offsets=[offset, offset + len(raw)])
        chunks.append(raw)
        offset += len(raw)
    if extra:
        header[extra] = dict(dtype="BF16", shape=[1], data_offsets=[offset, offset + 2])
        chunks.append(b"\x00\x00")
        offset += 2
    encoded = json.dumps(header).encode()
    source = root / "model.safetensors"
    source.write_bytes(struct.pack("<Q", len(encoded)) + encoded + b"".join(chunks))
    (root / "config.json").write_text(json.dumps(config if config is not None else cfg))
    return source


def _run_converter(source: Path, out: Path, config: Path | None = None):
    return subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "dflash_gguf.py"),
                           str(source), "-o", str(out)] + ([ "--config", str(config)] if config else []),
                          capture_output=True, text=True)


class ConverterRefusals(unittest.TestCase):
    """A good artifact schema needs a refusal for every critical field this checkpoint family relies on."""

    def test_missing_tensor(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = _mini_source(Path(tmp), MINI, drop="base_kernel")
            r = _run_converter(src, Path(tmp) / "d.gguf")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("missing tensors", r.stdout + r.stderr)

    def test_unexpected_tensor(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = _mini_source(Path(tmp), MINI, extra="lm_head.weight")
            r = _run_converter(src, Path(tmp) / "d.gguf")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("unexpected tensors", r.stdout + r.stderr)

    def test_bad_dtype(self):
        import dflash_gguf as d
        with tempfile.TemporaryDirectory() as tmp:
            src = _mini_source(Path(tmp), MINI)
            raw = bytearray(src.read_bytes())
            header_len = struct.unpack("<Q", raw[:8])[0]
            head = json.loads(raw[8:8 + header_len])
            head["fc.weight"]["dtype"] = "F32"
            enc = json.dumps(head).encode()
            src.write_bytes(struct.pack("<Q", len(enc)) + enc + bytes(raw[8 + header_len:]))
            with self.assertRaisesRegex(ValueError, "BF16"):
                d.read_safetensors(src)

    def test_wrong_3d_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = _mini_source(Path(tmp), MINI, reshape=("layers.0.attention_conv.base_kernel", [32, 2, 2]))
            r = _run_converter(src, Path(tmp) / "d.gguf")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("shape", r.stdout + r.stderr)

    def test_selector_geometry(self):
        import copy
        import dflash_gguf as d
        for key, value in (("selector_rank", 0), ("selector_rank", 2048), ("selector_top_k", 65)):
            cfg = copy.deepcopy(MINI)
            cfg["dflash_config"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                d.normalize_config(cfg)

    def test_conv_geometry(self):
        import copy
        import dflash_gguf as d
        cfg = copy.deepcopy(MINI)
        cfg["dflash_config"]["conv_group_size"] = 3
        with self.assertRaisesRegex(ValueError, "geometry"):
            d.normalize_config(cfg)

    def test_unknown_architecture_is_refused_by_the_converter(self):
        import copy
        cfg = copy.deepcopy(MINI)
        cfg["architectures"] = ["DFlash3DraftModel"]
        cfg.update(cfg.pop("dflash_config"))     # plain layout: the refusal is about the architecture
        cfg["sample_from_anchor"] = True
        build = copy.deepcopy(cfg); build.pop("architectures")   # tensors are plain-geometry
        with tempfile.TemporaryDirectory() as tmp:
            src = _mini_source(Path(tmp), build, config=cfg)
            r = _run_converter(src, Path(tmp) / "d.gguf")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("unsupported architectures", r.stdout + r.stderr)

    def test_known_dflash1_arch_keeps_the_plain_path(self):
        import copy
        cfg = copy.deepcopy(MINI)
        cfg["architectures"] = ["DFlashDraftModel"]
        cfg.update(cfg.pop("dflash_config"))
        cfg["sample_from_anchor"] = True
        with tempfile.TemporaryDirectory() as tmp:
            src = _mini_source(Path(tmp), cfg)
            out = Path(tmp) / "d.gguf"
            r = _run_converter(src, out)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("SELF-CHECK PASS", r.stdout)
            meta, tensors, _, _ = gguf_read(out)
            self.assertEqual(meta["dflash.sample_from_anchor"], "true")
            self.assertFalse(any("conv" in t["name"] or "selector" in t["name"] for t in tensors))

    def test_no_architectures_key_stays_legacy_plain(self):
        import copy
        cfg = copy.deepcopy(MINI)
        cfg.pop("architectures")
        cfg.update(cfg.pop("dflash_config"))
        cfg["sample_from_anchor"] = True
        with tempfile.TemporaryDirectory() as tmp:
            src = _mini_source(Path(tmp), cfg)
            out = Path(tmp) / "d.gguf"
            r = _run_converter(src, out)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_dropout_and_target_layers(self):
        import copy
        import dflash_gguf as d
        cfg = copy.deepcopy(MINI)
        cfg["attention_dropout"] = 0.1
        with self.assertRaisesRegex(ValueError, "dropout"):
            d.normalize_config(cfg)
        cfg = copy.deepcopy(MINI)
        cfg["num_target_layers"] = 2
        cfg["dflash_config"]["target_layer_ids"] = [1, 5]
        with self.assertRaisesRegex(ValueError, "num_target_layers"):
            d.normalize_config(cfg)
        cfg = copy.deepcopy(MINI)
        cfg["num_target_layers"] = 48
        d.normalize_config(cfg)  # present and valid: accepted, unchanged geometry
        self.assertEqual(d.normalize_config(cfg)["num_target_layers"], 48)


class ValidatorRejectsCorrupt(unittest.TestCase):
    """The validator must refuse a GGUF whose offsets/shapes lie, not just trust the writer.

    Directory entry layout: [8B name length][name][4B n_dims][8B x n_dims dims][4B type][8B offset].
    The writer pads 32 bytes after every tensor, including the last.
    """

    def _tiny(self, root: Path) -> Path:
        from gguf_writer import GGUFWriter
        w = GGUFWriter()
        w.add("general.architecture", "dflash")
        w.add("dflash.block_count", 2, "u32")
        w.add_bf16("fc.weight", np.arange(12, dtype=np.float32).reshape(3, 4))
        w.add_bf16("output_norm.weight", np.ones(4, dtype=np.float32), shape=[4])
        p = root / "tiny.gguf"
        w.write(p)
        return p

    @staticmethod
    def _entry(raw: bytearray, name: str):
        i = raw.index(name.encode()) + len(name)      # past the name bytes
        return i                                       # n_dims at i, dims at i+4, type at i+12, offset at i+16 (1-D tensor)

    def test_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._tiny(Path(tmp))
            p.write_bytes(p.read_bytes()[:-32])       # reaches the last payload (norm ends 40 B into data)
            with self.assertRaisesRegex(ValueError, "payload past the file end"):
                gguf_read(p)

    def test_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._tiny(Path(tmp))
            raw = bytearray(p.read_bytes())
            i = self._entry(raw, "output_norm.weight")
            raw[i + 16: i + 24] = struct.pack("<Q", 0)   # second tensor points at offset 0
            p.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "overlap"):
                gguf_read(p)

    def test_unaligned(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._tiny(Path(tmp))
            raw = bytearray(p.read_bytes())
            i = self._entry(raw, "output_norm.weight")
            raw[i + 16: i + 24] = struct.pack("<Q", 1)
            p.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "aligned"):
                gguf_read(p)

    def test_non_bf16(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._tiny(Path(tmp))
            raw = bytearray(p.read_bytes())
            i = self._entry(raw, "output_norm.weight")
            raw[i + 12: i + 16] = struct.pack("<I", 20)
            p.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "not BF16"):
                gguf_read(p)

    def test_bad_magic(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._tiny(Path(tmp))
            p.write_bytes(b"GGSF" + p.read_bytes()[4:])
            with self.assertRaisesRegex(ValueError, "not a GGUF"):
                gguf_read(p)


if __name__ == "__main__":
    unittest.main()

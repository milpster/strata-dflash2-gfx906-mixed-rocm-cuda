#!/usr/bin/env python3
"""Safetensors reader + canonical mapping for the Inco DFlash2 checkpoint (authority).

The checkpoint at /mnt/user/dflash2/checkpoint (incoai/Qwen3.8-Flash-Next-DFlash2,
rev a22a1744e85c17364dcc54a6a420af7135d4c7f6, model.safetensors sha256
d6723f2a359eaf33ec0c95f22ae8edda896774020c552296a85e1913b4d3186f, 81 BF16 tensors,
metadata: none) is the REFERENCE AUTHORITY for drafter numerics: safetensors first,
never GGUF-only self-checks.  Tensors load lazily per name via memmap (one CPU
thread, layerwise; prod-RAM friendly).  The safetensors package is preferred; a
stdlib header+memmap fallback keeps this dependency-free (no Torch, no ROCm).

Canonical names follow the GGUF keys tools/dflash2_stage_parity.py consumes, so the
SAME oracle checks both sources (no duplicated formulas).  The checkpoint holds NO
lm_head/output tensors (config: tie_word_embeddings false) - the head is the target's;
callers must report head truth as NOTRUN, not fabricate it.
"""
from __future__ import annotations

import json
import pathlib
import struct
import sys

import numpy as np

EXPECTED_SHA256 = "d6723f2a359eaf33ec0c95f22ae8edda896774020c552296a85e1913b4d3186f"
HF_REV = "a22a1744e85c17364dcc54a6a420af7135d4c7f6"

# safetensors (torch [out, in]) -> the oracle's canonical weight key.
NAME_MAP = {
    "fc.weight": "fc.weight",
    "hidden_norm.weight": "enc.output_norm.weight",
    "norm.weight": "output_norm.weight",
    "candidate_selector.hidden_projection.weight": "selector_hidden.weight",
    "candidate_selector.predecessor_codebook": "selector_predecessor.weight",
    "candidate_selector.successor_codebook": "selector_successor.weight",
}
LAYER_MAP = {
    "input_layernorm.weight": "attn_norm.weight",
    "post_attention_layernorm.weight": "ffn_norm.weight",
    "self_attn.q_proj.weight": "attn_q.weight",
    "self_attn.k_proj.weight": "attn_k.weight",
    "self_attn.v_proj.weight": "attn_v.weight",
    "self_attn.o_proj.weight": "attn_output.weight",
    "self_attn.q_norm.weight": "attn_q_norm.weight",
    "self_attn.k_norm.weight": "attn_k_norm.weight",
    "mlp.gate_proj.weight": "ffn_gate.weight",
    "mlp.up_proj.weight": "ffn_up.weight",
    "mlp.down_proj.weight": "ffn_down.weight",
    "attention_conv.base_kernel": "attn_conv_base",
    "attention_conv.kernel_projection.weight": "attn_conv_proj.weight",
    "mlp_conv.base_kernel": "ffn_conv_base",
    "mlp_conv.kernel_projection.weight": "ffn_conv_proj.weight",
}

DTYPE_CODE = {"BF16": "<u2", "F32": "<f4", "F16": "<u2", "I32": "<i4", "I64": "<i8", "U8": "u1"}


class Checkpoint:
    """Lazy per-tensor reader over model.safetensors in a checkpoint directory."""

    def __init__(self, directory: str | pathlib.Path):
        self.dir = pathlib.Path(directory)
        self.st = self.dir / "model.safetensors"
        cfg = self.dir / "config.json"
        if not self.st.exists():
            raise ValueError(f"checkpoint: {self.st} missing")
        if not cfg.exists():
            raise ValueError("checkpoint: config.json missing")
        self.config = json.loads(cfg.read_text())
        self.header, self.data_start = self._read_header()

    def _read_header(self) -> tuple[dict, int]:
        with open(self.st, "rb") as f:
            n = struct.unpack("<Q", f.read(8))[0]
            if n <= 0 or n > 100_000_000:
                raise ValueError(f"checkpoint: header length {n} implausible (malformed safetensors)")
            header = json.loads(f.read(n))
        if "__metadata__" in header and header["__metadata__"]:
            raise ValueError("checkpoint: unexpected safetensors metadata (the Inco file has none)")
        off = min(t["data_offsets"][0] for k, t in header.items() if k != "__metadata__")
        return header, 8 + n + off

    def names(self) -> list[str]:
        return sorted(k for k in self.header if k != "__metadata__")

    def tensor(self, name: str) -> np.ndarray:
        """One tensor as F32 in torch [out, in] layout - exactly what the oracle's W dict holds."""
        t = self.header.get(name)
        if t is None:
            raise ValueError(f"checkpoint: tensor {name} missing")
        s, e = t["data_offsets"]
        shape = tuple(t["shape"])
        code = DTYPE_CODE.get(t["dtype"])
        if code is None:
            raise ValueError(f"checkpoint: tensor {name} dtype {t['dtype']} unsupported")
        n = (e - s) // np.dtype(code).itemsize
        if n != int(np.prod(shape)):
            raise ValueError(f"checkpoint: tensor {name} byte span implies {n} elements, shape says {shape}")
        raw = np.memmap(self.st, mode="r", dtype=code, offset=self.data_start + s, shape=(n,))
        if t["dtype"] == "BF16":
            out = (raw.astype("<u4") << np.uint32(16)).view("<f4")
        else:
            out = raw.astype(np.float32)
        return out.reshape(shape)


def meta_from_config(cfg: dict) -> dict:
    """config.json -> the oracle's dflash.* metadata keys (the same values the converter writes)."""
    d = cfg.get("dflash_config", {})
    need = ["block_size", "conv_group_size", "conv_kernel_size", "mask_token_id",
            "selector_rank", "selector_top_k", "target_layer_ids"]
    for k in need:
        if k not in d:
            raise ValueError(f"config.json dflash_config.{k} missing")
    for k in ("hidden_size", "intermediate_size", "num_attention_heads", "num_key_value_heads",
              "head_dim", "num_hidden_layers", "num_target_layers", "vocab_size",
              "sliding_window", "rms_norm_eps"):
        if k not in cfg:
            raise ValueError(f"config.json {k} missing")
    rope = cfg.get("rope_parameters", {}).get("rope_theta")
    if rope is None:
        raise ValueError("config.json rope_parameters.rope_theta missing")
    if cfg.get("is_causal") is not False or cfg.get("use_sliding_window") is not True:
        raise ValueError("config.json must declare is_causal false + use_sliding_window true (the capture contract)")
    return {
        "dflash.embedding_length": cfg["hidden_size"],
        "dflash.attention.key_length": cfg["head_dim"],
        "dflash.attention.head_count": cfg["num_attention_heads"],
        "dflash.attention.head_count_kv": cfg["num_key_value_heads"],
        "dflash.conv_kernel_size": d["conv_kernel_size"],
        "dflash.conv_group_size": d["conv_group_size"],
        "dflash.selector_rank": d["selector_rank"],
        "dflash.selector_top_k": d["selector_top_k"],
        "dflash.block_count": cfg["num_hidden_layers"],
        "dflash.target_layers": d["target_layer_ids"],
        "dflash.mask_token_id": d["mask_token_id"],
        "dflash.block_size": d["block_size"],
        "dflash.attention.sliding_window": cfg["sliding_window"],
        "dflash.rope.frequency_base": rope,
        "dflash.vocab_size": cfg["vocab_size"],
        "dflash.rms_norm_eps": cfg["rms_norm_eps"],
        "dflash.num_target_layers": cfg["num_target_layers"],
    }


def canonical_weights(cp: Checkpoint, *, layers: int | None = None) -> dict[str, np.ndarray]:
    """Load the canonical W dict the oracle consumes, refusing missing/mis-shaped entries.

    The oracle indexes W directly; a missing canonical name is a hard failure, never a
    silent skip.  Shapes follow the Inco geometry (hidden 2560, head 128, 32/8 heads,
    conv 2x2 grouped 16, selector 256/16, vocab 248320)."""
    cfg = cp.config
    H, D = cfg["hidden_size"], cfg["head_dim"]
    nq, nkv = cfg["num_attention_heads"], cfg["num_key_value_heads"]
    L = layers or cfg["num_hidden_layers"]
    inter, vocab = cfg["intermediate_size"], cfg["vocab_size"]
    taps = cfg["dflash_config"]["conv_kernel_size"]
    want_shape = {
        "fc.weight": (H, H * len(cfg["dflash_config"]["target_layer_ids"])),
        "enc.output_norm.weight": (H,),
        "output_norm.weight": (H,),
        "selector_hidden.weight": (cfg["dflash_config"]["selector_rank"], H),
        "selector_predecessor.weight": (vocab, cfg["dflash_config"]["selector_rank"]),
        "selector_successor.weight": (vocab, cfg["dflash_config"]["selector_rank"]),
    }
    layer_shape = {
        "attn_norm.weight": (H,), "ffn_norm.weight": (H,),
        "attn_q.weight": (nq * D, H), "attn_k.weight": (nkv * D, H), "attn_v.weight": (nkv * D, H),
        "attn_output.weight": (H, nq * D), "attn_q_norm.weight": (D,), "attn_k_norm.weight": (D,),
        "ffn_gate.weight": (inter, H), "ffn_up.weight": (inter, H), "ffn_down.weight": (H, inter),
        "attn_conv_base": (2, taps, H), "attn_conv_proj.weight": (2 * taps * (H // cfg["dflash_config"]["conv_group_size"]), H),
        "ffn_conv_base": (2, taps, H), "ffn_conv_proj.weight": (2 * taps * (H // cfg["dflash_config"]["conv_group_size"]), H),
    }
    W: dict[str, np.ndarray] = {}
    for src, canon in NAME_MAP.items():
        t = cp.tensor(src)
        if t.shape != want_shape[canon]:
            raise ValueError(f"checkpoint: {src} shape {t.shape} != expected {want_shape[canon]}")
        W[canon] = t
    for l in range(L):
        for src, canon in LAYER_MAP.items():
            t = cp.tensor(f"layers.{l}.{src}")
            if t.shape != layer_shape[canon]:
                raise ValueError(f"checkpoint: layers.{l}.{src} shape {t.shape} != expected {layer_shape[canon]}")
            W[f"blk.{l}.{canon}"] = t
    return W


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--checkpoint", default="/mnt/user/dflash2/checkpoint")
    ap.add_argument("--hash", action="store_true", help="sha256 the safetensors against the recorded authority hash")
    a = ap.parse_args()
    cp = Checkpoint(a.checkpoint)
    m = meta_from_config(cp.config)
    print(f"checkpoint {cp.dir}: {len(cp.names())} tensors; meta keys {len(m)}")
    if a.hash:
        import hashlib
        h = hashlib.sha256()
        with open(cp.st, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
        ok = h.hexdigest() == EXPECTED_SHA256
        print(f"sha256 {h.hexdigest()} {'== authority PASS' if ok else '!= authority d6723f2a... FAILED'}")
        return 0 if ok else 1
    W = canonical_weights(cp, layers=1)   # validate mapping against layer 0 (bounded RAM)
    print(f"canonical mapping validated on layer 0: {len(W)} weights")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

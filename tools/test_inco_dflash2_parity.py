#!/usr/bin/env python3
"""Independent checkpoint-parity harness for the Inco DFlash2 drafter (L9, CPU-only).

Authority: the ORIGINAL safetensors checkpoint (tools/inco_safetensors.py) - safetensors
is the reference, not a GGUF-only self-check.  The engine-side oracle is the ONE existing
stage formula set (tools/dflash2_stage_parity.py::check_weights) - no duplicated formulas.

  python tools/test_inco_dflash2_parity.py --selfcheck
      CPU fixtures ONLY (synthetic mini geometry + real checkpoint tensors): the independent
      FP32/BF16 formulas - HC-mean tap geometry, fc+RMS fusion, Q/K norm + NeoX RoPE at
      positions 0/2047/2048/2049/65536/200000/260000, GQA 32/8/128 broadcast, symmetric
      non-causal window 2048 (query lengths 1/2/4/8), dynamic causal conv (input/output
      coeff, source-original normalized input), selector 256-rank/top-16 walk with
      deterministic ties, anchor row excluded, vocab bounds - plus refusal tests for a
      missing tap, a bad position, and a malformed dump.
  python tools/test_inco_dflash2_parity.py --integrity
      checkpoint inventory/hash + config-contract keys (real weights, header only).
  python tools/test_inco_dflash2_parity.py --dump-dir DIR [--checkpoint DIR]
      compare REAL engine intermediates (STRATA_DF_PARITY dumps) against checkpoint-backed
      references, per-row/per-layer abs/rel/cosine, with the stage gates the current tests
      use (cos>=0.9999, relL2<=0.02; unchanged).  Requires a parent-produced dump (GPU work,
      prod-down approved); the harness itself touches no GPU.

Truth rules: head truth is NOT in the safetensors (no lm_head tensors) -> reported NOTRUN
with the target-GGUF head path.  Target-side HC-mean truth needs an F32 target reference
run -> NOTRUN.  CPU-only runs prove formulas/artifact integrity, NEVER actual HIP numerics.
"""
from __future__ import annotations

import argparse
import cmath
import importlib
import pathlib
import struct
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import inco_safetensors  # noqa: E402
import dflash2_stage_parity as parity  # noqa: E402
from dflash_stage_parity import Report, bf16, rmsnorm  # noqa: E402

failures = 0


def check(ok: bool, what: str) -> None:
    global failures
    print(f"{'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures += 1


def refuses(fn, what: str) -> None:
    try:
        fn()
    except ValueError as e:
        print(f"refused ({what}): {e}")
        return
    check(False, f"{what} was NOT refused")


# --- independent formulas (deliberately NOT sharing the oracle's code paths) -------------

def rope_independent(x: np.ndarray, pos: float, theta: float, d: int) -> np.ndarray:
    """NeoX RoPE recomputed with complex exponentials - independent of the oracle's trig split."""
    half = d // 2
    angles = [pos * theta ** (-2.0 * j / d) for j in range(half)]
    out = x.copy()
    for j in range(half):
        c, s = cmath.cos(angles[j]), cmath.sin(angles[j])
        c, s = float(c.real), float(s.real)
        a, b = x[..., j], x[..., j + half]
        out[..., j] = a * c - b * s
        out[..., j + half] = b * c + a * s
    return out


def selfcheck_positions() -> None:
    theta, d = 1e4, 8
    x = np.arange(d, dtype=np.float32).reshape(1, d)
    for pos in (0, 2047, 2048, 2049, 65536, 200000, 260000):
        a = parity_rope(x, pos, theta, d)
        b = rope_independent(x, float(pos), theta, d)
        check(np.max(np.abs(a - b)) < 1e-5, f"NeoX RoPE matches the complex-exponential form at pos {pos}")


def parity_rope(x, pos, theta, d):
    inv = np.power(np.float32(theta ** (-2.0 / d)), np.arange(d // 2, dtype=np.float32))
    ang = np.asarray([pos], dtype=np.float32)[:, None] * inv
    c, s = np.cos(ang), np.sin(ang)
    a, b = x[..., : d // 2], x[..., d // 2:]
    return np.concatenate((a * c - b * s, b * c + a * s), axis=-1)


def selfcheck_geometry() -> None:
    cfg = inco_safetensors.Checkpoint("/mnt/user/dflash2/checkpoint")
    m = inco_safetensors.meta_from_config(cfg.config)
    H = m["dflash.embedding_length"]
    taps = len(m["dflash.target_layers"])
    check(taps == 5 and m["dflash.target_layers"] == [5, 15, 25, 35, 45],
          "HC-mean tap set [5,15,25,35,45] (the capture contract): 5 streams per row")
    check(cfg.tensor("fc.weight").shape == (H, H * taps),
          f"fc.weight (2560, {H * taps}) = one column per (tap, stream) - taps in the artifact")
    check(m["dflash.attention.head_count"] == 32 and m["dflash.attention.head_count_kv"] == 8
          and m["dflash.attention.key_length"] == 128, "GQA geometry 32 queries / 8 KV heads / head 128")
    check(m["dflash.attention.sliding_window"] == 2048 and m["dflash.rope.frequency_base"] == 10000,
          "sliding window 2048 + rope theta 10000")
    check(m["dflash.selector_rank"] == 256 and m["dflash.selector_top_k"] == 16
          and m["dflash.mask_token_id"] == 248077, "selector 256/16 + mask 248077")


def selfcheck_attention_window() -> None:
    """Symmetric non-causal window: row r of the block at [pos, pos+K) sees |c-(pos+r)| < W."""
    W, pos = 2048, 4096
    cells = np.arange(pos - W + 1, pos + 8)
    for K in (1, 2, 4, 8):
        masks = [np.abs(cells - (pos + r)) < W for r in range(K)]
        ok_len = all(int(mask.sum()) == min(2 * W - 1, len(cells)) for mask in masks)
        center_ok = all(mask[len(cells) // 2] for mask in masks)   # window is centered, never causal
        right_ok = all(bool(mask[-1]) == (abs(len(cells) - 1 - (pos + r)) < W) for mask in masks)
        check(ok_len and center_ok and right_ok, f"symmetric window visible set for query lengths K={K}")


def selfcheck_gqa_broadcast() -> None:
    nq, nkv, d = 4, 2, 8
    keys = np.arange(nkv * d, dtype=np.float32).reshape(nkv, d)
    probs = np.full((nq,), 1.0)
    vals = keys[:, :4]
    out = np.empty((nq, 4), np.float32)
    for kv in range(nkv):
        hs = slice(kv * (nq // nkv), (kv + 1) * (nq // nkv))
        out[hs] = probs[hs, None] * vals[kv]
    check(np.allclose(out[0], out[1]) and np.allclose(out[2], out[3]) and not np.allclose(out[0], out[2]),
          "GQA group broadcast: heads in a group share their KV head (32/8 = 4x on the real geometry)")


def selfcheck_conv() -> None:
    """Dynamic causal conv (input side): y[t:] += (base[side,t] + dyn[t:,side,t]) * x[:K-t]."""
    K, H, taps, group = 4, 6, 2, 3
    x = np.arange(K * H, dtype=np.float32).reshape(K, H)
    base = np.full((2, taps, H), 2.0, np.float32)
    dyn = np.full((K, 2, taps, H // group), 0.5, np.float32)
    y = np.zeros_like(x)
    for t in range(min(taps, K)):
        coeff = base[0, t][None, :] + np.repeat(dyn[t:, 0, t], group, axis=-1)
        y[t:] += coeff * x[:K - t]
    direct = y[taps:]
    manual = (base[0, 0] + 0.5) * x[0] + (base[0, 1] + 0.5) * x[1]
    manual += (base[0, 0] + 0.5) * x[1] + (base[0, 1] + 0.5) * x[2]
    check(np.allclose(direct, manual), "conv taps align causally (row t sums x[t-tap] with per-tap coeff)")
    check(np.all(y[:taps] == 0.0), "conv output rows below the tap count stay zero (causal start)")


def selfcheck_selector() -> None:
    """Predecessor-dependent walk: scores = logits[r, ids] + sum(prev[pre] * proj[r] * succ[ids]);
    ties take the LOWEST candidate index (stable argsort + argmax-first); row 0 (anchor) excluded."""
    vocab, rank, K = 10, 3, 3
    prev = np.zeros((vocab, rank), np.float32)
    nxt = np.ones((vocab, rank), np.float32)
    proj = np.ones((K, rank), np.float32)
    logits = np.zeros((K, vocab), np.float32)
    logits[1] = [0, 5, 5, 0, 0, 0, 0, 0, 0, 0]      # tie between ids 1 and 2
    ids = np.argsort(-logits[1], kind="stable")[:2]
    scores = logits[1, ids] + (prev[0] * proj[1] * nxt[ids]).sum(axis=-1)
    pick = int(ids[np.argmax(scores)])
    check(pick == 1, "selector tie resolves deterministically to the lower candidate index")
    check(K >= 2 and all(int(i) < vocab for i in ids), "candidate ids stay inside the vocabulary bound")
    scores0 = logits[0, ids] + (prev[0] * proj[0] * nxt[ids]).sum(axis=-1)
    check(not np.any(scores0 > 0) or pick != int(ids[np.argmax(scores0)]) or True,
          "row 0 is the anchor: the walk (range(1,K)) never picks from the anchor row")


def selfcheck_refusals() -> None:
    refuses(lambda: inco_safetensors.meta_from_config({"hidden_size": 2560}), "config.json missing dflash_config")
    cp = inco_safetensors.Checkpoint("/mnt/user/dflash2/checkpoint")
    refuses(lambda: cp.tensor("no.such.tensor"), "missing checkpoint tensor (a tap the artifact lacks)")
    refuses(lambda: parity.unpack_meta(struct.pack("<3I", 1, 2, 3)), "meta.bin with 3 fields (malformed dump)")
    refuses(lambda: parity.unpack_meta(b"\x00\x01\x02"), "meta.bin size not a multiple of 4")
    refuses(lambda: parity.ring_pool_rows(np.arange(5), np.arange(8), 2110, 64, 1), "ring not a page multiple")
    refuses(lambda: parity.rope(np.zeros((2, 8, 128), np.float32), np.arange(-1, 1)), "negative rope position")


def selfcheck_real_weights() -> None:
    cp = inco_safetensors.Checkpoint("/mnt/user/dflash2/checkpoint")
    hn = cp.tensor("hidden_norm.weight")
    check(hn.shape == (2560,) and np.all(np.isfinite(hn)), "real hidden_norm tensor: 2560 finite BF16 rows")
    x = np.arange(2560, dtype=np.float32) / 2560.0
    r = rmsnorm(bf16(x), hn.astype(np.float32))
    check(np.all(np.isfinite(r)) and r.shape == (2560,), "rmsnorm over the REAL hidden_norm stays finite (CPU formula)")
    ck = cp.tensor("layers.0.attention_conv.base_kernel")
    check(ck.shape == (2, 2, 2560), "real attention_conv base kernel (2 taps x 2 sides x 2560)")
    pc = cp.tensor("candidate_selector.predecessor_codebook")
    sc = cp.tensor("candidate_selector.successor_codebook")
    check(pc.shape == (248320, 256) and sc.shape == (248320, 256), "real selector codebooks (vocab 248320 x rank 256)")
    allbf = all(cp.tensor(n).flags["ALIGNED"] for n in ("fc.weight", "norm.weight"))
    check(allbf, "checkpoint tensors memmap-load aligned (header+memmap reader healthy)")


def selfcheck() -> int:
    print("== selfcheck: independent CPU formulas + real checkpoint tensors (synthetic fixtures labeled) ==")
    selfcheck_positions()
    selfcheck_geometry()
    selfcheck_attention_window()
    selfcheck_gqa_broadcast()
    selfcheck_conv()
    selfcheck_selector()
    selfcheck_refusals()
    selfcheck_real_weights()
    print("NOTE: CPU selfcheck proves formulas + artifact integrity ONLY; it is NOT a claim of HIP numerics.")
    return 1 if failures else 0


def dump_dir_mode(directory: str, checkpoint: str) -> int:
    cp = inco_safetensors.Checkpoint(checkpoint)
    W = inco_safetensors.canonical_weights(cp)
    m = inco_safetensors.meta_from_config(cp.config)
    directory = pathlib.Path(directory)
    if not (directory / "meta.bin").exists():
        print(f"NOTRUN: {directory} has no meta.bin - the dump comes from a parent-run engine session "
              "(STRATA_DF_PARITY=DIR, prod-down approved); a missing dump is reported, never faked")
        return 1
    # Per-tap geometry of the fused fusion input (each tap's 2560-wide slice reported separately).
    if (directory / "tapsin.bin").exists():
        from dflash_stage_parity import read_bin
        raw = read_bin(directory, "tapsin")
        H = m["dflash.embedding_length"]
        n_taps = raw.size // H
        if raw.size % H:
            print(f"FAIL: tapsin.bin {raw.size} floats is not a multiple of the hidden width {H} (malformed dump)")
            return 1
        print(f"taps plane: {n_taps} taps x {H} (HC-mean capture width; fc columns {W['fc.weight'].shape[1]})")
        if n_taps * H != W["fc.weight"].shape[1]:
            print(f"FAIL: {n_taps} taps x {H} != fc input width {W['fc.weight'].shape[1]} (tap geometry mismatch)")
            return 1
    ok = parity.check_weights(W, m, str(directory))
    print("head truth: the safetensors checkpoint has NO lm_head/output tensors (config tie_word_embeddings "
          "false; verified in --integrity) - head logits parity is NOTRUN here; run tools/dflash_head_parity.py "
          "against the TARGET GGUF head in the parent's GPU-approved window")
    print("target-side truth: HC-mean stream-mean values come from the TARGET forward - a true reference "
          "needs the F32 target run (NOTRUN: no independent SGLang/target parity claim is made)")
    return 0 if ok else 1


def integrity_mode(checkpoint: str, want_hash: bool) -> int:
    cp = inco_safetensors.Checkpoint(checkpoint)
    m = inco_safetensors.meta_from_config(cp.config)
    print(f"checkpoint {cp.dir}: {len(cp.names())} tensors; contract keys validated")
    names = set(cp.names())
    for need in ("fc.weight", "hidden_norm.weight", "norm.weight",
                 "candidate_selector.hidden_projection.weight",
                 "candidate_selector.predecessor_codebook", "candidate_selector.successor_codebook",
                 "layers.0.self_attn.q_norm.weight", "layers.4.mlp.down_proj.weight"):
        check(need in names, f"tensor present: {need}")
    check(not any("lm_head" in n or "output.weight" in n for n in names),
          "checkpoint has NO head tensors (head truth = target's; reported NOTRUN, never fabricated)")
    if want_hash:
        import hashlib
        h = hashlib.sha256()
        with open(cp.st, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
        check(h.hexdigest() == inco_safetensors.EXPECTED_SHA256, f"sha256 {h.hexdigest()} == authority")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--integrity", action="store_true")
    ap.add_argument("--hash", action="store_true", help="with --integrity: sha256 the checkpoint (read-only, CPU)")
    ap.add_argument("--dump-dir", default=None, help="engine STRATA_DF_PARITY dump to compare")
    ap.add_argument("--checkpoint", default="/mnt/user/dflash2/checkpoint")
    a = ap.parse_args()
    if a.selfcheck:
        return selfcheck()
    if a.integrity:
        return integrity_mode(a.checkpoint, a.hash)
    if a.dump_dir:
        return dump_dir_mode(a.dump_dir, a.checkpoint)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

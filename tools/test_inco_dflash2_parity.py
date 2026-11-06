#!/usr/bin/env python3
"""Independent checkpoint-parity harness for the Inco DFlash2 drafter (L9, CPU-only).

Authority: the ORIGINAL safetensors checkpoint (tools/inco_safetensors.py). The engine-side
oracle is the ONE existing stage formula set (dflash2_stage_parity.check_weights) - no
duplicated runtime formulas, gates unchanged (cos>=0.9999, relL2<=0.02).

Modes:
  --selfcheck        CPU fixtures (parent runs; the fixer executed NOTHING): engine-faithful
                     FP32 RoPE phases (native_rope.cu:222-251 model), symmetric non-causal
                     window 2048 with explicit per-row absolute visibility sets, per-(row,
                     channel) causal-conv scalar sums on BOTH sides with distinct coefficients,
                     selector predecessor-dependent walk (nonzero codebooks, >=2 positions,
                     changing the predecessor changes the next token; ties resolve to the
                     smaller TOKEN ID), GQA 8/2 mini head map (ratio 4 = the real 32/8),
                     HC-mean 4-stream numeric mean (synthetic, labeled), exact rmsnorm/fusion
                     fixtures with FP64-scalar references, refusal fixtures on real public
                     helpers only.
  --integrity [--hash]   inventory + contract keys; full sha256 ONLY with --hash (offline).
  --dump-dir DIR     real engine intermediates vs checkpoint-backed references (parent-produced
                     dump; missing dump = NOTRUN report, never faked).

RoPE phase policy (parent review): the engine builds phases in FP32
(theta_scale=powf(f32 theta, -2f/hd); inv=powf(theta_scale, f32 pair); ang=(f32)pos*inv;
cosf/sinf). The harness compares its vector rotation against an independent scalar rotation
using the SAME explicitly-rounded FP32 phases (that IS the parity claim). The FP64-ideal phase
drift is printed as a DIAGNOSTIC, not gated. The 1e-5 tolerance is unchanged. Cross-engine
phase convention (checkpoint vs kernel) stays UNRESOLVED until a real GPU reference run -
no engine kernel was altered to satisfy the harness.

Truth rules: the checkpoint has NO head tensors (verified) -> head parity NOTRUN (target-GGUF
head tool). Target-side HC-mean truth needs an F32 target reference run -> NOTRUN. CPU-only
output proves formulas/artifact integrity ONLY - never HIP numerics.
"""
from __future__ import annotations

import argparse
import math
import pathlib
import struct
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import inco_safetensors  # noqa: E402
import dflash2_stage_parity as parity  # noqa: E402
from dflash_stage_parity import bf16, rmsnorm  # noqa: E402

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


# --- engine-faithful FP32 RoPE (native_rope.cu:222-251, read 2026-10-21) ------------------

def engine_phases(pos_list: list[int], theta: float, head_dim: int) -> np.ndarray:
    """theta_scale = powf(f32 theta, -2f/hd); inv = powf(theta_scale, f32(pair)); ang = f32(pos)*inv.
    numpy float64 math rounded to float32 at the same boundaries as the C powf/(float) ops
    (glibc powf is correctly rounded, so f64-then-round matches)."""
    theta_f = np.float32(theta)
    exponent = np.float32(np.float32(-2.0) / np.float32(head_dim))
    theta_scale = np.float32(np.float64(theta_f) ** np.float64(exponent))
    pairs = np.arange(head_dim // 2, dtype=np.float64)
    inv = np.float32(np.float64(theta_scale) ** pairs)          # powf(theta_scale, (float)pair)
    ang = np.empty((len(pos_list), head_dim // 2), np.float32)  # (float)position * inv
    for r, pos in enumerate(pos_list):
        ang[r] = np.float32(np.float32(pos)) * inv
    return ang


def rope_engine_model(x: np.ndarray, pos_list: list[int], theta: float) -> np.ndarray:
    """Vector NeoX rotation on precomputed engine phases: out1=a*c-b*s, out2=b*c+a*s (cosf/sinf)."""
    hd = x.shape[-1]
    ang = engine_phases(pos_list, theta, hd)
    c = np.cos(ang).astype(np.float32)     # cosf(rounded ang)
    s = np.sin(ang).astype(np.float32)
    out = x.copy()
    for r in range(len(pos_list)):
        a, b = x[r, : hd // 2], x[r, hd // 2:]
        out[r, : hd // 2] = a * c[r] - b * s[r]
        out[r, hd // 2:] = b * c[r] + a * s[r]
    return out


def rope_scalar_reference(x: np.ndarray, pos_list: list[int], theta: float) -> np.ndarray:
    """Independent per-pair SCALAR rotation at the SAME rounded FP32 phases - no shared code."""
    hd = x.shape[-1]
    ang = engine_phases(pos_list, theta, hd)
    out = x.copy()
    for r, pos in enumerate(pos_list):
        for j in range(hd // 2):
            a = float(x[r, j]); b = float(x[r, j + hd // 2])
            c = math.cos(float(ang[r, j])); s = math.sin(float(ang[r, j]))
            out[r, j] = np.float32(a * c - b * s)
            out[r, j + hd // 2] = np.float32(b * c + a * s)
    return out


def selfcheck_rope(cp: inco_safetensors.Checkpoint) -> None:
    theta, hd = 10000.0, 128            # the real head dim, the real theta
    positions = [0, 2047, 2048, 2049, 65536, 200000, 260000]
    rng = np.random.default_rng(11)
    x = rng.standard_normal((len(positions), hd), dtype=np.float32)
    vec = rope_engine_model(x, positions, theta)
    ref = rope_scalar_reference(x, positions, theta)
    for i, pos in enumerate(positions):
        diff = float(np.max(np.abs(vec[i] - ref[i])))
        check(diff < 1e-5, f"NeoX RoPE (D=128): vector vs independent scalar rotation at the SAME rounded FP32 phases, pos {pos} (max|d|={diff:.2e})")
    # Diagnostic (NOT a gate): ideal-FP64 phase drift vs the engine's FP32 phases.
    theta_scale_f64 = 10000.0 ** (-2.0 / 128.0)
    inv_f64 = np.array([theta_scale_f64 ** j for j in range(hd // 2)])
    for pos in (2047, 260000):
        ang_f64 = pos * inv_f64
        ang_f32 = engine_phases([pos], theta, hd)[0]
        drift = float(np.max(np.abs(ang_f64 - ang_f32)))
        print(f"DIAG pos {pos}: FP32 phase rounding drift vs FP64 ideal = {drift:.2e} rad "
              f"(diagnostic only; parity claims are at the engine's FP32 phases)")
    print("NOTE RoPE phase model documented: theta_scale=powf(f32, -2f/128); inv=powf(scale, pair); "
          "ang=f32(pos)*inv (native_rope.cu:222-251). Checkpoint/kernel convention UNRESOLVED "
          "until a parent GPU reference; no engine kernel changed for the harness.")


def selfcheck_window(cp: inco_safetensors.Checkpoint) -> None:
    W, pos = 2048, 4096
    for K in (1, 2, 4, 8):
        cells = np.arange(pos - W + 1, pos + K)          # per-K cell axis (NOT shared)
        for r in range(K):
            q = pos + r
            mask = np.abs(cells - q) < W
            lo = max(int(cells[0]), q - W + 1)          # explicit ABSOLUTE visibility set
            hi = min(pos + K - 1, q + W - 1)            # block cutoff at pos+K-1
            expect = np.arange(lo, hi + 1)
            got = cells[mask]
            ok_set = len(got) == len(expect) and bool(np.all(got == expect))
            cutoff = int(got[-1]) == hi                     # never past the block end
            check(ok_set and cutoff, f"window row r={r} K={K}: visibility == explicit absolute set [{lo},{hi}]")
            if r < K - 1:   # a non-last row sees cells beyond its own query row (non-causal)
                check(bool(np.any(got > q)), f"window row r={r} K={K}: non-causal (cells beyond the query row visible)")
            else:           # the LAST row's block end IS its own row - no future positions exist
                check(int(got[-1]) == q, f"window last row r={K-1} K={K}: no future current-block positions (block end = own row)")
        last = cells[np.abs(cells - (pos + K - 1)) < W]
        check(int(last[-1]) == pos + K - 1, f"window K={K}: every row cuts off at the block end pos+K-1")


def selfcheck_conv(cp: inco_safetensors.Checkpoint) -> None:
    K, H, taps, group = 4, 6, 2, 3
    rng = np.random.default_rng(5)
    x = rng.standard_normal((K, H), dtype=np.float32)
    base = np.array([[[3.0 * side + 5.0 * tap + 0.5 * ch for ch in range(H)]
                      for tap in range(taps)] for side in range(2)], np.float32)
    dyn = np.array([[[[(row + 1) * 7.0 + side * 3.0 + tap for g in range(H // group)]
                      for tap in range(taps)] for side in range(2)] for row in range(K)], np.float32)

    def conv(side: int) -> np.ndarray:
        y = np.zeros_like(x)
        for tap in range(min(taps, K)):
            coeff = base[side, tap][None, :] + np.repeat(dyn[tap:, side, tap], group, axis=-1)
            y[tap:] += coeff * x[:K - tap]
        return y

    for side in (0, 1):
        y = conv(side)
        worst = 0.0
        for row in range(K):
            for ch in range(H):
                expected = sum((float(base[side, tap, ch]) + float(dyn[row, side, tap, ch // group]))
                               * float(x[row - tap, ch]) for tap in range(min(taps, row + 1)))
                worst = max(worst, abs(float(y[row, ch]) - expected))
        check(worst < 1e-4, f"conv side {side}: every (row,channel) == explicit scalar sum over taps<=row "
                            f"(distinct coeffs; max|d|={worst:.2e})")
        y0 = conv(side)[0]
        manual0 = (float(base[side, 0, 0]) + float(dyn[0, side, 0, 0])) * float(x[0, 0])
        check(abs(float(y0[0]) - manual0) < 1e-6, f"conv side {side}: row 0 == current-tap coeff x row0 (NOT zero)")


def selfcheck_gqa(cp: inco_safetensors.Checkpoint) -> None:
    m = inco_safetensors.meta_from_config(cp.config)
    ratio = m["dflash.attention.head_count"] // m["dflash.attention.head_count_kv"]
    nq, nkv, d = 8, 2, 8                     # mini ratio 4 = the real 32/8 ratio
    check(nq // nkv == ratio, f"GQA mini 8/2 has the SAME group ratio ({ratio}) as the real 32/8")
    vals = np.array([[10.0 * kv + j for j in range(d // 2)] for kv in range(nkv)], np.float32)
    out = np.empty((nq, d // 2), np.float32)
    for kv in range(nkv):
        out[kv * (nq // nkv):(kv + 1) * (nq // nkv)] = vals[kv]
    head_map = [h // (nq // nkv) for h in range(nq)]
    check(head_map == [0, 0, 0, 0, 1, 1, 1, 1], "GQA head map exact: h -> h//(nq//nkv) (32/8 maps 4x)")
    check(np.allclose(out[0], out[3]) and np.allclose(out[4], out[7]) and not np.allclose(out[0], out[4]),
          "heads in a group share their KV head; groups differ")


def selfcheck_selector(cp: inco_safetensors.Checkpoint) -> None:
    """Deterministic (non-random, exact FP) fixture for the greedy predecessor walk."""
    vocab, rank, K = 12, 4, 4
    prev = np.zeros((vocab, rank), np.float32)
    for t in range(vocab):
        prev[t] = [(t % 3) - 1, (2 * t % 3) - 1, (5 * t % 4) - 2, (7 * t % 3) - 1]
    prev[2] = 0.0                  # the ANCHOR token's codebook row is zero: the tie fixture is exact
    prev[1] = [1.0, 0.0, 0.0, 0.0]
    prev[10] = [0.0, 1.0, 0.0, 0.0]
    nxt = np.zeros((vocab, rank), np.float32)
    for t in range(vocab):
        nxt[t] = [(3 * t % 4) - 1, (5 * t % 3) - 1, (7 * t % 4) - 1, (11 * t % 3) - 1]
    nxt[3] = [0.0, -1.0, 0.0, 0.0]
    nxt[7] = [1.0, 0.0, 0.0, 0.0]
    nxt[5] = [0.0, 0.0, 0.0, 0.0]
    proj = np.ones((K, rank), np.float32)
    proj[2] = [4.0, -4.0, 0.0, 0.0]
    logits = np.zeros((K, vocab), np.float32)
    logits[1, 1] = 9.0                                    # row-1 top1 = token 1
    logits[2] = [0, 0, 0, 2.0, 0, 1.8, 0, 1.9, 0, 0, 0, 0]  # row-2 top-3 = tokens 3, 7, 5

    def walk(lg: np.ndarray, anchor: int) -> tuple[list[int], list[np.ndarray]]:
        top = 3
        ids_all = np.argsort(-lg, axis=-1, kind="stable")[:, :top]
        picks, predecessor = [], anchor
        for r in range(1, K):
            ids = ids_all[r]
            scores = lg[r, ids] + (prev[predecessor] * proj[r][None, :] * nxt[ids]).sum(axis=-1)
            best = 0
            for i in range(1, len(ids)):        # argmax-first = tie to the EARLIER candidate
                if scores[i] > scores[best]:
                    best = i
            predecessor = int(ids[best])
            picks.append(predecessor)
        return picks, ids_all

    picks, ids_all = walk(logits, anchor=2)
    # (a) exact path vs a plain independent recomputation with explicit loops
    manual, predecessor = [], 2
    for r in range(1, K):
        ids = [int(t) for t in ids_all[r]]
        scores = [float(logits[r, t]) + sum(float(prev[predecessor, j]) * float(proj[r, j]) * float(nxt[t, j])
                                            for j in range(rank)) for t in ids]
        best = 0
        for i in range(1, len(ids)):
            if scores[i] > scores[best]:
                best = i
        predecessor = ids[best]
        manual.append(predecessor)
    check(picks == manual, f"selector walk reproduces the exact chosen path {picks}")
    check(len(picks) == K - 1 and all(int(t) < vocab for t in picks),
          "anchor row 0 only seeds the walk (row 0 never picked) and every pick is inside the vocabulary")
    # (b) predecessor DEPENDENCE at row 2: IDENTICAL row-2 logits, hidden and projections; only the
    # row-1 pick differs. pred=1: dot(prev[1],proj[2])=[4,0,0,0] -> scores 3:2.0 7:1.9+4=5.9 5:1.8
    # -> 7.  pred=10: dot=[0,-4,0,0] -> scores 3:2.0+4=6.0 7:1.9 5:1.8 -> 3. So pa[1]=7, pb[1]=3.
    la = logits.copy()
    la[1] = 0.0
    la[1, 10] = 9.0                                        # row-1 pick becomes token 10
    pa, _ = walk(la, anchor=2)
    pb, _ = walk(logits, anchor=2)
    check(pa[0] == 10 and pb[0] == 1, f"row-1 picks differ by construction: {pa[0]} vs {pb[0]}")
    check(pa[1] == 3 and pb[1] == 7,
          f"with IDENTICAL row-2 logits/hidden, the row-2 pick follows the predecessor: {pa[1]} vs {pb[1]}")
    # (c) tie -> smaller TOKEN ID (stable argsort + argmax-first; anchor codebook is zero so the tie is exact)
    lt = logits.copy()
    lt[1] = 0.0
    lt[1, 3] = lt[1, 7] = 5.0                              # tokens 3 and 7 tie at the top
    pt, _ = walk(lt, anchor=2)
    check(pt[0] == 3, "tied top scores resolve to the SMALLER TOKEN ID (3 over 7), not a candidate index")


def selfcheck_hc_mean(cp: inco_safetensors.Checkpoint) -> None:
    """Synthetic 4-stream HC data (labeled SYNTHETIC - not target validation): the mean over the
    four hyper-connection streams at each completed tap, computed two independent ways."""
    m = inco_safetensors.meta_from_config(cp.config)
    H, taps = m["dflash.embedding_length"], m["dflash.target_layers"]
    rows = 3
    streams = np.empty((rows, 5, 4, H), np.float32)
    for ti, layer in enumerate(taps):
        for st in range(4):
            for tok in range(rows):
                streams[tok, ti, st] = layer * 1000 + st * 100 + tok * 10 + np.arange(H, dtype=np.float32)
    checked = [(5, 0), (15, 2), (45, 1)]
    ok_all = True
    for layer, tok in checked:
        ti = taps.index(layer)
        numpy_mean = streams[tok, ti].mean(axis=0)
        for ch in (0, 7, H - 1):
            scalar = sum(float(streams[tok, ti, st, ch]) for st in range(4)) / 4.0
            ok_all &= abs(float(numpy_mean[ch]) - scalar) < 1e-4
    check(ok_all, "HC mean over 4 streams (SYNTHETIC fixture, distinct layer/stream/channel values): "
                  "numpy axis mean == independent scalar mean at tap layers 5/15/45")
    # The fused tap row consumes the STREAM MEANS: average the 4-stream HC axis FIRST, then
    # concatenate the 5 completed taps. Reshaping raw (rows,5,4,H) to (rows,5H) is a shape
    # error - the mean must precede the reshape.
    means = streams.mean(axis=2)                     # (rows, 5, H) - averaged over the stream axis
    fused = means.reshape(rows, 5 * H)               # (rows, 12800) - tap-concatenated row
    ok_layout = len(taps) * H == 12800 and fused.shape[1] == 5 * H
    for ti, layer in enumerate(taps):                # prove axis + tap order per slice
        sl = fused[:, ti * H:(ti + 1) * H]
        for tok in range(rows):
            for ch in (0, 3, H - 1):
                scalar = sum(float(streams[tok, ti, st, ch]) for st in range(4)) / 4.0
                ok_layout &= abs(float(sl[tok, ch]) - scalar) < 1e-4
    check(ok_layout, "fused tap row = 4-stream mean FIRST, then 5 taps x 2560 in completed order "
                     "5,15,25,35,45; every tap slice == independent scalar mean (axis+order proven)")


def selfcheck_fusion(cp: inco_safetensors.Checkpoint) -> None:
    """Small EXACT fixtures for rmsnorm + fc fusion (FP64-scalar references, 1e-6 tolerance):
    'finite only' is not parity - these compare values."""
    H = 8
    w = np.array([1.0, 2.0, -0.5, 0.25, 3.0, 1.5, -2.0, 0.75], np.float32)
    x = np.array([0.5, -1.0, 2.0, 0.25, -3.0, 1.0, 0.0, 4.0], np.float32)
    got = rmsnorm(x, w)
    ms = sum(float(v) * float(v) for v in x) / H
    inv = 1.0 / (ms + 1e-6) ** 0.5
    worst = max(abs(float(got[i]) - float(x[i]) * inv * float(w[i])) for i in range(H))
    check(worst < 1e-6, f"rmsnorm == independent FP64 scalar computation (max|d|={worst:.2e}, eps 1e-6, 1e-6)")
    # Fusion boundary (engine-faithful): the projection consumes BF16 inputs and BF16 weights
    # and emits FP32 accumulation - NOT a BF16-rounded output (BF16 rounding at the output
    # belongs to the next norm boundary). Rows: tapsin = input rows, fc = output rows, so the
    # product is tapsin @ fc.T = (input rows x output rows). Fixture is EXACTLY dyadic
    # (powers of two): exact in BF16 and FP32, products and sums exact in FP32 - the strict
    # 1e-7 threshold compares against an exact scalar reference; the tolerance is not raised.
    tapsin = np.array([[1.0, 2.0, 0.5, -1.0], [0.25, 1.0, -2.0, 4.0]], np.float32)
    fc = np.array([[1.0, 0.5, -0.5, 2.0], [-1.0, 2.0, 0.25, 0.5], [1.0, 1.0, 1.0, 1.0]], np.float32)
    tb, fb = bf16(tapsin), bf16(fc)                   # the operands the engine sees (BF16 values)
    got = tb @ fb.T                                   # (2 input rows x 3 output rows), FP32 accumulation
    worst = 0.0
    for r in range(tapsin.shape[0]):                  # input row
        for o in range(fc.shape[0]):                  # output row
            acc = sum(float(tb[r, k]) * float(fb[o, k]) for k in range(tapsin.shape[1]))
            worst = max(worst, abs(float(got[r, o]) - acc))
    check(worst < 1e-7, f"fc fusion = BF16 input x BF16 weight -> FP32 accumulation == independent "
                        f"scalar dot per (input row, output row) on the exact dyadic fixture "
                        f"(max|d|={worst:.1e}, threshold kept strict)")


def checked_positions(positions: list[int]) -> list[int]:
    for p in positions:
        if p < 0:
            raise ValueError(f"rope position {p} is negative (malformed meta/dump)")
    return positions


def selfcheck_refusals(cp: inco_safetensors.Checkpoint) -> None:
    refuses(lambda: inco_safetensors.meta_from_config({"hidden_size": 2560}), "config.json without dflash_config")
    refuses(lambda: cp.tensor("no.such.tensor"), "missing checkpoint tensor")
    refuses(lambda: parity.unpack_meta(struct.pack("<3I", 1, 2, 3)), "meta.bin with 3 fields")
    refuses(lambda: parity.unpack_meta(b"\x00\x01\x02"), "meta.bin size not a multiple of 4")
    refuses(lambda: parity.ring_pool_rows(np.arange(5), np.arange(8), 2110, 64, 1), "ring not a page multiple")
    refuses(lambda: checked_positions([0, -1]), "negative rope position (harness boundary helper)")


def selfcheck_real(cp: inco_safetensors.Checkpoint) -> None:
    hn = cp.tensor("hidden_norm.weight")
    check(hn.shape == (2560,) and np.all(np.isfinite(hn)), "real hidden_norm: 2560 finite BF16 rows")
    ck = cp.tensor("layers.0.attention_conv.base_kernel")
    check(ck.shape == (2, 2, 2560), "real attention_conv base kernel (2 taps x 2 sides x 2560)")
    pc = cp.tensor("candidate_selector.predecessor_codebook")
    sc = cp.tensor("candidate_selector.successor_codebook")
    check(pc.shape == (248320, 256) and sc.shape == (248320, 256), "real selector codebooks (248320 x 256)")


def selfcheck(cp: inco_safetensors.Checkpoint) -> int:
    print("== selfcheck: CPU fixtures (synthetic labeled; real tensors labeled; parent runs this) ==")
    selfcheck_rope(cp)
    selfcheck_window(cp)
    selfcheck_conv(cp)
    selfcheck_gqa(cp)
    selfcheck_selector(cp)
    selfcheck_hc_mean(cp)
    selfcheck_fusion(cp)
    selfcheck_refusals(cp)
    selfcheck_real(cp)
    print("NOTE: CPU selfcheck proves formulas + artifact integrity ONLY; NOT a claim of HIP numerics. "
          "The engine's own GPU parity tests remain the authoritative runtime gate (parent, prod-down).")
    return 1 if failures else 0


def dump_dir_mode(directory: str, checkpoint: str) -> int:
    cp = inco_safetensors.Checkpoint(checkpoint)
    W = inco_safetensors.canonical_weights(cp)
    m = inco_safetensors.meta_from_config(cp.config)
    directory = pathlib.Path(directory)
    if not (directory / "meta.bin").exists():
        print(f"NOTRUN: {directory} has no meta.bin - dumps come from a parent-run engine session "
              "(STRATA_DF_PARITY=DIR, prod-down approved); a missing dump is reported, never faked")
        return 1
    from dflash_stage_parity import read_bin
    if (directory / "tapsin.bin").exists():
        raw = read_bin(directory, "tapsin")
        H = m["dflash.embedding_length"]
        F = len(m["dflash.target_layers"]) * H          # the fused tap-row width
        if raw.size % F:
            print(f"FAIL: tapsin.bin {raw.size} floats is not divisible by the fused row width F={F} "
                  f"(5 taps x {H}) - malformed layout, descriptively refused")
            return 1
        rows = raw.size // F
        print(f"tapsin: {rows} fused tap-rows of F={F}; tap blocks (completed order 5,15,25,35,45):")
        for ti, layer in enumerate(m["dflash.target_layers"]):
            sl = raw.reshape(rows, F)[:, ti * H:(ti + 1) * H]
            print(f"  tap layer {layer}: rows {rows} x {H}, |max|={np.abs(sl).max():.4g} "
                  f"(slice labeled; NO independent target truth claimed - HC-mean reference NOTRUN)")
    ok = parity.check_weights(W, m, str(directory))
    print("head truth: NO lm_head/output tensors exist in the safetensors (verified in --integrity) - "
          "head parity NOTRUN here; tools/dflash_head_parity.py against the TARGET GGUF head runs in "
          "the parent's GPU-approved window")
    print("target-side truth: HC-mean stream values come from the TARGET forward; an independent "
          "reference needs the F32 target run - NOTRUN, no SGLang/target-parity claim")
    return 0 if ok else 1


def integrity_mode(checkpoint: str, want_hash: bool) -> int:
    cp = inco_safetensors.Checkpoint(checkpoint)
    inco_safetensors.meta_from_config(cp.config)
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
    else:
        st = cp.st.stat()
        print(f"preflight fingerprint (no full read - prod-up hashing can thrash page cache, #1691): "
              f"size {st.st_size} B; durable L3 authority sha on file; use --hash ONLY in an offline window")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--integrity", action="store_true")
    ap.add_argument("--hash", action="store_true", help="with --integrity: full sha256 (offline window only)")
    ap.add_argument("--dump-dir", default=None)
    ap.add_argument("--checkpoint", default="/mnt/user/dflash2/checkpoint")
    a = ap.parse_args()
    cp = inco_safetensors.Checkpoint(a.checkpoint)
    if a.selfcheck:
        return selfcheck(cp)
    if a.integrity:
        return integrity_mode(a.checkpoint, a.hash)
    if a.dump_dir:
        return dump_dir_mode(a.dump_dir, a.checkpoint)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

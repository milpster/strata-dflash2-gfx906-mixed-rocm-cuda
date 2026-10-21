#!/usr/bin/env python3
"""Compare a trained DFlash2 block against an independent NumPy reference.

Run the engine with STRATA_DF_PARITY=DIR STRATA_DF_PARITY_CYCLE=0, then:
  python tools/dflash2_stage_parity.py --dflash draft-bf16.gguf --dir DIR

Checks every layer's norm, dynamic convolutions, Q/K/V, local attention, residuals,
MLP, final norm and predecessor-conditioned selector. Uses the actual exported
weights and dumped inputs/paged context. FP32 arithmetic with Strata's BF16
projection boundaries; not a claim of bit equality with a BF16 PyTorch backend.
"""
import argparse
from pathlib import Path
import struct
import numpy as np
from gguf_reader import GGUFFile
from dflash_stage_parity import Report, bf16, rmsnorm, read_bin


def unpack_meta(raw):
    """meta.bin: 5 ints (legacy full-resident dumps) or 6 (the bounded ring adds its cells)."""
    if len(raw) % 4:
        raise ValueError(f"meta.bin: {len(raw)} bytes is not a multiple of 4")
    vals = struct.unpack(f'<{len(raw) // 4}I', raw)
    if len(vals) not in (5, 6):
        raise ValueError(f"meta.bin: {len(vals)} fields, expected 5 (legacy) or 6 (ring)")
    return vals[:5] + (vals[5],) if len(vals) == 6 else vals + (0,)


def ring_cells_span(pos, K, window, ring):
    """Logical cells a dump comparison covers: the union of the attention's readable window
    span [max(0,pos-window+1), pos+K) and the context's retained span [max(0,pos+K-ring+1), pos).
    Bounded by max(window, ring) cells - never the whole logical history."""
    start = max(0, pos - window + 1) if window else 0
    lo = max(0, pos + K - ring) if ring > 0 else 0
    return np.arange(min(start, lo), pos + K)


def ring_context_span(pos, K, ring):
    """First context cell whose pool row survives the block's own writes [pos, pos+K): a cell's
    row is overwritten by its next congruent cell c+ring, so cells BELOW pos+K-ring carry NOISE
    (the dumped context at those cells is not in the pool) - comparing them is contamination."""
    return max(0, pos + K - ring) if ring > 0 else 0


def ring_pool_rows(pt, cells, ring, page, nkv):
    """Pool rows for logical cells: the same `cell % ring` the kernel applies (ring 0: identity).
    Malformed ring dumps (short tables/pools) are refused with a descriptive message."""
    if ring > 0:
        if ring % page:
            raise ValueError(f"ring {ring} is not a multiple of page {page}")
        if len(pt) != ring // page:
            raise ValueError(f"ring page table has {len(pt)} entries, expected {ring // page} "
                             f"(ring {ring}, page {page})")
        pc = cells % ring
    else:
        pc = cells
    over = np.nonzero(pc // page >= len(pt))[0]
    if len(over):
        raise ValueError(f"cells at span offsets {list(over[:4])} index past the {len(pt)}-entry "
                         f"page table: the dump is truncated or the meta is malformed")
    rows = pt[pc // page] * nkv * page + pc % page
    return rows


def check(path, directory):
    g=GGUFFile(path); m=g.metadata
    weights={}
    for t in g.tensors:
        if t.type_id != 30: raise ValueError('parity requires original BF16 weights')
        raw=np.memmap(path,mode='r',dtype='<u2',offset=g.data_start+t.offset,shape=(int(np.prod(t.shape)),))
        weights[t.name]=(raw.astype('<u4')<<16).view('<f4').reshape(tuple(reversed(t.shape)))
    return check_weights(weights, m, directory)

def check_weights(W, m, directory):
    """The stage oracle over an explicit canonical-weights dict (name -> F32 [out, in]) and a
    metadata dict with the dflash.* keys.  check() feeds it from the original BF16 GGUF; the
    safetensors harness (tools/test_inco_dflash2_parity.py) feeds it from the Inco checkpoint -
    the same formulas, the same gates, no duplicated oracle."""
    H=int(m['dflash.embedding_length']);D=int(m['dflash.attention.key_length'])
    nq=int(m['dflash.attention.head_count']);nkv=int(m['dflash.attention.head_count_kv'])
    taps=int(m['dflash.conv_kernel_size']);group=int(m['dflash.conv_group_size'])
    rank=int(m['dflash.selector_rank']);top=int(m['dflash.selector_top_k'])
    window=int(m.get('dflash.attention.sliding_window',0));theta=float(m['dflash.rope.frequency_base'])
    pos,K,anchor,mask,page,ring=unpack_meta((Path(directory)/'meta.bin').read_bytes())
    report=Report()
    def dump(name):return read_bin(directory,name)
    def stage(name,ref):
        return report.stage(name,dump(name),ref)
    def project(x,name):return bf16(x) @ W[name].T
    def conv(x,dynamic,base,side):
        y=np.zeros_like(x)
        for tap in range(min(taps,K)):
            coeff=base[side,tap][None,:]+np.repeat(dynamic[tap:,side,tap],group,axis=-1)
            y[tap:]+=coeff*x[:K-tap]
        return y
    def dynamic(x,name):return project(x,name+'_proj.weight').reshape(K,2,taps,H//group)
    def rope(x, positions=None):
        inv=np.power(np.float32(theta**(-2.0/D)),np.arange(D//2,dtype=np.float32))
        if positions is None: positions=np.arange(pos,pos+K,dtype=np.float32)
        ang=np.asarray(positions,dtype=np.float32)[:,None,None]*inv
        c,s=np.cos(ang),np.sin(ang)
        a,b=x[...,:D//2],x[...,D//2:]
        return np.concatenate((a*c-b*s,b*c+a*s),axis=-1)
    # Fusion inputs can be overwritten by later add_context calls; each dump is self-contained.
    if (Path(directory)/'tapsin.bin').exists():
        x=dump('tapsin').reshape(-1,W['fc.weight'].shape[1])
        stage('ctx',rmsnorm(project(x,'fc.weight'),W['enc.output_norm.weight']))
    # Context rows keyed by ABSOLUTE cell (the dump's p0), bounded to the retained span:
    # cells below pos+K-ring map to pool rows the block's own writes overwrite.
    lo = ring_context_span(pos, K, ring)
    context = {}
    for chunk in Path(directory).glob('ctx_b*.bin'):
        p0=int(chunk.stem.removeprefix('ctx_b'))
        values=dump(chunk.stem).reshape(-1,H)
        count=min(len(values),pos-p0)
        for r in range(count):
            c=p0+r
            if c>=lo:context[c]=values[r]
    h=dump('emb').reshape(K,H)
    for l in range(int(m['dflash.block_count'])):
        pre=f'blk.{l}.'
        x=rmsnorm(h,W[pre+'attn_norm.weight']);stage(f'xn{l}',x)
        dyn=dynamic(x,pre+'attn_conv');x=conv(x,dyn,W[pre+'attn_conv_base'],0)
        q=project(x,pre+'attn_q.weight').reshape(K,nq,D);stage(f'qraw{l}',q)
        k=project(x,pre+'attn_k.weight').reshape(K,nkv,D)
        v=project(x,pre+'attn_v.weight').reshape(K,nkv,D)
        q=rmsnorm(q,W[pre+'attn_q_norm.weight']);stage(f'qnormed{l}',q)
        q=rope(q);k=rope(rmsnorm(k,W[pre+'attn_k_norm.weight']))
        stage(f'q{l}',q);stage(f'k{l}',k);stage(f'v{l}',v)
        # Attention oracle reads paged FP16 pools, independently of the GPU attention, over the
        # window-bounded logical span only: the bounded ring maps every cell through the same
        # `cell % ring` the kernel applies; malformed ring dumps are refused descriptively.
        pt=dump(f'pt{l}').view('<i4')
        cells=ring_cells_span(pos,K,window,ring);start=int(cells[0]) if len(cells) else pos+K
        pr=ring_pool_rows(pt,cells,ring,page,nkv)
        kp=dump(f'kpool{l}').reshape(-1,D);vp=dump(f'vpool{l}').reshape(-1,D)
        if len(pr) and (int(pr.max())>=len(kp) or int(pr.max())>=len(vp)):
            raise ValueError(f"pool row {int(pr.max())} is past the dumped kpool/vpool "
                             f"({len(kp)} rows): truncated ring dump")
        if pos>lo:
            if len(context)!=pos-lo:
                raise ValueError(f"context chunks cover {len(context)} of {pos-lo} retained "
                                 f"cells [lo={lo},pos): malformed dump")
            rows=np.stack([context[c] for c in range(lo,pos)])
            expected_k=project(rows,pre+'attn_k.weight').reshape(pos-lo,nkv,D)
            expected_k=rope(rmsnorm(expected_k,W[pre+'attn_k_norm.weight']),np.arange(lo,pos))
            expected_v=project(rows,pre+'attn_v.weight').reshape(pos-lo,nkv,D)
            pool_rows=pr[lo-start:pos-start,None]+np.arange(nkv)[None,:]*page
            report.stage(f'context_k{l}',kp[pool_rows],expected_k.astype(np.float16).astype(np.float32))
            report.stage(f'context_v{l}',vp[pool_rows],expected_v.astype(np.float16).astype(np.float32))
        attn=np.zeros((K,nq,D),np.float32)
        for r in range(K):
            visible=np.abs(cells-(pos+r))<window if window else np.ones(len(cells),bool)
            for kv in range(nkv):
                hs=slice(kv*(nq//nkv),(kv+1)*(nq//nkv))
                keys=kp[pr[visible]+kv*page];values=vp[pr[visible]+kv*page]
                scores=q[r,hs]@keys.T/np.sqrt(np.float32(D))
                probs=np.exp(scores-scores.max(axis=-1,keepdims=True));probs/=probs.sum(axis=-1,keepdims=True)
                attn[r,hs]=probs@values
        stage(f'attn{l}',attn)
        output=project(attn.reshape(K,-1),pre+'attn_output.weight')
        h=h+conv(output,dyn,W[pre+'attn_conv_base'],1);stage(f'h_attn{l}',h)
        x=rmsnorm(h,W[pre+'ffn_norm.weight']);dyn=dynamic(x,pre+'ffn_conv')
        x=conv(x,dyn,W[pre+'ffn_conv_base'],0)
        gate=project(x,pre+'ffn_gate.weight');up=project(x,pre+'ffn_up.weight')
        output=project((gate/(1+np.exp(-gate)))*up,pre+'ffn_down.weight')
        h=h+conv(output,dyn,W[pre+'ffn_conv_base'],1);stage(f'h_mlp{l}',h)
    hidden=rmsnorm(h,W['output_norm.weight']);stage('final_norm',hidden)
    if (Path(directory)/'selector_hidden.bin').exists():
        projected=project(hidden,'selector_hidden.weight');stage('selector_hidden',projected)
        # Use engine logits: verifies selection independently of the target head's quantizer.
        logits=dump('head_logits').reshape(K,-1)
        candidates=np.argsort(-logits,axis=-1,kind='stable')[:,:top]
        ids=dump('selector_ids').view('<i4').reshape(K,top)
        if not np.array_equal(candidates,ids):raise AssertionError('top-k candidate IDs differ')
        prev=W['selector_predecessor.weight'];nxt=W['selector_successor.weight']
        picks=[];predecessor=anchor
        for r in range(1,K):
            scores=logits[r,ids[r]]+((prev[predecessor]*projected[r])[None,:]*nxt[ids[r]]).sum(axis=-1)
            predecessor=int(ids[r,np.argmax(scores)]);picks.append(predecessor)
        got=dump('head_picks').view('<i4')
        if not np.array_equal(picks,got):raise AssertionError(f'selector differs: {picks} vs {got.tolist()}')
        print('PASS: full-vocabulary top-k and conditioned selector IDs')
    report.print()
    return report.ok

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--dflash',required=True);ap.add_argument('--dir',required=True)
    a=ap.parse_args();raise SystemExit(0 if check(a.dflash,a.dir) else 1)

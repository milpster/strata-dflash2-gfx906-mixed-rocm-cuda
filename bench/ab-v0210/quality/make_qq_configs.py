#!/usr/bin/env python3
"""Quality-yardstick configs for the fleet packs (tools/quant_quality.py run).

Four arms, everything constant except the lineage/pack under test:
  REF  Swift-IQ3_S GGUF, no pack        (live-lineage weights anchor)
  A    pack iq3s                        (live prod's pack)   -> REF vs A = pack fidelity (live)
  C    Swift-1.5-IQ3_XXS GGUF, no pack  (swift15-lineage anchor)
  B    pack iq3xxs-15                   (swift15's pack)     -> C vs B = pack fidelity (swift15); REF vs C = quant delta

The harness pins the serving layer (expert-cache 1 on CPU, fp16 kv, --serve over stdio,
no helper, no drafts, fixed prefill/short-read), so dflash/helper/suffix-draft args are
absent on purpose: scored logits come from the target model only.
"""
import json
import pathlib

BASE = pathlib.Path("/home/user/dev/Strata/bench/ab-v0210/quality")
GGUF_IQ3S = "/mnt/user/ai/Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S.gguf"
GGUF_XXS15 = "/mnt/user/ai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf"
PLE = "/mnt/user/ai/Qwen3.8-Flash-Next-PLE-Q8_0.gguf"  # constant PLE source in every arm
PROFILE = "/mnt/user/Strata/data/expert-profile.bin"   # same profile file both lineages use

ENV = {
    "STRATA_ENGINE_NICE": "0",
    "STRATA_WATCHDOG_S": "1800",  # CPU-expert segments are slow; 300 (bench) could trip mid-segment
    "HSA_OVERRIDE_GFX_VERSION": "9.0.6",
    "HIP_FORCE_P2P": "1",
    "HSA_ENABLE_SDMA": "1",
    "CUDA_VISIBLE_DEVICES": "0,1",
    "STRATA_GFX906_TOPK_REG": "2",
    "STRATA_GFX906_TOPK_FIT17": "1",
    "STRATA_PRIMARY_COMMIT_OVERLAP": "1",
    "STRATA_NO_PCIE_GROUP": "1",
    "STRATA_HIP_PROMPT_F16": "1",
}


def cfg(model, pack, tok, name, exe=None, cwd=None):
    args = [
        "--gpu", "0,1",
        "--native", model,
        "--native-dense-gguf", model,
        "--ple-gguf", PLE,
        "--spec", "5",
        "--spec-min-p", "0.5",
        "--expert-profile", PROFILE,
        "--vram-reserve-mib", "640",
        "--vram-reserve-later-mib", "640",
        "--expert-cache-cpu-order",
        "--prompt-cache-spread",
    ]
    if pack:
        args[2:2] = ["--pack", pack]
    return {
        "exe": exe or "/home/user/dev/Strata/build-v0210/rpcw-hip/strata",
        "args": args,
        "cwd": cwd or "/home/user/dev/Strata",
        "tokenizer": tok,
        "model_name": name,
        "lib_dirs": ["/home/user/rocm-gfx906-xnack/lib", "/opt/rocm-6.1.0/lib"],
        "env": dict(ENV),
    }


STOCK_EXE = "/home/user/dev/strata-stock-v0210/build/rpcw-hip/strata"
STOCK_CWD = "/home/user/dev/strata-stock-v0210"

ARMS = {
    # No-pack arms are IMPOSSIBLE here: without --pack the engine reads the canonical Q2_0
    # "pack/full", whose CPU kernel needs AVX-512 (this CPU is AVX2) -> instant exit 1.
    # Packs are THE expert source, so the gate is stock binary vs relief binary on identical packs:
    #   REF vs A = the headline stack delta on the live pack (gate: ~0)
    #   C   vs B = the same on the swift15 pack            (gate: ~0)
    #   REF vs C = quant/lineage delta                     (informational)
    "qqREF": cfg(GGUF_IQ3S, "/mnt/user/ai/iq3s", "/mnt/user/ai/iq3s/tokenizer", "qq-ref-stock-pack-iq3s", exe=STOCK_EXE, cwd=STOCK_CWD),
    "qqA": cfg(GGUF_IQ3S, "/mnt/user/ai/iq3s", "/mnt/user/ai/iq3s/tokenizer", "qq-a-relief-pack-iq3s"),
    # S = A + STRATA_PREFILL_CPU_SHARE=auto: isolates the T-lane winner (promoted to prod 2026-11-05)
    # on our binary. The yardstick pins --prefill 512, so every scored chunk runs through the share
    # path (below the _MAX threshold) — the gate exercises the knob, it cannot bypass it.
    # S vs A = the share's own delta (expect upstream-documented KL~0.004 class); S vs REF = stack+share vs stock.
    "qqS": {**cfg(GGUF_IQ3S, "/mnt/user/ai/iq3s", "/mnt/user/ai/iq3s/tokenizer", "qq-s-relief-cpushare-iq3s"),
            "env": {**ENV, "STRATA_PREFILL_CPU_SHARE": "auto"}},
    "qqC": cfg(GGUF_XXS15, "/mnt/user/ai/iq3xxs-15", "/mnt/user/ai/iq3xxs-15/tokenizer", "qq-c-stock-pack-iq3xxs15", exe=STOCK_EXE, cwd=STOCK_CWD),
    "qqB": cfg(GGUF_XXS15, "/mnt/user/ai/iq3xxs-15", "/mnt/user/ai/iq3xxs-15/tokenizer", "qq-b-relief-pack-iq3xxs15"),
}

if __name__ == "__main__":
    BASE.mkdir(parents=True, exist_ok=True)
    (BASE / "out").mkdir(exist_ok=True)
    for n, c in ARMS.items():
        p = BASE / f"{n}.json"
        p.write_text(json.dumps(c, indent=1) + "\n")
        json.loads(p.read_text())  # it must round-trip
        print("wrote", p)

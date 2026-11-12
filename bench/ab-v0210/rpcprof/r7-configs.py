#!/usr/bin/env python3
"""Round-7 attribution A/B configs (2026-11-12).

Derives two trial configs from arm-m2.json, both EXACTLY the v0225 int8 panel
tuple as booted for the V25I8 panel (engine-v0225-int8.log line 133), differing
only in the arena re-rank arm:

  arm-r7a  : the panel tuple verbatim (attribution repro; STRATA_TRACE_ADAPT=1)
  arm-r7b  : + --expert-helper-relief-arena-cap-rerank
             + --expert-helper-relief-arena-cap-decode-mib 6000

Own sockets / logs / cache dir so the parent's trial state and the diet-soak
conversation-cache dir stay untouched.
"""
import json
import copy
import pathlib

SRC = pathlib.Path("/mnt/srcds/Strata/bench/ab-v0210/rpcprof/arm-m2.json")
OUT = pathlib.Path("/mnt/srcds/Strata/bench/ab-v0210/rpcprof")

# The v0225 V25I8 panel engine args, verbatim from the boot line.
ARGS = [
    "--gpu", "0,1",
    "--pack", "/mnt/srcds/ai/iq3xxs-15",
    "--native", "/mnt/srcds/ai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf",
    "--native-dense-gguf", "/mnt/srcds/ai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf",
    "--ple-gguf", "/mnt/srcds/ai/Qwen3.8-Flash-Next-PLE-Q8_0.gguf",
    "--expert-profile", "/mnt/srcds/Strata/data/expert-profile.bin",
    "--expert-cache", "auto",
    "--prefill", "16384",
    "--spec", "5",
    "--spec-min-p", "0.5",
    "--max-context", "262144",
    "--kv", "int8",
    "--vram-reserve-mib", "256",
    "--vram-reserve-later-mib", "256",
    "--kv-resident", "262144",
    "--pcie-frac", "0",
    "--expert-cache-cpu-order",
    "--prompt-cache-spread",
    "--conversation-cache-mib", "4096",
    "--conversation-cache-min-tokens", "2048",
    "--conversation-cache-park-gain", "4096",
    "--vision",
    "--dflash", "/mnt/srcds/dflash2/artifacts/inco-dflash2-q4_0.gguf",
    "--dflash-block", "5",
    "--suffix-draft", "0",
    "--expert-helper-slots", "4600",
    "--expert-helper-ram-relief",
    "--conversation-cache-dir", "/mnt/srcds/Strata/run/r7-conv-cache",
    "--conversation-cache-disk-mib", "24576",
    "--conversation-cache-disk-ram-mib", "64",
    "--conversation-cache-disk-idle-save",
    "--conversation-cache-ram-mib", "512",
    "--kv-resident-mib", "4096",
    "--conversation-cache-evict-page-cache",
    "--expert-helper-ram-relief-packs",
    "--expert-helper-relief-hot-mib", "15000",
    "--expert-helper-relief-arena-cap-mib", "22000",
    "--layer-split", "34",
    "--expert-helper-socket", "/mnt/srcds/Strata/run/r7-trial.sock",
    "--expert-helper-socket2", "/mnt/srcds/Strata/run/r7-trial-2.sock",
    "--expert-helper-timeout-ms", "10000",
]


def derive(arm: str, extra_args, log: str) -> dict:
    d = json.loads(SRC.read_text())
    d.pop("_ab9_diff", None)
    d["args"] = ARGS + list(extra_args)
    d["exe"] = "/mnt/srcds/Strata/v0225/build-mixed-gpu/rpcw-hip/strata"
    d["log"] = log
    d["model_name"] = f"swift-1.5-iq3_xxs-int8-{arm}"
    d["expert_helper_socket"] = "/mnt/srcds/Strata/run/r7-trial.sock"
    d["expert_helper_socket2"] = "/mnt/srcds/Strata/run/r7-trial-2.sock"
    d["port"] = 8021
    d["layer_split"] = "34"
    d["env"]["STRATA_TRACE_ADAPT"] = "1"   # per-window adapt visibility (both arms, identical)
    return d


for arm, extra, log in [
    ("r7a", [], "/mnt/srcds/Strata/logs/engine-r7a.log"),
    ("r7b", ["--expert-helper-relief-arena-cap-rerank",
             "--expert-helper-relief-arena-cap-decode-mib", "6000"],
     "/mnt/srcds/Strata/logs/engine-r7b.log"),
]:
    (OUT / f"trial-{arm}.json").write_text(json.dumps(derive(arm, extra, log), indent=1) + "\n")
    print(f"wrote trial-{arm}.json")

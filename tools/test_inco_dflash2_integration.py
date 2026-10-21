#!/usr/bin/env python3
"""Parent-owned offline preflight + explicit-execution plan for Inco DFlash2 (L9).

PLAN-ONLY by default: it inspects the REAL engine invocation (read-only pgrep), validates the
flag schema against the source semantics (line-cited), writes a PROTOTYPE config under
/mnt/user/dflash2/config/ (prod config is never touched), and prints the exact parent
commands with real paths.  --execute prints the same commands marked parent-owned; this
script NEVER launches, stops, restarts, or probes the engine/GPU - every GPU case is
emitted as a command for the parent, guarded: with the production engine detected running,
execution plans REFUSE unless the caller explicitly passes --expect-stopped (the user's
approved downtime state).

Source semantics verified in src/program/generate.cpp (read at 9c0223cd, cited per rule):
  --mtp/--dflash mutually exclusive (5440); --spec in [2, kVerifyMaxT=8] (5445-5454,
  verify_kernels.hpp:21); effective width dflash_k = min(--dflash-block, --spec-1) (5500) -
  "width 3" = --spec 4 --dflash-block 3 (1 anchor + 3 candidates); --dflash-block <= trained
  block 8 (5482; artifact block_size 8); --spec-min-p default 0.5 for dflash2 (5497);
  dflash2 refuses --dflash-head/--dflash-vocab selector variants (runtime bind 638-640);
  serve --batch + dflash refused (2836-2842); --pipeline-windows + dflash + split refused
  (4214); sampled CLI requests fall back to target-only (2830-2834); the split's head lives
  on the LAST stage (5198) and the drafter binds it (9180-9190).

  python tools/test_inco_dflash2_integration.py            # plan: schema + commands + manifest
  python tools/test_inco_dflash2_integration.py --execute  # same plan, marked for the parent
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

PROD_BINARY = "/mnt/user/Strata-2966/build-2966/strata"
PROTO_BINARY = "/mnt/user/dflash2/Strata-wt/build-gfx906-dflash2/strata"
ARTIFACT = "/mnt/user/dflash2/artifacts/inco-dflash2-bf16.gguf"
NATIVE = "/mnt/user/ai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf"
PACK = "/mnt/user/ai/iq3xxs-15"
PROFILE = "/mnt/user/Strata/data/expert-profile.bin"
CONFIG_DIR = pathlib.Path("/mnt/user/dflash2/config")
PROD_ENV_PREFIX = ("env LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib "
                   "HSA_ENABLE_SDMA=1 HSA_OVERRIDE_GFX_VERSION=9.0.6 HIP_VISIBLE_DEVICES=0,1 HIP_FORCE_P2P=1")

# The production serve invocation as observed live (pgrep 2026-10-21) - preserved verbatim.
PROD_SERVE_ARGS = ("--serve --gpu 0,1 --pack {pack} --native {native} --ple-gguf {native} "
                   "--expert-profile {profile} --expert-cache auto --prefill 8192 --spec 6 --spec-min-p 0.7 "
                   "--max-context 262144 --kv int8 --vram-reserve-mib 400 --vram-reserve-later-mib 550 "
                   "--kv-resident 262144 --pcie-frac 0 --expert-cache-cpu-order --prompt-cache-spread "
                   "--conversation-cache-mib 2048 --conversation-cache-min-tokens 2048 "
                   "--conversation-cache-park-gain 4096 --vision --layer-split 32").format(
                       pack=PACK, native=NATIVE, profile=PROFILE)

SCHEMA = [
    ("--dflash <gguf>", "drafter artifact; --mtp is refused with it (5440)", "dflash"),
    ("--dflash-block K", "0< = trained 8; width=min(K,spec-1) (5482,5500)", "dflash"),
    ("--dflash-window N", "capacity; prompt+max_new must fit (5567)", "dflash"),
    ("--dflash-mask-token", "CLI override wins over artifact 248077 (5476,5502)", "dflash"),
    ("--dflash-head/--dflash-vocab", "refused for dflash2 (shared full target head; runtime 638-640)", "dflash"),
    ("--spec T", "2..8 (kVerifyMaxT; 5445-5454)", "both"),
    ("--spec-min-p", "default 0.5 for dflash2 (5497)", "dflash"),
    ("--mtp <gguf>", "mutually exclusive with --dflash (5440); prod reference mode", "mtp"),
    ("--serve --layer-split 32 --gpu 0,1", "dflash allowed via plan validation at upload (5534); batch refused (2836)", "dflash"),
    ("--pipeline-windows", "auto-refused with dflash+split (4214)", "dflash"),
    ("sampled requests", "CLI/serve sampled -> target-only fallback, logged (2830, 12457)", "dflash"),
]

CASES = [
    ("greedy-prompt-chain", "serve request, greedy, 512 tokens, --dflash --layer-split 32",
     "startup line 'cross-GPU tap collection serial on CUDA1: boundary 6<-CUDA0 ... (HC stream mean capture)'; "
     "no 'stale'/'refus' lines; proposals/accept counters advance", "NOTRUN (GPU, prod-down)"),
    ("greedy-width3", "--spec 4 --dflash-block 3: width 3 (1 anchor + 3 candidates, 5500)",
     "engine reports K=3 drafts per cycle", "NOTRUN (GPU, prod-down)"),
    ("sampled-fallback", "--serve request temperature>0",
     "explicit 'sampled request uses target-only decoding' line; NEVER a falsely active DFlash claim",
     "NOTRUN (GPU, prod-down)"),
    ("repeat-conversation-reset", "two sequential conversations reusing pos0 keys",
     "reset_staging() at request start: no 'chunk slot rows disagree' on the second request",
     "NOTRUN (GPU, prod-down)"),
    ("cancel-restore", "cancel mid-prompt, parked-prefix restore, deep rewind (pos>200K)",
     "restore validates under OnDevice(drafter); no foreign tap reads; ring retained span respected",
     "NOTRUN (GPU, prod-down)"),
    ("eos-stop", "EOS + max_new stop with active drafter",
     "clean stream drain; collector slots released at release()", "NOTRUN (GPU, prod-down)"),
]


def prod_running() -> bool:
    """Read-only detection of the production engine (never touches it)."""
    try:
        out = subprocess.run(["pgrep", "-f", "build-2966/strata"], capture_output=True, text=True, check=False)
        return bool(out.stdout.strip())
    except OSError:
        return False


def build_plan() -> dict:
    proto_args = (PROD_SERVE_ARGS + " --dflash {artifact} --spec 4 --dflash-block 3 --spec-min-p 0.5").format(
        artifact=ARTIFACT)
    plan = {
        "schema": SCHEMA,
        "width_semantics": "width 3 = --spec 4 --dflash-block 3 -> dflash_k=min(3,4-1)=3 (generate.cpp:5500); "
                           "trained block 8 caps --dflash-block (5482); --spec<=8 (5445-5454)",
        "modes": {
            "prod_reference_MTP": f"{PROD_ENV_PREFIX} {PROD_BINARY} {PROD_SERVE_ARGS}",
            "new_target_only": f"{PROD_ENV_PREFIX} {PROTO_BINARY} {PROD_SERVE_ARGS}",
            "new_dflash": f"{PROD_ENV_PREFIX} {PROTO_BINARY} {proto_args}",
        },
        "cases": [{"case": c, "setup": s, "expected": e, "status": st} for c, s, e, st in CASES],
        "reuse": "GPU gates reuse tools/dflash_sampling.py (greedy/sampled/reset), tools/dflash_cache.py "
                 "(park/restore), tools/dflash2_stage_parity.py + tools/test_inco_dflash2_parity.py "
                 "(dump parity) - no parallel frameworks",
        "rules": ["no --batch with dflash (2836)", "no --pipeline-windows with dflash+split (4214)",
                  "sampled requests are target-only (2830) - reported explicitly",
                  "prod config NEVER edited; prototype config lives under /mnt/user/dflash2/config/",
                  "no script stops/restarts production; execution requires the user-approved downtime state"],
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / "inco-dflash2-prototype.json"
    path.write_text(json.dumps(plan, indent=2) + "\n")
    plan["written"] = str(path)
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true",
                    help="mark commands for execution by the PARENT (this script still never runs them)")
    ap.add_argument("--expect-stopped", action="store_true",
                    help="declare the user-approved state: production engine is stopped")
    a = ap.parse_args()
    plan = build_plan()
    running = prod_running()
    print(f"production engine detected: {'RUNNING' if running else 'not running'} "
          f"(read-only pgrep -f build-2966/strata)")
    print(f"prototype config written: {plan['written']} (prod config untouched)")
    print(json.dumps(plan, indent=2))
    if a.execute:
        if running and not a.expect_stopped:
            print("REFUSED: the production engine is RUNNING. This harness never stops/restarts it; every "
                  "GPU case needs the user-approved prod-down window. Re-run with --expect-stopped only "
                  "after the user has approved and observed the stopped state.")
            return 2
        print("EXECUTION PLAN (parent-owned): run each 'modes' command from the printed prefix; the "
              "GPU cases above carry their expected observables. This script executed NOTHING.")
    else:
        print("plan-only mode: no GPU, no network inference, no process mutation performed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""ab9 runtime-config builder: turns a validated arm into the config the harness boots.

Stage-1 arms (A/B/C): the LIVE prod config with env overrides only
  A: STRATA_PREFILL_HELPER=0            B: =1, PIPE=0            C: =1, PIPE=1
  (+ STRATA_RPC_PROFILE=1, STRATA_DBG_PROMPT_HELPER=1 on every arm).
  The prod config file itself is never edited; the runtime copy lives in out/ab9/run/.
Stage-2 arms (m1..m4): the validated arm-m*.json (single-key diff from prod, verified by
  validate-arms.py) plus the same harness plumbing (log path + profile env).

Also emits the helper-side args line ("<slots> <fill_mib> <reserve_mib>") so arm m3
(helper fill 7200 + reserve 128 — a helper-args lever, zero config keys) reaches the boot.
"""
import argparse
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
PROD_CONFIG = ROOT / "prod-config-swift15-f16kv-dflash-w4-q4_0-mixed.json"
ARMS_DIR = pathlib.Path(__file__).resolve().parent
STAGE1_ENV = {
    "A": {"STRATA_PREFILL_HELPER": "0"},
    "B": {"STRATA_PREFILL_HELPER": "1", "STRATA_PREFILL_HELPER_PIPE": "0"},
    "C": {"STRATA_PREFILL_HELPER": "1", "STRATA_PREFILL_HELPER_PIPE": "1"},
    # D: arm-B env shape on the helper-ipc-diet binaries (exe/_ab9_helper_exe from arm-d.json)
    "D": {"STRATA_PREFILL_HELPER": "1", "STRATA_PREFILL_HELPER_PIPE": "0"},
    # E: helper-OFF on the diet engine (exe/_ab9_helper_exe from arm-e.json) - separates
    # the diet engine's own local-path gains from the helper's (attempt-4 anomaly)
    "E": {"STRATA_PREFILL_HELPER": "0"},
}
PROFILE_ENV = {"STRATA_RPC_PROFILE": "1", "STRATA_DBG_PROMPT_HELPER": "1"}
DEFAULT_HELPER = {"slots": 4600, "fill_mib": 6796, "reserve_mib": 300}  # prod launcher line 84-85


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, choices=["A", "B", "C", "D", "E", "m1", "m2", "m3", "m4"])
    ap.add_argument("--out", required=True, help="runtime config to write")
    ap.add_argument("--elog", required=True, help="engine log path for this arm's boot")
    ap.add_argument("--helper-args", required=True, help="file to write '<slots> <fill> <reserve>'")
    a = ap.parse_args()

    arm = a.arm
    if arm in ("D", "E"):  # diet-binaries arms: prod config verbatim, exe/_ab9_helper_exe swapped
        cfg = json.loads((ARMS_DIR / f"arm-{arm.lower()}.json").read_text())
    else:
        cfg = json.loads(PROD_CONFIG.read_text()) if arm in STAGE1_ENV \
            else json.loads((ARMS_DIR / f"arm-{arm}.json").read_text())
    if "env" not in cfg:
        cfg["env"] = {}
    if arm in STAGE1_ENV:
        cfg["env"].update(STAGE1_ENV[arm])
    cfg["env"].update(PROFILE_ENV)
    cfg["log"] = a.elog  # harness-owned log so the collector slices a private file
    cfg["_ab9_arm"] = arm
    pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(a.out).write_text(json.dumps(cfg, indent=1, sort_keys=True) + "\n")

    import os
    helper = {"fill_mib": int(os.environ.get("AB9_HELPER_FILL_MIB", DEFAULT_HELPER["fill_mib"])),
              "reserve_mib": int(os.environ.get("AB9_HELPER_RESERVE_MIB", DEFAULT_HELPER["reserve_mib"])),
              # the helper's --slots must equal THIS arm's ask (the ud socket-name class of defect:
              # a per-arm slot change in the config must reach the helper command line too)
              "slots": int(os.environ.get("AB9_HELPER_SLOTS", cfg.get("expert_helper_slots", DEFAULT_HELPER["slots"])))}
    note = cfg.get("_ab9_helper")
    if note:
        helper.update({k: int(v) for k, v in note.items()})
    pathlib.Path(a.helper_args).write_text(f"{helper['slots']} {helper['fill_mib']} {helper['reserve_mib']}\n")
    # per-arm helper BINARY (empty = the env's default): the diet arm swaps engine AND helper
    pathlib.Path(a.helper_args + ".bin").write_text(cfg.get("_ab9_helper_exe", "") + "\n")
    print(f"ab9_mkconfig: arm={arm} cfg={a.out} elog={a.elog} helper={helper}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

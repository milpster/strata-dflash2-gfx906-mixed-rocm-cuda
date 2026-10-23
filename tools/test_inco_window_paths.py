#!/usr/bin/env python3
"""CPU fixtures for the window-harness PATH overrides (integ-023). Parent runs this; nothing here
touches production, the GPU, any port, or any service, and nothing writes into the source tree.

WHAT IS PROVEN (and what is NOT weakened by the overrides):
  * DEFAULTS BYTE-FOR-BYTE: with the env absent, the cloner (tools/test_inco_dflash2_integration.py)
    and the branch-tracked controller (tools/window_harness/model_window.py) keep the literal paths
    every earlier lane used: the prototype binary .../Strata-wt/build-gfx906-dflash2/strata, the
    config dir /mnt/user/dflash2/config, and PROD_BINARY/PROD_CONFIG/PROD_EXE unchanged.
  * AN ABSOLUTE OVERRIDE IS HONORED BY BOTH, and they always resolve THE SAME binary and config dir
    (this is the blocker the lane had: the imported clone-provider wrote configs with its own
    hardcoded binary/dir while the controller checked against env-selected values).
  * A RELATIVE OVERRIDE IS REFUSED at import time - never resolved against a cwd.
  * THE GUARDS STILL REFUSE: an exe that is not the mode's binary, a log that is not the mode's
    exact log, a value/key drift from the CURRENT production config, a dropped flag, an extra key,
    an invented mode label.
  * THE OLD PRODUCTION ARM IS NOT OVERRIDABLE: old_target_only keeps PROD_BINARY/W.PROD_EXE, and
    the structured layer_split (production '32' at context 262144) is preserved verbatim by every
    clone, so the two-GPU split contract is untouched.

Run: python3 tools/test_inco_window_paths.py     (exit 0 = all fixtures pass)
"""
from __future__ import annotations

import copy
import importlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent          # tools/
HARNESS = HERE / "window_harness"                       # the branch-tracked controller copy
sys.path.insert(0, str(HARNESS))                        # window/model_window/greedy_gate...
import model_window as mw                                # noqa: E402  the branch-tracked controller
import window as W                                       # noqa: E402  snapshot/stop/restore constants
sys.path.insert(0, str(HERE))
import test_inco_dflash2_integration as I                # noqa: E402  the reviewed cloner

ENV_KEYS = ("STRATA_WINDOW_CLONE", "STRATA_WINDOW_PROTO_BINARY", "STRATA_WINDOW_PROTO_SERVER",
            "STRATA_WINDOW_CONFIG_DIR")
DEF_PROTO = "/mnt/user/dflash2/Strata-wt/build-gfx906-dflash2/strata"
DEF_CONFIG = "/mnt/user/dflash2/config"
DEF_PROD_BINARY = "/mnt/user/Strata-2966/build-2966/strata"
OVERRIDE_PROTO = "/mnt/user/dflash2/Strata-integ/build-matrix-e1h0/strata"
FAILS: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        FAILS.append(what)


def set_env(overrides: dict) -> None:
    """Re-import the cloner AND the controller with EXACTLY these path variables, so every fixture
    sees the same pair the parent's command line gives them (defaults when a key is absent)."""
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update(overrides)
    importlib.reload(I)
    importlib.reload(mw)


def synth_prod() -> dict:
    """Synthetic production config: the REAL schema keys and the REAL flag shapes (the real
    production file is never read by these fixtures). layer_split is production's '32'."""
    args = ["--gpu", "0,1", "--pack", "/synth/pack", "--native", "/synth/model.gguf",
            "--native-dense-gguf", "/synth/model.gguf", "--ple-gguf", "/synth/ple.gguf",
            "--expert-profile", "/synth/profile.bin",
            "--expert-cache", "auto", "--prefill", "8192", "--spec", "6", "--spec-min-p", "0.7",
            "--max-context", "262144", "--kv", "int8", "--vram-reserve-mib", "896",
            "--vram-reserve-later-mib", "1000", "--kv-resident", 262144, "--pcie-frac", "0",
            "--expert-cache-cpu-order", "--prompt-cache-spread", "--conversation-cache-mib", "2048",
            "--conversation-cache-min-tokens", "2048", "--conversation-cache-park-gain", "4096",
            "--vision"]
    return {"exe": DEF_PROD_BINARY, "args": args, "cwd": "/synth/prod-cwd",
            "tokenizer": "/synth/tok", "model_name": "swift-1.5-iq3_xxs",
            "log": "/mnt/user/dflash2/logs/prod-engine.log", "env": {"HOME": "/synth"},
            "env_to_user": {}, "lib_dirs": ["/synth/lib"], "vision": True, "gpu": "0,1",
            "host": "127.0.0.1", "port": 8009, "backend": "strata", "layer_split": "32",
            "fit_max_tokens": 8192, "reasoning_budget_tokens": 4096, "reasoning_effort": "none",
            "sampling": 1.0}


MODES = ["suffix_lookup", "target_only", "old_target_only", "mtp", "dflash_w3"]


# ------------------------------------------------------------------ 1. defaults are byte-identical
def fixture_defaults() -> None:
    set_env({})
    check(I.PROTO_BINARY == DEF_PROTO, f"cloner default PROTO_BINARY is the literal {DEF_PROTO}")
    check(str(I.CONFIG_DIR) == DEF_CONFIG, f"cloner default CONFIG_DIR is the literal {DEF_CONFIG}")
    check(I.PROD_BINARY == DEF_PROD_BINARY, "cloner PROD_BINARY (OLD production arm) is unchanged")
    check(mw.CLONE == "/mnt/user/dflash2/Strata-wt" and mw.PROTO_BINARY == DEF_PROTO,
          "controller default CLONE/PROTO_BINARY are the parent's pre-existing literals")
    check(str(mw.CONFIG_DIR) == DEF_CONFIG, "controller default CONFIG_DIR is the literal /mnt/user/dflash2/config")
    check(mw.PROTO_SERVER == "/mnt/user/dflash2/Strata-wt/serve/server.py",
          "controller default PROTO_SERVER is the parent's pre-existing literal")
    prod = synth_prod()
    exe_ok = all(I.clone_server_config(m, prod)["exe"] == DEF_PROTO for m in MODES if m != "old_target_only")
    check(exe_ok, "with no env every NEW-binary clone still points at the default prototype binary")
    check(all(I.clone_server_config(m, prod)["port"] == 8019 for m in MODES),
          "every clone stays on the isolated 8019 (production 8009 never appears in a clone)")
    check(all(I.clone_server_config(m, prod)["log"].startswith("/mnt/user/dflash2/logs/") for m in MODES),
          "clone logs remain ABSOLUTE under the project log dir (never relative, never the prod tree)")
    check(all(I.clone_server_config(m, prod)["log"] == mw.log_want(m) for m in MODES),
          "the cloner's log names match the controller's exact per-mode log contract (no waived path)")


# ------------------------------------------------------------------ 2. absolute overrides, both sides
def fixture_absolute_override(td: pathlib.Path) -> None:
    cfgdir = td / "cfg"
    set_env({"STRATA_WINDOW_CLONE": "/mnt/user/dflash2/Strata-integ",
             "STRATA_WINDOW_PROTO_BINARY": OVERRIDE_PROTO,
             "STRATA_WINDOW_PROTO_SERVER": "/mnt/user/dflash2/Strata-integ/serve/server.py",
             "STRATA_WINDOW_CONFIG_DIR": str(cfgdir)})
    check(I.PROTO_BINARY == OVERRIDE_PROTO, "cloner honors an ABSOLUTE STRATA_WINDOW_PROTO_BINARY")
    check(str(I.CONFIG_DIR) == str(cfgdir), "cloner honors an ABSOLUTE STRATA_WINDOW_CONFIG_DIR")
    check(mw.PROTO_BINARY == OVERRIDE_PROTO, "controller honors the same STRATA_WINDOW_PROTO_BINARY")
    check(str(mw.CONFIG_DIR) == str(cfgdir), "controller honors the same STRATA_WINDOW_CONFIG_DIR")
    check(mw.PROTO_BINARY == I.PROTO_BINARY and str(mw.CONFIG_DIR) == str(I.CONFIG_DIR),
          "imported clone-provider and controller resolve THE SAME binary and config dir (the blocker)")

    prod = synth_prod()
    new = [m for m in MODES if m != "old_target_only"]
    check(all(I.clone_server_config(m, prod)["exe"] == OVERRIDE_PROTO for m in new),
          "every NEW-binary arm's cloned exe is the selected binary (suffix/target/mtp/dflash_w3)")
    check(I.clone_server_config("old_target_only", prod)["exe"] == DEF_PROD_BINARY,
          "the OLD production arm keeps PROD_BINARY - the override never redirects it")
    check(I.clone_server_config("dflash_w3", prod)["log"] == mw.log_want("dflash_w3")
          and I.clone_server_config("old_target_only", prod)["log"] == mw.log_want("old_target_only"),
          "log fields stay the contract's paths under the override (logs are not redirected)")
    check(I.clone_server_config("dflash_w3", prod)["port"] == 8019,
          "port override is NOT part of the mechanism: the clone stays on 8019")

    # --plan writes ONLY into the selected dir (the retained /config clones are never touched).
    cfgdir.mkdir(parents=True, exist_ok=True)
    paths = I.write_artifacts(prod)
    check(all(pathlib.Path(p).is_relative_to(cfgdir) for p in paths.values() if p.endswith(".json")),
          "write_artifacts writes manifest + clones ONLY inside the selected config dir")
    check((cfgdir / "inco-dflash2-plan-manifest.json").is_file()
          and (cfgdir / "proto-server-swift15-dflash_w3.json").is_file()
          and (cfgdir / "proto-server-swift15-old_target_only.json").is_file(),
          "the selected dir holds the new manifest + one config per mode")
    man = json.loads((cfgdir / "inco-dflash2-plan-manifest.json").read_text())
    check(man["modes"]["new_dflash_w3"]["exe"] == OVERRIDE_PROTO
          and man["modes"]["new_suffix_lookup"]["exe"] == OVERRIDE_PROTO
          and man["modes"]["old_target_only"]["exe"] == DEF_PROD_BINARY
          and man["modes"]["prod_reference"]["exe"] == DEF_PROD_BINARY,
          "the manifest labels the new arms with the selected binary and the old arms with PROD_BINARY")

    # currency accepts the cloner's own configs under the selected dir (no exe/log drift problems).
    for mode, width in (("suffix_lookup", 1), ("target_only", 1), ("old_target_only", 1), ("dflash_w3", 3)):
        _sum, problems = mw.currency(mode, width, prod)
        check(problems == [], f"currency is clean for the cloner's {mode} config in the selected dir {problems[:1]}")

    # and currency REFUSES an exe that is not the mode's binary (the exact old-lane mismatch).
    p = cfgdir / "proto-server-swift15-dflash_w3.json"
    good = json.loads(p.read_text())
    stale = copy.deepcopy(good)
    stale["exe"] = DEF_PROTO                                   # the pre-override binary
    p.write_text(json.dumps(stale, indent=2) + "\n")
    _sum, problems = mw.currency("dflash_w3", 3, prod)
    check(any("is not the mode's binary" in s for s in problems),
          f"currency REFUSES a config whose exe is the pre-override binary: {problems[:1]}")
    p.write_text(json.dumps(good, indent=2) + "\n")


# ------------------------------------------------------------------ 3. guards stay armed
def fixture_guards(td: pathlib.Path) -> None:
    cfgdir = td / "guards"
    cfgdir.mkdir(parents=True, exist_ok=True)
    set_env({"STRATA_WINDOW_PROTO_BINARY": OVERRIDE_PROTO, "STRATA_WINDOW_CONFIG_DIR": str(cfgdir)})
    prod = synth_prod()
    cases = {
        "dflash_w3": 3, "old_target_only": 1, "target_only": 1, "suffix_lookup": 1,
    }
    for mode, width in cases.items():
        (cfgdir / f"proto-server-swift15-{mode}.json").write_text(
            json.dumps(I.clone_server_config(mode, prod), indent=2) + "\n")

    def currency_of(mode: str, width: int) -> list[str]:
        return mw.currency(mode, width, prod)[1]

    check(currency_of("dflash_w3", 3) == [] and currency_of("old_target_only", 1) == [],
          "baseline: unmodified clones are clean (the refusals below are the ONLY diffs)")
    check(any("no intended-diff template" in s for s in currency_of("invented_mode", 1)),
          "currency REFUSES an invented mode label (controllers never invent a mode)")
    check(not any("missing" in s for s in currency_of("target_only", 1)),
          "sanity: the target_only fixture config exists in the selected dir")

    def tamper(mode: str, key: str, value) -> list[str]:
        p = cfgdir / f"proto-server-swift15-{mode}.json"
        base = json.loads(p.read_text())
        bad = copy.deepcopy(base)
        bad[key] = value
        p.write_text(json.dumps(bad, indent=2) + "\n")
        problems = currency_of(mode, 3 if mode.startswith("dflash_w") else 1)
        p.write_text(json.dumps(base, indent=2) + "\n")
        return problems

    check(any("not the mode's binary" in s for s in tamper("dflash_w3", "exe", "/synth/other-exe")),
          "exe mismatch is refused (an arbitrary exe never passes the identity check)")
    check(any("is not the mode's exact log" in s for s in tamper("dflash_w3", "log", "/synth/other.log")),
          "log drift is refused (evidence can never be read from an unselected log)")
    check(any("is not the isolated" in s for s in tamper("dflash_w3", "port", 8009)),
          "a clone aimed at production's port is refused")
    check(any("differs from production" in s for s in tamper("dflash_w3", "tokenizer", "/synth/other-tok")),
          "config drift on a non-intended key is refused (tokenizer)")
    check(any("differs from production" in s for s in tamper("dflash_w3", "layer_split", "16")),
          "config drift is refused on the split key itself (a clone never picks its own split)")

    p = cfgdir / "proto-server-swift15-dflash_w3.json"
    base = json.loads(p.read_text())
    dropped = copy.deepcopy(base)
    a = dropped["args"]
    del a[a.index("--dflash") + 1], a[a.index("--dflash")]
    p.write_text(json.dumps(dropped, indent=2) + "\n")
    check(any("added flags" in s for s in currency_of("dflash_w3", 3)),
          "a dropped drafter flag is refused (the intended structural diff must match exactly)")
    extra = copy.deepcopy(base)
    extra["surprise_key"] = 1
    p.write_text(json.dumps(extra, indent=2) + "\n")
    check(any("is not in the production config" in s for s in currency_of("dflash_w3", 3)),
          "an extra key not in production is refused")
    reordered = copy.deepcopy(base)
    reordered["args"] = list(reordered["args"])
    reordered["args"].reverse()
    p.write_text(json.dumps(reordered, indent=2) + "\n")
    check(bool(currency_of("dflash_w3", 3)), "a reordered flag list is refused (the clone appends, never reorders)")
    p.write_text(json.dumps(base, indent=2) + "\n")
    check(currency_of("dflash_w3", 3) == [], "the restored clone is clean again (the tampering is the only cause)")

    # split32 + schema preservation under the override, for every mode.
    for m in MODES:
        c = I.clone_server_config(m, prod)
        check(c.get("layer_split") == prod["layer_split"] == "32",
              f"{m}: the structured layer_split '32' survives the clone verbatim (server-derived --layer-split)")
        check(c.get("gpu") == prod["gpu"] and c.get("env") == prod["env"]
              and c.get("lib_dirs") == prod["lib_dirs"] and c.get("vision") == prod["vision"]
              and c.get("model_name") == prod["model_name"] and c.get("cwd") == prod["cwd"],
              f"{m}: schema/split/GPU/weights/env/cwd/tokenizer fields are preserved, only exe/log/port move")
    check(mw.EXE_WANT.get("old_target_only") == W.PROD_EXE,
          "the controller's old-arm binary stays window.PROD_EXE (never env-driven)")
    check(any(s in I.PROD_CONFIG for s in ("prod-config-swift15.json",)),
          "the production config path the cloner reads stays the original production file")
    check(str(W.LOGS) == "/mnt/user/dflash2/logs" and str(W.OPS) == "/mnt/user/dflash2/ops",
          "results/logs keep going to the project dirs, never into the source tree")


# ------------------------------------------------------------------ 4. relative overrides refused
def fixture_relative_refused() -> None:
    set_env({})
    for var in ("STRATA_WINDOW_PROTO_BINARY", "STRATA_WINDOW_CONFIG_DIR"):
        try:
            I._abs_override(var, "rel/path")
            check(False, f"{var}=rel/path: REFUSAL expected, none raised")
        except ValueError as e:
            check("REFUSED" in str(e) and "ABSOLUTE" in str(e), f"cloner helper refuses a relative {var}")
        for mod in ("test_inco_dflash2_integration", "model_window"):
            r = subprocess.run([sys.executable, "-c", f"import {mod}"], cwd="/tmp/agent",
                               env={**os.environ, var: "rel/path",
                                    "PYTHONPATH": f"{HERE}:{HARNESS}"},
                               capture_output=True, text=True, timeout=60)
            check(r.returncode != 0 and "REFUSED" in (r.stderr + r.stdout),
                  f"import-time refusal for a relative {var} in {mod} (rc={r.returncode})")
    set_env({})
    check(I.PROTO_BINARY == DEF_PROTO and str(I.CONFIG_DIR) == DEF_CONFIG
          and mw.PROTO_BINARY == DEF_PROTO and str(mw.CONFIG_DIR) == DEF_CONFIG,
          "after an empty env the defaults are restored byte-for-byte (no state leaks between fixtures)")
    set_env({k: "" for k in ("STRATA_WINDOW_PROTO_BINARY", "STRATA_WINDOW_CONFIG_DIR")})
    check(I.PROTO_BINARY == DEF_PROTO and str(I.CONFIG_DIR) == DEF_CONFIG,
          "an EMPTY override means the default, not an empty path")
    set_env({})


def main() -> int:
    with tempfile.TemporaryDirectory() as tdname:
        td = pathlib.Path(tdname)
        fixture_defaults()
        fixture_absolute_override(td)
        fixture_guards(td)
    fixture_relative_refused()
    check(not (HARNESS / "inco-dflash2-plan-manifest.json").exists(),
          "no manifest/config artifact was ever written into tools/window_harness")
    print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nall path fixtures pass")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

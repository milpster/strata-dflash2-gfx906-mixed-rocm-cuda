#!/usr/bin/env python3
"""port_ab_boot_plan.py - the frozen boot plan a Phase-2 boot runs from.

port_ab_boot.sh calls this twice:
  --plan    write a shell-sourceable plan (paths, identities, the helper's full argv and env)
            and FAIL CLOSED if the derived config is not byte-identical to the manifest pin,
            or if an engine/helper sha left the manifest. Nothing is booted, nothing is
            cleared; the plan file is /run/<boot key>.env and is evidence in its own right.
  --record  assert the cache dir is empty and write boots/<boot key>.json (started_at/epoch,
            loadavg, page cache, the exact helper argv/env, the identities the boot will use).

Why a separate module: the boot used to re-derive the config in bash and keep its own copies
of fill/slots/reserve, so a boot could silently differ from the frozen tuple. Every value here
comes from the manifest and port_ab_configs, never from a shell default.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shlex
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import port_ab_configs as pc
import port_ab_contract as C


def sha256_path(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(path):
    path = pathlib.Path(path)
    if not path.is_file():
        raise SystemExit("REFUSING: missing frozen manifest " + str(path)
                         + " (run port_ab_prepare.py --prepare)")
    return json.loads(path.read_text())


def page_cache():
    cached = majfault = None
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("Cached:"):
                cached = int(line.split()[1]) // 1024
                break
    with open("/proc/vmstat") as f:
        for line in f:
            if line.startswith("pgmajfault "):
                majfault = int(line.split()[1])
                break
    return {"cached_mib": cached, "pgmajfault": majfault}


def build(args):
    """Everything a boot needs, with every frozen value taken from the manifest."""
    manifest = load_manifest(args.manifest)
    cfg, deltas, paths = pc.derive(args.arm, window=args.window, rep=args.rep, out=args.out,
                                   boot=args.boot)
    # the window's margins mode decides the reserve gate (W1d: both flags must be ABSENT)
    problems = pc.check_tuple(cfg, args.arm, window=args.window)
    if problems:
        raise SystemExit("REFUSING: " + "; ".join(problems))

    key = paths["key"]
    entry = (manifest.get("configs") or {}).get(key)
    if not entry:
        raise SystemExit("REFUSING: boot " + key + " is not in the frozen manifest; "
                         "re-run port_ab_prepare.py --prepare")
    body = (json.dumps(cfg, indent=1, sort_keys=True) + "\n").encode()
    derived_sha = hashlib.sha256(body).hexdigest()
    pinned = entry["sha256"]
    config_path = pathlib.Path(paths["config"])
    if not config_path.is_file():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_bytes(body)
    actual = sha256_path(config_path)
    if actual != pinned:
        raise SystemExit("REFUSING: " + str(config_path) + " is " + actual[:16]
                         + ", the frozen manifest pins " + pinned[:16]
                         + ": the config on disk is not the frozen one")
    if derived_sha != pinned:
        raise SystemExit("REFUSING: the generator now produces " + derived_sha[:16]
                         + " for " + key + ", the manifest pins " + pinned[:16]
                         + ": regenerate the manifest deliberately, never at boot")

    for arm in ("A", "B"):
        spec = pc.ARMS[arm]
        for kind, path in (("engine", pc.ROOT / spec["engine"]),
                           ("helper", pc.ROOT / spec["helper"])):
            want = manifest["arms"][arm][kind]["sha256"]
            got = sha256_path(path)
            if got != want:
                raise SystemExit("REFUSING: " + kind + " " + str(path) + " is " + got[:16]
                                 + ", the manifest pins " + want[:16])
    engine = pc.ROOT / pc.ARMS[args.arm]["engine"]
    helper = pc.ROOT / pc.ARMS[args.arm]["helper"]
    work = pc.ROOT / pc.ARMS[args.server_arm]["server_tree"]
    helper_argv = pc.helper_argv(args.arm, paths)
    helper_env = pc.helper_env()
    return {"key": key, "cfg": cfg, "paths": paths, "engine": engine, "helper": helper,
            "work": work, "helper_argv": helper_argv, "helper_env": helper_env,
            "manifest": manifest, "config_sha": pinned,
            "engine_sha": manifest["arms"][args.arm]["engine"]["sha256"],
            "helper_sha": manifest["arms"][args.arm]["helper"]["sha256"]}


def plan_text(plan, args):
    p, paths = plan["paths"], plan["paths"]
    lines = [
        "P_BOOT_ID=" + shlex.quote(plan["key"]),
        "P_CONFIG=" + shlex.quote(paths["config"]),
        "P_SOCK=" + shlex.quote(paths["socket"]),
        "P_SOCK2=" + shlex.quote(paths["socket2"]),
        "P_ELOG=" + shlex.quote(plan["cfg"]["log"]),
        "P_HLOG=" + shlex.quote(paths["helper_log"]),
        "P_BLOG=" + shlex.quote(paths["boot_log"]),
        "P_CACHE=" + shlex.quote(paths["cache_dir"]),
        "P_ENGINE=" + shlex.quote(str(plan["engine"])),
        "P_HELPER=" + shlex.quote(str(plan["helper"])),
        "P_WORK=" + shlex.quote(str(plan["work"])),
        "P_PORT=" + shlex.quote(str(plan["cfg"]["port"])),
        "P_CONFIG_SHA=" + shlex.quote(plan["config_sha"]),
        "P_ENGINE_SHA=" + shlex.quote(plan["engine_sha"]),
        "P_HELPER_SHA=" + shlex.quote(plan["helper_sha"]),
        "P_HELPER_ARGV=(" + " ".join(shlex.quote(a) for a in plan["helper_argv"]) + ")",
        "P_HELPER_ENV=(" + " ".join(shlex.quote(k + "=" + v)
                                    for k, v in plan["helper_env"].items()) + ")",
    ]
    return "\n".join(lines) + "\n"


def record(plan, args):
    cache = pathlib.Path(plan["paths"]["cache_dir"])
    if cache.exists() and any(cache.iterdir()):
        raise SystemExit("REFUSING: cache dir not empty (a stale checkpoint would be served as "
                         "a fresh prefill): " + str(cache))
    cache.mkdir(parents=True, exist_ok=True)
    boot_dir = pathlib.Path(args.out) / "boots"
    boot_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "key": plan["key"], "window": args.window, "arm": args.arm,
        "boot_group": args.boot, "rep": args.rep, "server_arm": args.server_arm,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "started_epoch": time.time(),
        "config": plan["paths"]["config"], "config_sha256": plan["config_sha"],
        "engine": str(plan["engine"]), "engine_sha256": plan["engine_sha"],
        "helper": str(plan["helper"]), "helper_sha256": plan["helper_sha"],
        "server_tree": str(plan["work"]),
        "sockets": [plan["paths"]["socket"], plan["paths"]["socket2"]],
        "port": plan["cfg"]["port"], "cache_dir": str(cache), "cache_empty_at_boot": True,
        "helper_argv": plan["helper_argv"], "helper_env": plan["helper_env"],
        "loadavg": open("/proc/loadavg").read().split()[:3],
        "page_cache": page_cache(),
        "plan_file": str(pathlib.Path(args.out) / "run" / (plan["key"] + ".env")),
    }
    (boot_dir / (plan["key"] + ".json")).write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    return record


def main():
    ap = argparse.ArgumentParser(description="write the frozen plan / boot record of one boot")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--window", required=True)
    ap.add_argument("--arm", required=True, choices=["A", "B"])
    ap.add_argument("--boot", required=True)
    ap.add_argument("--rep", type=int, required=True)
    ap.add_argument("--server-arm", default=None)
    ap.add_argument("--out", type=pathlib.Path, default=pc.OUT_DEFAULT)
    ap.add_argument("--manifest", type=pathlib.Path, default=HERE / "port-ab-manifest.json")
    a = ap.parse_args()
    a.server_arm = a.server_arm or a.arm
    plan = build(a)
    if a.plan:
        (a.out / "run").mkdir(parents=True, exist_ok=True)
        sys.stdout.write(plan_text(plan, a))
        return 0
    if a.record:
        record(plan, a)
        sys.stdout.write(plan_text(plan, a))
        return 0
    ap.error("choose --plan or --record")


if __name__ == "__main__":
    raise SystemExit(main())

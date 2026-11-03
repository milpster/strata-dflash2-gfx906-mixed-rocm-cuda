#!/usr/bin/env python3
"""Boundary-regression driver for the Q8_K expert-helper wire (default OFF).

Reuses finish_window.py's proven measure pieces verbatim - helper_argv,
reference_argv, reference_env, window_paths, the strict-v2 auditor, the
sha256 pinning, the helper teardown ladder - with exactly three recorded
differences:

  1. The helper and the reference harness are CANDIDATE builds passed on the
     command line (their sha256 lands in the result JSON); the protected
     rpcw-*-nmse2 binaries the pooled verdict used are never touched.
  2. STRATA_EXPERT_HELPER_Q8K=1 is injected into the reference harness env
     (the engine's opt-in gate; the candidate helper advertises kFeatureQ8K,
     the candidate harness's begin_prompt then ships image_format 2 frames).
  3. NO production outage and NO restore: this driver REFUSES to start while
     any pair_supervisor / canonical launcher / serve server.py is alive, and
     signals nothing. The session granted "production down until fixed",
     which supersedes the between-window recovery contract; the canonical
     service is restored by hand at final closeout.

Everything the pooled verdict depends on stays UNCHANGED: the fin1 capture
(hardlinked read-only into the family dir), the fixtures, the layer
coverage, the report schema, the strict-v2 audit and the relative
p99.9<=0.005 / max<=0.02 gates. Without --execute the driver only prints
the plan it verified.

Usage:
  python3 tools/mixed_gpu/measure_q8k_wire.py --family q8k1 \
      --helper   build-mixed-gpu/repro-q81-cuda/strata-expert-helper \
      --reference build-mixed-gpu/repro-q81-hip/p2_measure_hip_reference \
      --layers 32-35 [--report-name measure.json] [--window-timeout 7200] \
      [--execute]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import finish_window as fw                                              # noqa: E402

WORK = fw.WORK
EV_ROOT = fw.EV_ROOT
CAP_SOURCE = EV_ROOT / "fin1/cap"          # the pooled verdict's own capture
PROD_MARKERS = ("pair_supervisor.py", "launch-prod-", "serve/server.py")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def production_absent() -> tuple[bool, str]:
    """Refuse to run beside ANY live production process. Nothing is ever
    signalled by this driver; this check only proves the precondition."""
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        joined = b" ".join(argv).decode("utf-8", "replace")
        if int(proc.name) == os.getpid():
            continue
        for marker in PROD_MARKERS:
            if marker in joined:
                return False, f"live production process pid={proc.name}: {joined[:120]}"
    return True, "no pair_supervisor / canonical launcher / server process alive"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Boundary-regression driver for the Q8_K expert-helper wire (default OFF)")
    ap.add_argument("--family", required=True,
                    help="evidence family dir under build-mixed-gpu/p2-finish (created fresh)")
    ap.add_argument("--helper", required=True, help="CANDIDATE strata-expert-helper binary")
    ap.add_argument("--reference", required=True,
                    help="CANDIDATE p2_measure_hip_reference harness binary")
    ap.add_argument("--layers", default="32-47")
    ap.add_argument("--report-name", default="measure.json")
    ap.add_argument("--window-timeout", type=int, default=7200)
    ap.add_argument("--execute", action="store_true",
                    help="actually run; without it only the verified plan prints")
    args = ap.parse_args()

    helper = Path(args.helper).resolve()
    reference = Path(args.reference).resolve()
    for binary in (helper, reference):
        if not binary.is_file() or not os.access(binary, os.X_OK):
            print(f"REFUSING: candidate binary missing: {binary}", file=sys.stderr)
            return 2
    fw.parse_layers(args.layers)

    ev = EV_ROOT / args.family
    capdir = ev / "cap"
    if ev.exists():
        print(f"REFUSING: family dir already exists: {ev}", file=sys.stderr)
        return 2
    complete, bad = fw.capture_complete(CAP_SOURCE, list(fw.fixture_texts()))
    if not complete:
        print(f"REFUSING: fin1 capture incomplete: {bad[:4]}", file=sys.stderr)
        return 2
    absent, why = production_absent()
    if not absent:
        print(f"REFUSING: {why}", file=sys.stderr)
        return 2

    # The three recorded substitutions; the argv/env builders stay single-sourced.
    stock_helper, stock_reference = fw.CUDA_HELPER, fw.HIP_REFERENCE
    fw.CUDA_HELPER, fw.HIP_REFERENCE = helper, reference

    print(f"family          : {ev.name}")
    print(f"capture         : {CAP_SOURCE} (hardlinked, read-only use)")
    print(f"layers          : {args.layers}")
    print(f"helper          : {helper}  sha256 {sha256_file(helper)[:16]}...")
    print(f"  (stock was    : {stock_helper})")
    print(f"reference       : {reference}  sha256 {sha256_file(reference)[:16]}...")
    print(f"  (stock was    : {stock_reference})")
    print(f"q8k opt-in env  : STRATA_EXPERT_HELPER_Q8K=1 (reference harness only)")
    print(f"production      : verified absent ({why}); no outage, no restore")
    if not args.execute:
        print("plan verified; pass --execute to run")
        return 0

    ev.mkdir(parents=True)
    shutil.copytree(CAP_SOURCE, capdir, copy_function=os.link)   # hardlinks: no bytes copied
    paths = fw.window_paths(ev, args.report_name)
    out_json, socket_path = paths["report"], paths["socket"]
    if out_json.exists() or socket_path.exists() or socket_path.is_symlink():
        print(f"REFUSING: existing report/socket in {ev}", file=sys.stderr)
        return 2

    result = dict(family=args.family, layers=args.layers, report=str(out_json),
                  helper=str(helper), helper_sha256=sha256_file(helper),
                  stock_helper=str(stock_helper),
                  reference=str(reference), reference_sha256=sha256_file(reference),
                  stock_reference=str(stock_reference),
                  q8k_env="STRATA_EXPERT_HELPER_Q8K=1 injected into the reference harness env",
                  production="already down by session permission; no outage performed, "
                             "nothing signalled, nothing restored",
                  reference_backend="gfx906-HIP")

    helper_proc = None
    helper_log = paths["helper_log"].open("ab")
    try:
        env_clean = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                     "HOME": os.environ.get("HOME", "/home/user"), "TERM": "dumb"}
        helper_proc = subprocess.Popen(fw.helper_argv(helper, socket_path), env=env_clean,
                                       stdout=helper_log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if socket_path.exists() and "listening" in paths["helper_log"].read_text(errors="replace"):
                break
            if helper_proc.poll() is not None:
                raise RuntimeError(f"helper died rc={helper_proc.returncode}")
            time.sleep(2)
        else:
            raise RuntimeError("helper socket/listening deadline expired")

        env_hip = fw.reference_env()
        env_hip["STRATA_EXPERT_HELPER_Q8K"] = "1"
        result["reference_env"] = {k: env_hip[k] for k in
                                   ("LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES",
                                    "HSA_OVERRIDE_GFX_VERSION", "HIP_FORCE_P2P",
                                    "HSA_ENABLE_SDMA", "STRATA_EXPERT_HELPER_Q8K")}
        with paths["reference_log"].open("wb") as rlog:
            ref = subprocess.run(fw.reference_argv(out_json, socket_path, capdir,
                                                   layers=args.layers),
                                 env=env_hip, stdout=rlog,
                                 stderr=subprocess.STDOUT, timeout=args.window_timeout)
        result["reference_rc"] = ref.returncode
        if ref.returncode != 0 or not out_json.exists():
            raise RuntimeError(f"reference harness rc={ref.returncode}")
        result["report_sha256"] = sha256_file(out_json)
        audit = subprocess.run([sys.executable, str(fw.AUDIT), str(out_json)],
                               capture_output=True, text=True, timeout=120)
        paths["audit_log"].write_bytes(audit.stdout.encode() + audit.stderr.encode())
        result["audit_rc"] = audit.returncode
        print(audit.stdout, flush=True)
    finally:
        helper_log.close()
        if helper_proc is not None and helper_proc.poll() is None:
            try:
                os.killpg(helper_proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                helper_proc.wait(timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(helper_proc.pid, signal.SIGKILL)
                helper_proc.wait(timeout=15)
        token = fw.pair.socket_token(socket_path) if socket_path.exists() else None
        if token is not None:
            fw.pair.release_sockets({socket_path: token})
        (ev / "measure-q8k-result.json").write_text(json.dumps(result, indent=1) + "\n",
                                                    encoding="utf-8")

    print(json.dumps({k: result[k] for k in ("reference_rc", "audit_rc",
                                             "report_sha256")}, indent=1))
    return 0 if result.get("audit_rc") == 0 and result.get("reference_rc") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Capture STRATA_DUMP_MOE_INPUT activations for the P2 measurement.

One engine process per (fixture, layer): the engine's dump fires once per
process on the first prefill chunk, so each pair needs a fresh wrapper+engine
with STRATA_DUMP_MOE_INPUT=<out>/<fixture>.<layer>.bin and
STRATA_DUMP_MOE_LAYER=<layer>, then ONE request carrying the fixture text.

Checkpointed and resumable: pairs recorded ok in <out>/checkpoint.tsv are
skipped on re-run. Fixture texts are extracted from p2_measure.cpp by the
same parser check_fixture_pins.py uses (no second source of truth).

Usage:
  python3 capture_window.py --out /tmp/agent/p2cap \
      --engine build-mixed-gpu/r4-dflash-after/bin/strata-hip \
      [--layers 32-47] [--fixtures id1,id2|all] [--port 8019]
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from check_fixture_pins import FIXTURES, c_string_literals  # noqa: E402

WRAPPER_PY = os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "/.venv/bin/python"
WRAPPER_CWD = os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + ""
TEMPLATE_EXE_ARGS = [
    "--gpu", "0,1",
    "--pack", os.environ.get("STRATA_AI_DIR", "/mnt/user/ai") + "/iq3xxs-15",
    "--native", os.environ.get("STRATA_AI_DIR", "/mnt/user/ai") + "/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf",
    "--native-dense-gguf", os.environ.get("STRATA_AI_DIR", "/mnt/user/ai") + "/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf",
    "--ple-gguf", os.environ.get("STRATA_AI_DIR", "/mnt/user/ai") + "/Qwen3.8-Flash-Next-PLE-Q8_0.gguf",
    "--expert-profile", os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "/data/expert-profile.bin",
    "--expert-cache", "auto", "--prefill", "8192", "--spec", "5",
    "--spec-min-p", "0.5", "--max-context", "262144", "--kv", "int8",
    "--vram-reserve-mib", "896", "--vram-reserve-later-mib", "1000",
    "--kv-resident", "262144", "--pcie-frac", "0", "--expert-cache-cpu-order",
    "--prompt-cache-spread", "--conversation-cache-mib", "2048",
    "--conversation-cache-min-tokens", "2048",
    "--conversation-cache-park-gain", "4096",
    "--dflash", os.environ.get("STRATA_DFLASH2_DIR", "/mnt/user/dflash2") + "/artifacts/inco-dflash2-q4_0.gguf",
    "--dflash-block", "5", "--suffix-draft", "0",
]
TEMPLATE_ENV = {
    "CUDA_VISIBLE_DEVICES": "0,1",
    "HIP_FORCE_P2P": "1",
    "HSA_ENABLE_SDMA": "1",
    "HSA_OVERRIDE_GFX_VERSION": "9.0.6",
    "LOOKAHEAD_STATS": "1",
    "STRATA_DECODE_TIMING": "1",
    "STRATA_DFLASH_SAMPLED": "1",
    "STRATA_ENGINE_NICE": "0",
    "STRATA_GFX906_TOPK_FIT17": "1",
    "STRATA_GFX906_TOPK_REG": "2",
    "STRATA_HIP_PROMPT_F16": "1",
    "STRATA_NO_PCIE_GROUP": "1",
    "STRATA_PRIMARY_COMMIT_OVERLAP": "1",
    "STRATA_VERIFY_PROFILE": "1",
    "STRATA_WATCHDOG_S": "300",
}


def fixture_texts(cpp_path):
    src = open(cpp_path, encoding="utf-8").read()
    out = {}
    for fid, sym in FIXTURES:
        m = re.search(r"\bconst char\* const %s\s*=" % sym, src)
        if not m:
            sys.exit("fixture constant %s not found in %s" % (sym, cpp_path))
        lits, _ = c_string_literals(src, src.index('"', m.end()))
        out[fid] = "".join(lits)
    return out


def parse_layers(spec):
    lo, hi = spec.split("-")
    return list(range(int(lo), int(hi) + 1))


def load_checkpoint(path):
    done = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 5 and parts[4] == "ok":
                done[(parts[0], int(parts[1]))] = parts[3]
    return done


def append_checkpoint(path, fixture, layer, nbytes, sha, status):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("%s\t%d\t%d\t%s\t%s\n" % (fixture, layer, nbytes, sha, status))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def health_ok(proc, port, deadline):
    url = "http://127.0.0.1:%d/health" % port
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if json.loads(r.read().decode()).get("loaded"):
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if proc.poll() is not None:
            return False
        time.sleep(3)
    return False


def one_pair(fixture, layer, text, args, cfgdir, logdir):
    dump = os.path.join(args.out, "%s.%d.bin" % (fixture, layer))
    if os.path.exists(dump):
        os.remove(dump)
    cfg = os.path.join(cfgdir, "cfg-%s-%d.json" % (fixture, layer))
    log = os.path.join(logdir, "engine-%s-%d.log" % (fixture, layer))
    env = dict(TEMPLATE_ENV)
    env["STRATA_DUMP_MOE_INPUT"] = dump
    env["STRATA_DUMP_MOE_LAYER"] = str(layer)
    config = {
        "args": TEMPLATE_EXE_ARGS, "backend": "hip", "cwd": WRAPPER_CWD,
        "env": env, "env_to_user": True, "exe": os.path.abspath(args.engine),
        "fit_max_tokens": True, "gpu": [0, 1], "host": "127.0.0.1",
        "layer_split": "32",
        "lib_dirs": [os.environ.get("STRATA_ROCM_ROOT", "/home/user/rocm-gfx906-xnack") + "/lib", "/opt/rocm-6.1.0/lib"],
        "log": log, "mcp_servers": {}, "model_name": "swift-1.5-iq3_xxs",
        "port": args.port, "reasoning_budget_tokens": 55000,
        "reasoning_effort": "high",
        "sampling": {"min_p": 0.0, "presence_penalty": 0.0,
                     "repetition_penalty": 1.0, "temperature": 1.0,
                     "top_k": 20, "top_p": 0.95},
        "tokenizer": os.environ.get("STRATA_AI_DIR", "/mnt/user/ai") + "/iq3xxs-15/tokenizer",
    }
    with open(cfg, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=1)
    with open(log, "wb") as elog:
        proc = subprocess.Popen(
            [WRAPPER_PY, "serve/server.py", "--engine", "strata",
             "--config", cfg, "--port", str(args.port)],
            cwd=WRAPPER_CWD, stdout=elog, stderr=subprocess.STDOUT,
            start_new_session=True)
        try:
            if not health_ok(proc, args.port, time.time() + args.load_timeout):
                return 0, "", "engine-not-ready rc=%s" % proc.poll()
            body = json.dumps({"messages": [{"role": "user", "content": text}],
                               "max_tokens": 8, "temperature": 0}).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:%d/v1/chat/completions" % port_of(args),
                data=body, headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=args.req_timeout).read()
            except urllib.error.HTTPError as exc:
                return 0, "", "request-http-%d" % exc.code
        finally:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=90)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=30)
    if not os.path.exists(dump) or os.path.getsize(dump) == 0:
        return 0, "", "no-dump"
    return os.path.getsize(dump), sha256_file(dump), "ok"


def port_of(args):
    return args.port


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine", required=True)
    ap.add_argument("--layers", default="32-47")
    ap.add_argument("--fixtures", default="all")
    ap.add_argument("--port", type=int, default=8019)
    ap.add_argument("--load-timeout", type=int, default=600)
    ap.add_argument("--req-timeout", type=int, default=600)
    args = ap.parse_args()

    texts = fixture_texts(os.path.join(HERE, "p2_measure.cpp"))
    ids = list(texts) if args.fixtures == "all" else args.fixtures.split(",")
    layers = parse_layers(args.layers)
    os.makedirs(args.out, exist_ok=True)
    cfgdir = os.path.join(args.out, "cfg")
    logdir = os.path.join(args.out, "engine-logs")
    os.makedirs(cfgdir, exist_ok=True)
    os.makedirs(logdir, exist_ok=True)
    ckpt = os.path.join(args.out, "checkpoint.tsv")
    done = load_checkpoint(ckpt)
    pairs = [(f, l) for f in ids for l in layers]
    todo = [p for p in pairs if p not in done]
    print("pairs total=%d done=%d todo=%d" % (len(pairs), len(done), len(todo)), flush=True)
    t0 = time.time()
    fails = 0
    for i, (fixture, layer) in enumerate(todo):
        if i and i % 4 == 0:
            print("progress %d/%d elapsed=%.0fs" % (i, len(todo), time.time() - t0), flush=True)
        nbytes, sha, status = one_pair(fixture, layer, texts[fixture], args, cfgdir, logdir)
        append_checkpoint(ckpt, fixture, layer, nbytes, sha, status)
        if status != "ok":
            fails += 1
            print("FAIL %s/%d: %s" % (fixture, layer, status), flush=True)
    print("capture complete: ok=%d fail=%d elapsed=%.0fs"
          % (len(todo) - fails, fails, time.time() - t0), flush=True)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

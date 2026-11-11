#!/usr/bin/env python3
"""One serve arm of the disk-cache A/B gate (gfx906 methodology: pinned config,
greedy parity probe FIRST on a fresh boot, one fresh PP judge, cached TG band).

Reuses the harness lifecycle (helper + engine + RSS sampling) from
tools/conversation_disk_cache_check.py.  The ONLY variables between arms are
the binary and the cache flags carried by the per-arm config JSON.

Usage:  python3 run/bench_serve_arm.py --config run/cfg-bench-base.json \
            --tag A [--restore-check] [--port 8012]
Output: run/logs/bench-<tag>.json plus judge lines on stdout.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
from conversation_disk_cache_check import Harness, big_message, http  # noqa: E402


def probe(port: int, seed: str, max_tokens: int) -> dict:
    """Greedy thinking-ON parity probe (temp 0, bounded effort): must be identical
    across arms on a fresh boot; any hash difference is a correctness failure."""
    body = {
        "messages": [{"role": "user",
                      "content": big_message(1500, seed) + "\nExplain briefly why the sky is blue, then count from 1 to 20."}],
        "max_tokens": max_tokens, "temperature": 0.0, "stream": False,
        "chat_template_kwargs": {"enable_thinking": True},
    }
    r = http(port, "POST", "/v1/chat/completions", body)
    msg = r["choices"][0]["message"]
    text = (msg.get("reasoning_content") or "") + "\x1f" + (msg.get("content") or "")
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "chars": len(text),
            "completion": r.get("usage", {}).get("completion_tokens")}


def arm(config: Path, tag: str, port: int, restore_check: bool) -> dict:
    h = Harness(config, port, f"bench-{tag}")
    out: dict = {"tag": tag, "config": str(config)}
    h.start()
    try:
        out["probe"] = probe(port, "probe", 512)   # FIRST request on a fresh boot

        # PP judge: one fresh ~16k-token conversation (mid regime; recorded in the scope)
        pp = [{"role": "user", "content": big_message(16000, "ppj") + "\nReply with the single word OK."}]
        log0 = len(h.engine_log())
        h.chat(pp, 2, "PP-judge")
        out["pp_line"] = (re.findall(r"prompt \d+ tokens = [^\n]*", h.engine_log()[log0:]) or [""])[0]

        # TG band: cached continuations of the SAME conversation (steady state)
        conv = list(pp)
        tg_lines, acc_lines = [], []
        for i in range(6):
            m = list(conv) + [{"role": "user", "content": f"Count from {i * 10 + 1} to {i * 10 + 8}."}]
            log0 = len(h.engine_log())
            r = h.chat(m, 64, f"TG{i}")
            new = h.engine_log()[log0:]
            tg_lines += re.findall(r"generated in [\d.]+ ms \([\d.]+ tok/s\)", new)
            acc_lines += re.findall(r"drafts accepted \d+ of \d+", new)
            conv = m + [{"role": "assistant", "content": r["text"]}]
        out["tg_lines"], out["acceptance"] = tg_lines, acc_lines

        # Token-level cache quality (all arms): conv C greedy 64; D displaces it (parks C to
        # the arm's own tier: RAM in A/B1, disk in B2); resend C -> restore -> continue greedy 64.
        # c1/c2 hashes must match across ALL arms - the restore path may not change tokens.
        C = [{"role": "user", "content": big_message(1500, "restore") + "\nCount from 1 to 40."}]
        D = [{"role": "user", "content": big_message(1500, "otherx") + "\nReply with the single word OK."}]
        c1 = h.chat(C, 64, "C1")
        h.chat(D, 2, "D-displace")
        log0 = len(h.engine_log())
        c2 = h.chat(C + [{"role": "assistant", "content": c1["text"]},
                         {"role": "user", "content": "Now count from 41 to 80."}], 64, "C2-after-restore")
        out["restore"] = {
            "c1_sha256": hashlib.sha256(c1["text"].encode()).hexdigest(),
            "c2_sha256": hashlib.sha256(c2["text"].encode()).hexdigest(),
            "engine": re.findall(r"conversation cache[^\n]*", h.engine_log()[log0:])[-6:],
        }
        out["rss_peaks"] = {k: h.rss.peak(k) >> 20 for k in ("VmRSS", "VmHWM", "Pss", "Anon")}
    finally:
        h.stop()
    vram = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"],
                          capture_output=True, text=True).stdout.strip().splitlines()
    out["vram_after"] = vram
    (REPO / "run" / "logs" / f"bench-{tag}.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1)[:3000], flush=True)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--port", type=int, default=8012)
    ap.add_argument("--restore-check", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    arm(a.config, a.tag, a.port, a.restore_check)
    print(f"[bench] arm {a.tag} done in {time.time() - t0:.0f}s", flush=True)

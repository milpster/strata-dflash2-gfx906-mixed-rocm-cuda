#!/usr/bin/env python3
"""S1/S2 GPU-window scenarios for the disk-KV churn fix.

S1 (churn): ONE conversation, N sequential turns of ~`append` new tokens each.
  - pre-fix engine: every turn restores its own just-published image from disk.
  - fixed engine: zero restores; publishes gated by --conversation-cache-park-gain.
S2 (gain cadence): one turn appending `burst` tokens -> publishes ~ burst/4096.

Usage: python3 run/churn_check.py --config run/cfg-churn-pre.json --tag PRE \
          [--depth 16000] [--turns 14] [--append 300] [--burst 12000] [--port 8011]
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
from conversation_disk_cache_check import Harness, big_message  # noqa: E402

PATTERNS = {
    "restores": r"conversation cache: restored (\d+) tokens from disk[^\n]*",
    "parks": r"conversation cache: disk park streamed (\d+) tokens[^\n]*",
    "gain_skips": r"idle save skipped \(park-gain[^\n]*",
    "dcache_writes": r"DCACHE write (\d+) (\d+)[^\n]*",
}


def drive(tag: str, cfg: Path, port: int, depth: int, turns: int, append: int, burst: int) -> dict:
    h = Harness(cfg, port, REPO / "run" / "logs" / f"churn-{tag}.engine.log")
    out = {"tag": tag, "depth": depth, "turns": turns, "append": append, "burst": burst, "requests": []}
    h.start()
    try:
        messages = [{"role": "user", "content": big_message(depth, "alpha") + "\nReply with the single word OK."}]
        log0 = len(h.engine_log())
        t0 = h.chat(messages, 12, "turn0")
        out["turn0_sha"] = hashlib.sha256((str(t0["prompt_tokens"]) + "|" + t0["text"]).encode()).hexdigest()[:16]
        for i in range(1, turns + 1):
            messages += [{"role": "assistant", "content": "OK."},
                         {"role": "user", "content": big_message(append, f"t{i}") + "\nReply with the single word OK."}]
            mark = len(h.engine_log())
            r = h.chat(messages, 12, f"turn{i}")
            seg = h.engine_log()[mark:]
            out["requests"].append({
                "turn": i, "seconds": r["seconds"], "prompt_tokens": r["prompt_tokens"],
                "restores": len(re.findall(PATTERNS["restores"], seg)),
                "parks": len(re.findall(PATTERNS["parks"], seg)),
                "gain_skips": len(re.findall(PATTERNS["gain_skips"], seg)),
            })
        s1_log = h.engine_log()[log0:]
        out["s1"] = {k: len(re.findall(p, s1_log)) for k, p in PATTERNS.items()}
        out["s1_restore_lines"] = re.findall(r"restored (\d+) tokens from disk \(conv-\d+\.sess\) in ([0-9.]+) ms", s1_log)[:6]
        # S2: one big appended turn; count fresh publishes in its stretch
        mark = len(h.engine_log())
        messages += [{"role": "assistant", "content": "OK."},
                     {"role": "user", "content": big_message(burst, "burst") + "\nReply with the single word OK."}]
        r = h.chat(messages, 12, "burst")
        seg = h.engine_log()[mark:]
        out["s2"] = {"seconds": r["seconds"], "prompt_tokens": r["prompt_tokens"],
                     "restores": len(re.findall(PATTERNS["restores"], seg)),
                     "parks": len(re.findall(PATTERNS["parks"], seg)),
                     "gain_skips": len(re.findall(PATTERNS["gain_skips"], seg))}
        out["rss_hwm_mib"] = h.rss.peak("VmHWM") >> 20
        out["parity_text_turn0"] = t0["text"]
    finally:
        h.stop()
    Path(REPO / "run" / "logs" / f"churn-{tag}.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--depth", type=int, default=16000)
    ap.add_argument("--turns", type=int, default=14)
    ap.add_argument("--append", type=int, default=300)
    ap.add_argument("--burst", type=int, default=12000)
    a = ap.parse_args()
    res = drive(a.tag, a.config, a.port, a.depth, a.turns, a.append, a.burst)
    per = [(r["turn"], r["seconds"], r["restores"], r["parks"]) for r in res["requests"]]
    print(json.dumps({"turn,sec,restores,parks": per, "s1": res["s1"], "s2": res["s2"],
                      "turn0_sha": res.get("turn0_sha"), "rss_hwm_mib": res.get("rss_hwm_mib")}, indent=1))

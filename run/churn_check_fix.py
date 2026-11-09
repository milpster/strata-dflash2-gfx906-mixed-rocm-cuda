#!/usr/bin/env python3
"""FIX-arm churn scenarios (v2, tokenizer-corrected sizes).

Phase A (parity): turn 0 with the SAME depth-16000 prompt as the PRE arm -> sha must match PRE.
Phase B (churn + gain): a FRESH conversation, depth 4000 words (~32k tok), N turns of ~250
words (~2k tok, below the 4096 gain), then a burst turn (~16k tok, above the gain).

Usage: python3 run/churn_check_fix.py --config run/cfg-churn-fix.json --tag FIX [--port 8011]
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
}


def counts(seg: str) -> dict:
    return {k: len(re.findall(p, seg)) for k, p in PATTERNS.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--tag", default="FIX")
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--depth", type=int, default=4000)
    ap.add_argument("--turns", type=int, default=10)
    ap.add_argument("--append", type=int, default=250)
    ap.add_argument("--burst", type=int, default=2000)
    a = ap.parse_args()

    h = Harness(a.config, a.port, REPO / "run" / "logs" / f"churn-{a.tag}.engine.log")
    out: dict = {"tag": a.tag, "phaseA": {}, "phaseB": {"requests": []}, "burst": {}}
    h.start()
    try:
        # Phase A: parity vs the PRE arm's turn 0 (identical prompt)
        mark = len(h.engine_log())
        t0 = h.chat([{"role": "user", "content": big_message(16000, "alpha") + "\nReply with the single word OK."}], 12, "A-turn0")
        out["phaseA"] = {
            "turn0_sha": hashlib.sha256((str(t0["prompt_tokens"]) + "|" + t0["text"]).encode()).hexdigest()[:16],
            "seconds": t0["seconds"], "prompt_tokens": t0["prompt_tokens"],
            "log": counts(h.engine_log()[mark:]),
        }
        # Phase B: fresh conversation, sequential small turns
        messages = [{"role": "user", "content": big_message(a.depth, "bravo") + "\nReply with the single word OK."}]
        mark = len(h.engine_log())
        b0 = h.chat(messages, 12, "B-turn0")
        out["phaseB"]["turn0"] = {"seconds": b0["seconds"], "prompt_tokens": b0["prompt_tokens"], "log": counts(h.engine_log()[mark:])}
        for i in range(1, a.turns + 1):
            messages += [{"role": "assistant", "content": "OK."},
                         {"role": "user", "content": big_message(a.append, f"t{i}") + "\nReply with the single word OK."}]
            mark = len(h.engine_log())
            r = h.chat(messages, 12, f"B-turn{i}")
            out["phaseB"]["requests"].append({"turn": i, "seconds": r["seconds"],
                                              "prompt_tokens": r["prompt_tokens"],
                                              **counts(h.engine_log()[mark:])})
        mark = len(h.engine_log())
        messages += [{"role": "assistant", "content": "OK."},
                     {"role": "user", "content": big_message(a.burst, "burst") + "\nReply with the single word OK."}]
        r = h.chat(messages, 12, "burst")
        out["burst"] = {"seconds": r["seconds"], "prompt_tokens": r["prompt_tokens"], **counts(h.engine_log()[mark:])}
        out["rss_hwm_mib"] = h.rss.peak("VmHWM") >> 20
    finally:
        h.stop()
    Path(REPO / "run" / "logs" / f"churn-{a.tag}.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()

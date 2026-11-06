#!/usr/bin/env python3
"""Short-prompt prober for the T-lane: fixed deterministic prompts, unique marker per
request (so the conversation/prompt cache cannot serve a repeat from cache), tiny
max_tokens so the wall is dominated by the prompt read.

Per arm: sizes [~214, ~1k, ~4k, ~18k] x 3 rounds (fresh content each round), then one
TG leg (1k prompt, 128 generated). Records client walls; the engine log's own
`DONE ... <prompt ms> <decode ms>` and `reading the prompt ... Ns` lines are the
server-side truth (parsed by the runner afterwards).
"""
import json
import time
import urllib.request

URL = "http://127.0.0.1:8019/v1/chat/completions"
PARA = (
    "The engine keeps a window tier of expert rows on the first socket and a prompt tier on the "
    "second socket; each channel fails on its own and the other keeps serving. Measurements are "
    "taken with fixed text on the same build, one change at a time, and every number is named "
    "with the card it ran on. "
)

SIZES = [("S214", 2), ("S1k", 9), ("S4k", 36), ("S18k", 160)]  # (tag, paragraph count)

# STRATA_PROBE_LEGS="S1k" (comma-separated tags; "TG" gates the TG leg): run only those legs.
# Default/empty = every leg = byte-identical behavior. The A/B discriminator windows use S1k-only
# mini arms: the fast (6.2-6.5 s) and regressed (8.6-29 s) S1k ranges are disjoint.
import os
_want = [t.strip() for t in os.environ.get("STRATA_PROBE_LEGS", "").split(",") if t.strip()]
SIZES = [s for s in SIZES if not _want or s[0] in _want]


def build(tag, paras, marker):
    return f"[{tag}#{marker}] " + (PARA * paras)


def ask(prompt, max_tokens):
    body = json.dumps({
        "model": "probe",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
    }).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        json.loads(r.read())
    return time.time() - t0


def main():
    out = open(os.environ.get("STRATA_DEV_TREE", "/home/user/dev/Strata") + "/bench/ab-v0210/shortpp/out/probe.jsonl", "a", buffering=1)
    # warm-up round (first-request cost is measured separately): one tiny throwaway
    t = ask("warm-up ping, reply with ok.", 4)
    out.write(json.dumps({"tag": "warmup", "wall_s": round(t, 3)}) + "\n")
    print(f"warmup {t:.2f}s", flush=True)
    for tag, paras in SIZES:
        for rnd in range(3):
            p = build(tag, paras, f"{tag}-r{rnd}")
            t = ask(p, 8)
            rec = {"tag": tag, "round": rnd, "chars": len(p), "wall_s": round(t, 3)}
            out.write(json.dumps(rec) + "\n")
            print(f"{tag} r{rnd}: {t:.2f}s", flush=True)
    t = ask(build("TG", 9, "tg-leg"), 128)
    out.write(json.dumps({"tag": "TG128", "wall_s": round(t, 3)}) + "\n")
    print(f"TG128 {t:.2f}s", flush=True)


if __name__ == "__main__":
    main()

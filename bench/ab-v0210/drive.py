#!/usr/bin/env python3
"""Drive one bench round against a booted arm on 127.0.0.1:8019.

Sequence per the box's A/B discipline:
  1. parity probe   - FIRST request on the fresh boot: temperature 0, thinking on,
                      effort low, bounded to 512 tokens. sha256 of the text.
  2. pp16384        - fresh long prompt (cache-busted by round) -> judge the engine's
                      'prompt N tokens = 0 reused + N read in X (PP tok/s)' line.
  3. tg1024 xN      - identical request repeated; judge cached-rerun
                      'generated in X ms (R tok/s)' lines (steady state).
  4. infill80k      - ~80k-token deep continuation; fresh prefill line judges it.

All requests temperature 0 (greedy) so arms are comparable token-for-token.
Writes out/<arm>-r<round>.json and prints a compact summary."""
import hashlib
import json
import sys
import time
import urllib.request

URL = "http://127.0.0.1:8019/v1/chat/completions"
ARM, ROUND = sys.argv[1], sys.argv[2]
TG_RUNS = int(sys.argv[3]) if len(sys.argv) > 3 else 3
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from corpus import PROBE_PROMPT, TG_PROMPT, PP_CHARS, INFILL_CHARS, infill_task, corpus

B = __file__.rsplit("/", 1)[0]
LOG = open(f"{B}/logs/{ARM}-r{ROUND}-drive.log", "w", buffering=1)


def content_text(out):
    c = out.get("choices", [{}])[0].get("message", {}).get("content", "")
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c if isinstance(c, str) else ""


def chat(messages, max_tokens, effort="low", tag=""):
    body = {
        "model": "swift-1.5-iq3_xxs",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "reasoning_effort": effort,
        "stream": False,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(URL, data=data, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=3600) as r:
        out = json.loads(r.read())
    wall = time.time() - t0
    text = content_text(out)
    u = out.get("usage", {})
    print(f"[{ARM} r{ROUND}] {tag}: wall {wall:.1f}s  prompt_tokens {u.get('prompt_tokens')}  "
          f"completion_tokens {u.get('completion_tokens')}  sha {hashlib.sha256(text.encode()).hexdigest()[:16]}", flush=True)
    LOG.write(json.dumps({"tag": tag, "wall": wall, "usage": u,
                          "sha": hashlib.sha256(text.encode()).hexdigest(), "text": text}) + "\n")
    return {"tag": tag, "wall": wall, "usage": u, "text": text}


res = {}
print(f"[{ARM} r{ROUND}] probe (temp 0, effort low, 512) - FIRST request on fresh boot", flush=True)
res["probe"] = chat([{"role": "user", "content": PROBE_PROMPT}], 512, effort="low", tag="probe")

pp_prompt = f"(round {ROUND}) Summarize the following document in one sentence.\n\n" + corpus(PP_CHARS)
res["pp16384"] = chat([{"role": "user", "content": pp_prompt}], 16, effort="low", tag="pp16384")

res["tg1024"] = []
for i in range(TG_RUNS):
    res["tg1024"].append(chat([{"role": "user", "content": TG_PROMPT}], 1024, effort="high", tag=f"tg1024.{i}"))

res["infill80k"] = chat([{"role": "user", "content": infill_task(INFILL_CHARS)}], 256, effort="low", tag="infill80k")

json.dump(res, open(f"{B}/out/{ARM}-r{ROUND}.json", "w"), indent=1)
print(f"[{ARM} r{ROUND}] done -> out/{ARM}-r{ROUND}.json", flush=True)

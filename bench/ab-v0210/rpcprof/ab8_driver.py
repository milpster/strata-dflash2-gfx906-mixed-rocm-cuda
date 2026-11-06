#!/usr/bin/env python3
"""ab8 speed driver: replicates the user's real traffic shape - one ~25k-token COLD
prompt (the stall driver), then a warm follow-up on the same conversation. Prints
end-to-end walls; the engine's own serve lines (in each lineage's log) give the
internal read/gen split for the same requests."""
import json
import sys
import time
import urllib.request

URL = "http://127.0.0.1:8009/v1/chat/completions"
PARA = ("The router reviewed the measured counts, the release notes and the two bench "
        "reports before it let the candidate leave the bench: every number carried its "
        "hardware, every claim carried its measurement, and the default path stayed "
        "byte-identical to the last release.\n")
BIG = PARA * 640          # ~25k tokens class, deterministic


def call(messages, max_tokens):
    body = json.dumps({"messages": messages, "max_tokens": max_tokens,
                       "temperature": 0}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        out = json.load(r)
    return time.time() - t0, out


phase = sys.argv[1] if len(sys.argv) > 1 else "run"
t, r = call([{"role": "user", "content": BIG + "\nReply with exactly one word: READY"}], 64)
u = r["usage"]
print(f"[{phase}] cold-25k: wall={t:.1f}s prompt_tokens={u['prompt_tokens']} "
      f"completion={u['completion_tokens']} cached={u.get('prompt_tokens_details', {}).get('cached_tokens', 0)}",
      flush=True)

msgs = [{"role": "user", "content": BIG + "\nReply with exactly one word: READY"},
        {"role": "assistant", "content": "READY"},
        {"role": "user", "content": "Now reply with exactly one word: WARM"}]
t, r = call(msgs, 64)
u = r["usage"]
print(f"[{phase}] warm-followup: wall={t:.1f}s prompt_tokens={u['prompt_tokens']} "
      f"completion={u['completion_tokens']} cached={u.get('prompt_tokens_details', {}).get('cached_tokens', 0)}",
      flush=True)

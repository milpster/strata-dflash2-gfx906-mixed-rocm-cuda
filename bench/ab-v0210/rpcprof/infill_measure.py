#!/usr/bin/env python3
"""Infill/decode measurement for a running Strata server (SSE streaming).

Records time-to-first-token (prefill), then decode throughput, and the server's
own usage counts at the end. Prints progress every PROGRESS_S seconds so a long
window is visible in the terminal.

Usage: infill_measure.py <url> <prompt_file> <max_tokens> <out_prefix>
Writes <out_prefix>.txt (the reply text) and <out_prefix>.json (the numbers).
"""
import json
import sys
import time
import urllib.request

url, prompt_file, max_tokens, out_prefix = sys.argv[1:5]
max_tokens = int(max_tokens)
prompt = open(prompt_file, encoding="utf-8").read()
body = json.dumps({
    "model": "swift-1.5-ud-iq3xxs-managed",
    "messages": [{"role": "user", "content": prompt}],
    "max_tokens": max_tokens,
    "temperature": 0,
    "stream": True,
    "stream_options": {"include_usage": True},
}).encode()

req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=body,
                             headers={"Content-Type": "application/json"})
t0 = time.time()
first = None
chunks = 0
text = []
usage = {}
last_print = t0
try:
    with urllib.request.urlopen(req, timeout=7200) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if ev.get("usage"):
                usage = ev["usage"]
            for ch in ev.get("choices", []):
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    if first is None:
                        first = time.time()
                        print(f"TTFT {first - t0:.2f}s (prefill of {len(prompt)} chars)", flush=True)
                    chunks += 1
                    text.append(piece)
            now = time.time()
            if now - last_print >= 10:
                last_print = now
                el = now - (first or t0)
                print(f"  {now - t0:7.1f}s chunks={chunks} decode≈{chunks / el if first else 0:.2f} tok/s", flush=True)
except Exception as e:  # noqa: BLE001 - report, do not hide
    print(f"REQUEST FAILED after {time.time() - t0:.1f}s: {type(e).__name__}: {e}", flush=True)

end = time.time()
pt = usage.get("prompt_tokens")
ct = usage.get("completion_tokens")
prefill_s = (first - t0) if first else None
decode_s = (end - first) if first else None
# The client's decode figure is only honest when the deltas arrive spread out. A server
# that batches the answer (or streams only thinking first) delivers every content delta in
# a burst, so (end - first_content) is a fraction of the real generation time: the 80k leg
# on 2026-11-20 produced "1009 tok/s" that way, against the engine's own 20.2 tok/s. Below
# the thresholds the field is reported as null with a flag, and the engine's own line
# ("N generated in M ms") is the number to use.
decode_reliable = bool(chunks >= 32 and decode_s and decode_s >= 5.0)
res = {
    "wall_s": round(end - t0, 2),
    "ttft_s": round(prefill_s, 2) if prefill_s else None,
    "prompt_tokens": pt,
    "completion_tokens": ct,
    "prefill_tok_s": round(pt / prefill_s, 2) if pt and prefill_s else None,
    "prefill_ms_per_token": round(1000 * prefill_s / pt, 2) if pt and prefill_s else None,
    "decode_tok_s": round(ct / decode_s, 2) if (decode_reliable and ct) else None,
    "decode_tok_s_client_unreliable": (not decode_reliable) if first else None,
    "decode_window_s": round(decode_s, 2) if decode_s else None,
    "stream_chunks": chunks,
}
open(out_prefix + ".txt", "w", encoding="utf-8").write("".join(text))
json.dump(res, open(out_prefix + ".json", "w"), indent=2)
print(json.dumps(res, indent=2), flush=True)
if decode_reliable is False and first:
    print("NOTE: deltas arrived in one burst; client decode_tok_s withheld - read the engine's "
          "\"N generated in M ms\" line in the serve log.", flush=True)

#!/usr/bin/env python3
"""Promotion traffic check: one chat request that must reply with the exact
token PROMO-OK. Handles reasoning models (content may be null while the text
lands in reasoning_content) and prints an explicit PASS/FAIL."""
import json
import sys
import urllib.request

URL = "http://127.0.0.1:8009/v1/chat/completions"
body = json.dumps({
    "messages": [{"role": "user", "content": "Reply with exactly one word: PROMO-OK"}],
    "max_tokens": 64,
    "temperature": 0,
    "reasoning_effort": "low",
}).encode()
req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        out = json.load(r)
except Exception as e:
    print("TRAFFIC TEST FAILED:", e)
    sys.exit(1)
m = out["choices"][0]["message"]
c = m.get("content") or ""
rc = m.get("reasoning_content") or ""
txt = (c + " " + rc).strip()
print("content:", repr(c[:60]), "| reasoning tail:", repr(rc[-40:]) if rc else "(none)")
print("completion_tokens:", out["usage"]["completion_tokens"])
print("PROMO-OK check:", "PASS" if "PROMO-OK" in txt else "FAIL")
sys.exit(0 if "PROMO-OK" in txt else 1)

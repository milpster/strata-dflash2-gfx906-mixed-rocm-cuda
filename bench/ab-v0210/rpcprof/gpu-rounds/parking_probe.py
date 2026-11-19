#!/usr/bin/env python3
"""Conversation-parking validation probe (plan: parking-validation-plan.md).

Sends chat-completion turns for one conversation to the local prod server
(127.0.0.1:8009, no key), keeps the conversation in <evidence>/conversation.json,
and records every turn's wall time and reply so park/restore behavior can be
judged from the engine log next to it.

Usage:
  parking_probe.py --evidence DIR --prompt-file F --label L          # turn 1 (fresh)
  parking_probe.py --evidence DIR --label L --continue              # next turn
  parking_probe.py --evidence DIR --label L --continue --followup TXT
"""
import argparse
import json
import time
import urllib.request
import sys
from pathlib import Path

URL = "http://127.0.0.1:8009/v1/chat/completions"
SAMPLING = {  # frozen prod pins (request-template.json of the reserve panel)
    "model": "ud-iq3_xxs",
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 0.0,
    "repetition_penalty": 1.0,
    "reasoning_effort": "high",
    "stream": False,
}
DEFAULT_FOLLOWUP = (
    "In two sentences, summarize the most important point of everything "
    "said so far in this conversation."
)
MAX_TOKENS = 256


def post(body: dict, timeout: int) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        URL, data=data, headers={"Content-Type": "application/json"}
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    return {"wall_s": round(time.monotonic() - t0, 3), "reply": out}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--prompt-file")
    ap.add_argument("--label", required=True)
    ap.add_argument("--continue", dest="cont", action="store_true")
    ap.add_argument("--followup", default=DEFAULT_FOLLOWUP)
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    ev = Path(args.evidence)
    ev.mkdir(parents=True, exist_ok=True)
    state_p = ev / "conversation.json"
    log_p = ev / "turns.jsonl"

    if args.cont:
        if not state_p.exists():
            print("no conversation to continue", file=sys.stderr)
            return 2
        state = json.loads(state_p.read_text())
        state["messages"].append({"role": "user", "content": args.followup})
    else:
        if not args.prompt_file:
            print("--prompt-file required for the first turn", file=sys.stderr)
            return 2
        state = {
            "label": args.label,
            "messages": [
                {"role": "user", "content": Path(args.prompt_file).read_text()}
            ],
        }

    body = dict(SAMPLING)
    body["max_tokens"] = args.max_tokens
    body["messages"] = state["messages"]
    res = post(body, args.timeout)
    reply_txt = res["reply"]["choices"][0]["message"]
    content = reply_txt.get("content") or ""
    usage = res["reply"].get("usage", {})

    state["messages"].append({"role": "assistant", "content": content})
    state_p.write_text(json.dumps(state))
    rec = {
        "label": args.label,
        "turn": len([m for m in state["messages"] if m["role"] == "user"]),
        "n_messages": len(state["messages"]),
        "wall_s": res["wall_s"],
        "usage": usage,
        "chars_out": len(content),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with log_p.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec, indent=None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

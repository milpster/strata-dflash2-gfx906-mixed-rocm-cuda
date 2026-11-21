#!/usr/bin/env python3
"""multiturn-stress.py - the shape that actually killed prod, in one script.

A single cold request of the death size survived on BOTH builds on 2026-11-21, so the
single-request shape is not what broke the pool. The real deaths came from a live lane:
a conversation that grows turn by turn, whose prefix the engine restores from disk
(measured: `conversation cache: restored 42299 tokens from disk ... 13.9 s`), that parks
between turns, and that asks for a long answer each time.

This script keeps one conversation, appends every answer plus a fresh question, and
optionally idles between turns so the engine parks the conversation and has to restore it.
It reports, per turn, the prompt size, the reused prefix, the generated count and the speed,
and it stops at the first error with the server's own message.

Usage:
    multiturn-stress.py [--url ...] [--model NAME] [--turns 8]
                        [--seed-tokens 55000] [--max-tokens 900] [--idle 20] [--seed 11]

Exit status: 0 = every turn answered, 1 = a turn returned an error, 2 = the connection dropped.
"""
import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request

WORDS = (
    "budget pool lease expert cache resident staging decode prefill window margin slot "
    "shard layer route tensor kernel stream memory region page table fence event queue "
    "worker thread lock mutex atomic counter sample metric ledger token context prompt "
    "answer batch graph allocation release admission eviction pin transport socket helper"
).split()


def block(target_tokens: int, seed: int) -> str:
    rng = random.Random(seed)
    out, size = [], 0
    while size < target_tokens * 4:
        line = " ".join(rng.choice(WORDS) for _ in range(rng.randint(10, 18)))
        out.append("%s %d." % (line, rng.randint(1, 9999)))
        size += len(out[-1]) + 1
    return "\n".join(out)


def ask(url: str, model: str, messages, max_tokens: int, timeout: float):
    body = json.dumps({"model": model, "messages": messages, "max_tokens": max_tokens,
                       "reasoning_effort": "low", "stream": False}).encode()
    req = urllib.request.Request(url + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8009")
    ap.add_argument("--model", default="swift-1.5-iq3_xxs")
    ap.add_argument("--turns", type=int, default=8)
    ap.add_argument("--seed-tokens", type=int, default=55000)
    ap.add_argument("--max-tokens", type=int, default=900)
    ap.add_argument("--idle", type=float, default=20.0,
                    help="seconds to idle between turns, so the engine parks the conversation "
                         "and the next turn has to restore it (the shape of the 04:09 death)")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--timeout", type=float, default=5400.0)
    args = ap.parse_args()

    history = [{"role": "user",
                "content": block(args.seed_tokens, args.seed)
                           + "\n\nSummarise the above in one short sentence."}]
    print("multiturn-stress: seed ~%d tokens, %d turns, max_tokens %d, idle %.0fs"
          % (args.seed_tokens, args.turns, args.max_tokens, args.idle), flush=True)

    for turn in range(1, args.turns + 1):
        if turn > 1 and args.idle > 0:
            time.sleep(args.idle)          # idle long enough for a park-to-disk
        started = time.time()
        try:
            payload = ask(args.url, args.model, history, args.max_tokens, args.timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            print("multiturn-stress: turn %d FAILED after %.1fs: HTTP %s %s"
                  % (turn, time.time() - started, e.code, detail), flush=True)
            return 1
        except Exception as e:
            print("multiturn-stress: turn %d connection failed after %.1fs: %r"
                  % (turn, time.time() - started, e), flush=True)
            return 2

        usage = payload.get("usage") or {}
        answer = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        elapsed = time.time() - started
        print("turn %d: prompt=%s completion=%s elapsed=%.1fs decode=%s tok/s answer_chars=%d"
              % (turn, usage.get("prompt_tokens"), usage.get("completion_tokens"), elapsed,
                 ("%.1f" % (usage["completion_tokens"] / elapsed)) if usage.get("completion_tokens") else "n/a",
                 len(answer)), flush=True)
        if not answer:
            print("multiturn-stress: turn %d returned an empty answer" % turn, flush=True)
            return 1
        # Grow the conversation the way an agent lane does: keep the answer, add the next question.
        history.append({"role": "assistant", "content": answer})
        history.append({"role": "user",
                        "content": block(4000, args.seed + turn)
                                   + "\n\nAnswer in one sentence, then list three risks."})
    print("multiturn-stress: all %d turns answered" % args.turns, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

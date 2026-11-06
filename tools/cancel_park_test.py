#!/usr/bin/env python3
"""#1620: a request cancelled right after it restored a parked conversation must not lose that conversation.

  A (long answer) -> B (an unrelated chat parks A) -> A2 = A + a short next turn, STOPped as soon as the engine
  says RESUME (the parked A is restored, nothing or a few windows read) -> A3 = A + another next turn.

A3 must reuse the whole of A and its answer from the live session; without the fix the cancelled A2 had taken
the parked entry and A3 fell back to the turn checkpoint (the prompt, not the answer).

  python3 tools/cancel_park_test.py CFG   # CFG: a run config json (as tools used by smoke); needs a conversation cache:
  STRATA_EXTRA_ARGS="--prompt-cache 4 --conversation-cache-mib 2048 --conversation-cache-slots 3"
Prints PASS / FAIL and the reused token counts.  Exit status 0 on PASS, 1 on FAIL, 3 on a PRECONDITION
failure - a stop BEFORE any cancellation coverage is claimed.

L23 (measured on the 262144 dflash_w3 window): with the production-inherited
--conversation-cache-min-tokens 2048 / --conversation-cache-park-gain 4096 this fixture parked NOTHING
(the engine printed "skip parking (1829 tokens, below the 2048 minimum; parked=0)"), so the cancellation
ran on a conversation that was never parked and the old "(A is parked now)" print was a false claim.
The tool now proves the precondition from the engine's OWN lines - "conversation cache: parked"
(generate.cpp:9691) after the switch to B, "conversation cache: restored" (13075) or a RESUME with a
reused count > 0 at A2 - and stops with PRECONDITION FAIL (exit 3), echoing the engine's own
"skip parking (…)" reason (9524), instead of testing and reporting a cancellation.  A run config for
this tool needs a cache that parks this fixture: --conversation-cache-min-tokens 1 and
--conversation-cache-park-gain 0 (0 parks every turn, the engine's default; generate.cpp:12879-12887).
The final PASS thresholds are unchanged: fin2 == "cancel" and reused3 >= len(A1)+len(outA)-3.
"""
import json, os, queue, sys, threading, time
from pathlib import Path

REPO = Path(os.environ.get("STRATA_REPO", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(REPO / "tools")); sys.path.insert(0, str(REPO))
import strata_tokenizer as ST
from serve.server import StrataEngine, child_env, layer_split_of, layer_split_value

cfg = json.load(open(sys.argv[1]))
tp = Path(cfg["tokenizer"])
vocab = json.loads((tp / "vocab.json").read_text())
toks = [None] * len(vocab)
for t, i in vocab.items():
    toks[i] = t
tok = ST.Tokenizer(toks, (tp / "merges.txt").read_text().split("\n"), json.loads((tp / "token_type.json").read_text()))
args = cfg["args"] + os.environ.get("STRATA_EXTRA_ARGS", "").split()
# L23: a config with several GPUs runs the REAL layer split, exactly the rule serve/server.py:2871
# applies in the server path (the engine gets --serve from StrataEngine, which --layer-split needs).
# A config naming one GPU adds nothing, so the tool's standalone behavior is unchanged.
if layer_split_of(cfg) and "--layer-split" not in args:
    args += ["--layer-split", layer_split_value(cfg)]
LOG = os.environ.get("SMOKE_LOG", cfg["log"])
LOG0 = Path(LOG).stat().st_size if Path(LOG).exists() else 0     # the engine log opens in APPEND mode
eng = StrataEngine(os.environ.get("STRATA_EXE", cfg["exe"]), args, cwd=cfg.get("cwd"),
                   log=LOG, env=child_env(cfg))

HEAD = "<|im_start|>user\n"
GEN = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def engine_lines():
    """The engine's OWN stderr lines written since this tool started (its log opens in append)."""
    try:
        with open(LOG, "rb") as fh:
            fh.seek(LOG0)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def cache_lines(text, word):
    """The engine's OWN conversation-cache lines: "parked" and "restored" are the events
    (generate.cpp:9691 and 13075), "skip parking" is the engine's refusal WITH the reason it
    measured (9524). No state is invented here - only lines the engine printed."""
    return [ln.strip() for ln in text.splitlines() if ("conversation cache: " + word) in ln]


def stop_engine():
    """Close the engine the way this tool always did (stdin.close, wait(60), else kill)."""
    try:
        eng.proc.stdin.close()
        eng.proc.wait(60)
    except Exception:
        eng.proc.kill()


def enc(text):
    return tok.encode(text, parse_special=True)


def run(ids, max_new=200):
    out = [t for t in eng.generate(ids, max_new, {"temperature": 0}, threading.Event()) if t is not None]
    return out, int(eng.last.get("reused") or 0)


def run_cancelled(ids):
    """GEN, then STOP once the engine has said RESUME (the restore is done, the prompt is not read yet)."""
    eng.proc.stdin.write("GEN 200 " + ",".join(str(int(t)) for t in ids) + "\n")
    eng.proc.stdin.flush()
    stopped, reused, fin = False, None, None
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            line = eng.lines.get(timeout=1)
        except queue.Empty:
            continue
        if line is None:
            raise SystemExit("FAIL: the engine died")
        if line.startswith("RESUME "):
            reused = int(line.split()[1])
            eng.proc.stdin.write("STOP\n"); eng.proc.stdin.flush(); stopped = True
        elif line.startswith("DONE "):
            fin = line.split()[5] if len(line.split()) > 5 else "?"
            break
    return reused, fin, stopped


story = " ".join(f"Item {i}: the quick brown fox number {i} jumps over lazy dog {i * 7}." for i in range(70))
A1 = enc(HEAD + story + "\nNow count from 1 to 120 separated by commas." + GEN)
outA, r0 = run(A1, 300)
print(f"A: prompt {len(A1)} tokens, {len(outA)} generated, reused {r0}", flush=True)
B1 = enc(HEAD + "Say hello in one short sentence." + GEN)
outB, rb = run(B1, 20)
# L23: the tool no longer CLAIMS a park. The engine prints its own park line when it stores one, and
# its own "skip parking" line with the reason it measured when it does not. Parking is a
# switch-time decision, so by now the switch to B has already tried to park A.
parked = cache_lines(engine_lines(), "parked")
skipped = cache_lines(engine_lines(), "skip parking")
print(f"B: prompt {len(B1)} tokens, reused {rb}; engine park lines {len(parked)}, skip-parking lines "
      f"{len(skipped)} (the tool states only what the engine printed)", flush=True)
if not parked:
    stop_engine()
    print("PRECONDITION FAIL: the engine parked NOTHING, so cancelling a restore tests nothing; its "
          "own words: " + " | ".join(skipped[-3:] or ["(no conversation-cache line at all)"]), flush=True)
    sys.exit(3)
cont = enc("<|im_end|>\n<|im_start|>user\nNow say only the word done." + GEN)
A2 = A1 + outA + cont
reused2, fin2, stopped = run_cancelled(A2)
restored = cache_lines(engine_lines(), "restored")
print(f"A2 (cancelled): restored/reused at RESUME {reused2}, finish {fin2}, STOP sent {stopped}; "
      f"engine restored lines {len(restored)}", flush=True)
if not ((reused2 or 0) > 0 or restored):
    stop_engine()
    print("PRECONDITION FAIL: no restore happened before the STOP (RESUME reused 0 and no "
          "'conversation cache: restored' line), so A3 would prove nothing about a parked "
          "conversation", flush=True)
    sys.exit(3)
cont3 = enc("<|im_end|>\n<|im_start|>user\nNow say only the word finished." + GEN)
A3 = A1 + outA + cont3
outA3, reused3 = run(A3, 20)
want = len(A1) + len(outA) - 3
print(f"A3: prompt {len(A3)} tokens, reused {reused3} (A + its answer = {len(A1) + len(outA)}; need >= {want})", flush=True)
eng.proc.stdin.close()
try:
    eng.proc.wait(60)
except Exception:
    eng.proc.kill()
ok = fin2 == "cancel" and reused3 >= want
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

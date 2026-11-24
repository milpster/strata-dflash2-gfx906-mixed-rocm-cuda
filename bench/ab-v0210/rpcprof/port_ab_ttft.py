#!/usr/bin/env python3
"""port_ab_ttft.py - the TTFT / time-between-turns leg, from the server's own event stream.

Why this exists: ab9_driver.post() blocks on a non-streaming completion, so it can time the
whole turn but not the first token, and infill_measure's ttft_s is not the served turn's
first token. The Phase-2 contract asks for TTFT and the inter-turn wall measured per turn:
  * client side: the wall time to the first non-empty streamed delta (a streaming request);
  * server side: the server's own activity events (queued / admitted / reading / first_token /
    finished, each with a timestamp) from GET /v1/telemetry's activity block, so the number
    does not depend on this client's scheduling.

One leg = one 3-turn conversation on one boot. Turn 1 reads the TTFT leg's OWN frozen 2k
prompt (SHAPES.ttft2k, a different head tag from pp2k, so turn 1 is a cold prefill even in a
boot whose short group already ran pp2k); turns 2 and 3 append a short user line to the SAME
conversation, so their TTFT is the cached-prefix turn latency production serves.

CPU-only standalone: it talks to the server over HTTP like any client. --selftest exercises
the SSE parser and the event pairing on a synthetic transcript, with no network.

Engine identity: the record names the binary the leg ran on (engine_exe, engine_sha256, read
through /proc/<pid>/exe after the requests and the sampler are done) exactly as ab9_driver.py
writes it, because a TTFT or a rate nobody can attribute to a build is not a measurement: the
W2 claims said "n=2/arm" while five ttft3 records named no engine at all. The identity code is
ab9_driver.engine_identity - imported, never duplicated - so the two record kinds cannot drift.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ab9_driver  # MemSampler + find_engine_pid: one memory-cost implementation

TURN_LINES = ("Now list, in one line each, the three numbers that appeared most often above.",
              "Now answer with a single sentence: what is this text about?",
              "Thanks - one word: which subject dominates?")


def iter_sse(lines, clock):
    """Parse an SSE byte stream, yielding (kind, payload, at).

    kind is "delta" (a chunk with content or reasoning text), "usage", or "done".
    clock() is called once per line so a test can pass a deterministic clock.
    """
    for raw in lines:
        at = clock()
        line = raw.strip()
        if not line or line.startswith(b":"):
            continue
        if line.startswith(b"event:"):
            continue
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if payload == b"[DONE]":
            yield "done", None, at
            return
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        emitted = False
        try:
            choice = obj["choices"][0]
        except (KeyError, IndexError, TypeError):
            choice = None
        if choice is not None:
            delta = choice.get("delta") or {}
            text = delta.get("content") or delta.get("reasoning_content") or ""
            if text:
                yield "delta", obj, at
                emitted = True
            elif choice.get("finish_reason"):
                yield "finish", choice.get("finish_reason"), at
                emitted = True
        if obj.get("usage"):
            yield "usage", obj, at
        elif not emitted:
            continue


def http_json(url, body=None, headers=None, timeout=900):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def server_events(url):
    """The server's activity rows: {seq, at, request_id, event, outcome?} plus its clock.

    /v1/telemetry carries the last 32 transitions; /metrics carries the same list. Prefer
    telemetry, fall back to metrics, and return [] when neither is readable.
    """
    for path, pick in (("/v1/telemetry", lambda j: (j.get("activity") or {}).get("events") or []),
                       ("/metrics", lambda j: (j.get("activity") or {}).get("events") or [])):
        try:
            return pick(http_json(url + path, timeout=10))
        except Exception:  # noqa: BLE001 - telemetry is evidence, never a blocker
            continue
    return []


def inter_turn_wall(previous, current):
    """Next turn's first token minus the previous turn's finish (the contract's definition).

    The client's own idle gap is not the turn latency: what production serves is the wait for
    the next turn's first token, counted from the moment the previous turn ended.
    """
    if not previous or not current:
        return None
    first, finished = current.get("first_delta_at"), previous.get("finished_at")
    if not isinstance(first, (int, float)) or not isinstance(finished, (int, float)):
        return None
    return round(first - finished, 3)


def turn_messages(corpus_text, turn):
    msgs = [{"role": "user", "content": corpus_text}]
    for i in range(1, turn):
        msgs.append({"role": "assistant", "content": ""})
        msgs.append({"role": "user", "content": TURN_LINES[(i - 1) % len(TURN_LINES)]})
    return msgs


def run_turn(url, messages, max_tokens, template, key_env, timeout):
    body = dict(template)
    body.update({"messages": messages, "max_tokens": max_tokens, "stream": True})
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if key_env and os.environ.get(key_env):
        headers["Authorization"] = "Bearer " + os.environ[key_env]
    req = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers=headers)
    t0 = time.time()
    first = None
    finish = None
    usage = None
    parts = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for kind, payload, at in iter_sse(r, time.time):
            if kind == "delta":
                if first is None:
                    first = at
                delta = payload["choices"][0]["delta"]
                parts.append(delta.get("content") or delta.get("reasoning_content") or "")
            elif kind == "usage":
                usage = payload.get("usage")
            elif kind == "finish":
                finish = payload
            elif kind == "done":
                break
    t_end = time.time()
    text = "".join(parts)
    return {"ttft_client_s": round((first - t0), 3) if first else None,
            "wall_s": round(t_end - t0, 3),
            # absolute epoch stamps: the inter-turn wall is the NEXT turn's first token minus
            # THIS turn's finish, never the client's idle gap between two calls
            "first_delta_at": first, "finished_at": t_end,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "chars": len(text), "finish_reason": finish, "usage": usage, "text": text}


def main() -> int:
    ap = argparse.ArgumentParser(description="TTFT / time-between-turns leg")
    ap.add_argument("--url", default="http://127.0.0.1:8009")
    ap.add_argument("--corpus", help="turn-1 prompt file (exact bytes, head-tagged)")
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--template", help="frozen request template JSON (must carry stream=false; "
                                      "this leg sets stream=true itself)")
    ap.add_argument("--arm", default="?")
    ap.add_argument("--leg", default="ttft3")
    ap.add_argument("--out", default=str(HERE / "out" / "port-ab-20261123" / "records"))
    ap.add_argument("--elog", default="")
    ap.add_argument("--hlog", default="")
    ap.add_argument("--engine-pid", type=int, default=None)
    ap.add_argument("--api-key-env", default="STRATA_API_KEY")
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--selftest", action="store_true", help="parse a synthetic transcript, no network")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    if not a.corpus:
        raise SystemExit("--corpus is required for a real leg")
    corpus = pathlib.Path(a.corpus).read_bytes().decode("utf-8")
    template = {"temperature": 0, "enable_thinking": False, "reasoning_effort": "low"}
    if a.template:
        template = {k: v for k, v in json.loads(pathlib.Path(a.template).read_text()).items()
                    if k not in ("messages", "max_tokens", "stream")}
    events_before = server_events(a.url)
    last_seq = max((e.get("seq", 0) for e in events_before), default=0)

    cached_start = ab9_driver.cached_mib()
    # the pid is pinned for the whole leg: the sampler and the engine identity below must
    # describe the SAME process (a second find_engine_pid() could land on prod's engine)
    engine_pid = a.engine_pid if a.engine_pid is not None else ab9_driver.find_engine_pid()
    sampler = ab9_driver.MemSampler(engine_pid)
    sampler.start()
    turns = []
    messages = turn_messages(corpus, 1)
    try:
        for turn in range(1, a.turns + 1):
            if turn > 1:
                messages = turn_messages(corpus, turn)
                messages[-2]["content"] = turns[-1]["text"]  # the assistant reply it must send back
            row = run_turn(a.url, messages, a.max_tokens, template, a.api_key_env, a.timeout)
            row["inter_turn_wall_s"] = inter_turn_wall(turns[-1] if turns else None, row)
            turns.append(row)
            print("turn " + str(turn) + ": ttft " + str(row["ttft_client_s"]) + " s, wall "
                  + str(row["wall_s"]) + " s, inter-turn " + str(row["inter_turn_wall_s"])
                  + " s, " + str(row["chars"]) + " chars", flush=True)
    finally:
        sampler.stop_evt.set()
        sampler.join()

    # Identity is read here, after the wire and after the sampler, exactly as ab9_driver does:
    # it is provenance for the turns above, so hashing a 30-50 MB engine must not sit anywhere
    # the measured wall could see it. cached_end_mib is taken first for the same reason.
    cached_end = ab9_driver.cached_mib()
    identity = ab9_driver.engine_identity(engine_pid)

    events_after = server_events(a.url)
    new_events = [e for e in events_after if e.get("seq", 0) > last_seq]
    by_request = {}
    for e in new_events:
        by_request.setdefault(e.get("request_id"), []).append(e)
    server_turns = []
    for rid, rows in by_request.items():
        stamps = {r["event"]: r["at"] for r in rows if "at" in r}
        ttft = (stamps["first_token"] - stamps["queued"]) if "first_token" in stamps and "queued" in stamps else None
        server_turns.append({"request_id": rid,
                             "ttft_server_s": round(ttft, 3) if ttft is not None else None,
                             "wall_s": round(stamps["finished"] - stamps["queued"], 3)
                             if "finished" in stamps and "queued" in stamps else None,
                             "events": [{"event": r["event"], "at": r["at"]} for r in rows]})

    rec = {"arm": a.arm, "leg": a.leg, "kind": "ttft", "turns": a.turns,
           "max_tokens": a.max_tokens, "ts": round(time.time(), 3),
           "corpus": a.corpus, "corpus_sha256": hashlib.sha256(corpus.encode()).hexdigest(),
           "request": {"template": template, "stream": True,
                       "template_sha256": hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest()},
           "turn_rows": [{k: v for k, v in row.items() if k != "text"} for row in turns],
           "server_turns": server_turns,
           "elog": a.elog, "hlog": a.hlog,
           "mem": {"engine_pid": engine_pid,
                   "cached_start_mib": cached_start, "cached_end_mib": cached_end,
                   "max_rss_mib": sampler.max_rss_mib,
                   "min_avail_mib": None if sampler.min_avail_mib == 1 << 30 else sampler.min_avail_mib,
                   "max_swap_used_mib": sampler.max_swap_used_mib,
                   "max_vram_mib": sampler.max_vram_mib},
           # Which build produced these turns (ab9_driver.MemSampler+engine_identity are the one
           # implementation of both). Without these a ttft3 record cannot be assigned to an
           # arm's build, and --check fails such a leg rather than printing it as INFO.
           "engine_exe": identity["engine_exe"],
           "engine_sha256": identity["engine_sha256"],
           "engine_identity_reason": identity["engine_identity_reason"]}
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / (a.arm + "-" + a.leg + ".json")
    path.write_text(json.dumps(rec, indent=1) + "\n")
    print(json.dumps({k: v for k, v in rec.items() if k != "server_turns"}))
    return 0


def selftest() -> int:
    """Prove the parser and the event pairing without a server or a GPU."""
    transcript = [
        (1.0, b": keepalive\n"),
        (2.0, b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n'),
        (2.5, b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n'),
        (3.0, b'data: {"choices":[{"delta":{"content":"lo"}}]}\n'),
        (3.5, b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"completion_tokens":2}}\n'),
        (4.0, b"data: [DONE]\n"),
    ]
    ticks = iter([t for t, _ in transcript])
    kinds = [(kind, at) for kind, _, at in iter_sse([line for _, line in transcript],
                                                    lambda: next(ticks))]
    assert kinds[0] == ("delta", 2.5), kinds            # first NON-EMPTY delta, not the role chunk
    assert ("finish", 3.5) in kinds and kinds[-1] == ("done", 4.0), kinds
    events = [{"seq": 1, "at": 10.0, "request_id": "r1", "event": "queued"},
              {"seq": 2, "at": 10.2, "request_id": "r1", "event": "admitted"},
              {"seq": 3, "at": 12.7, "request_id": "r1", "event": "first_token"},
              {"seq": 4, "at": 15.0, "request_id": "r1", "event": "finished"}]
    stamps = {e["event"]: e["at"] for e in events}
    assert round(stamps["first_token"] - stamps["queued"], 3) == 2.7
    assert round(stamps["finished"] - stamps["queued"], 3) == 5.0
    msg = turn_messages("TEXT", 3)
    assert msg[0]["content"] == "TEXT" and len(msg) == 5 and msg[-1]["role"] == "user", msg
    # inter-turn = next first token - previous finish (NOT the idle gap between calls)
    prev = {"finished_at": 100.0, "wall_s": 4.0}
    cur = {"first_delta_at": 106.5, "wall_s": 3.0}
    assert inter_turn_wall(prev, cur) == 6.5, inter_turn_wall(prev, cur)
    assert inter_turn_wall(None, cur) is None
    assert inter_turn_wall({"finished_at": None}, cur) is None
    # the old bug: the client's idle gap read ~0; the contract's number is the wait above
    assert inter_turn_wall(prev, cur) != round(0.0, 3)
    print("selftest OK: first-delta TTFT, finish/done, event pairing, 3-turn message shape, "
          "inter-turn = next first token - previous finish")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

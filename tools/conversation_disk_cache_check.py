#!/usr/bin/env python3
"""GPU-window harness for the automatic disk conversation cache (plan section 8.2).

Drives a DISPOSABLE candidate engine + server (worktree build, its own port, own
cache dir).  Never touches production.  Evidence: engine log lines (park /
restore / DCACHE / pair hits), /slots prompt-token counts, /metrics
conversation_cache card, per-request latencies, engine RSS/PSS high-water, and
greedy (temperature 0) continuation equality across park/restore boundaries.

    python3 tools/conversation_disk_cache_check.py --config CFG --scenario s3 --depth 16000

The config is a serve/server.py config json whose "exe" points at the candidate
build; the harness only starts/stops the SERVER (server.py spawns the engine).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def http(port: int, method: str, path: str, body: dict | None = None, timeout: int = 1800):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    for attempt in (0, 1):   # one retry on a just-started server's refused socket
        try:
            with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path}: {e.code} {e.read()[:400]!r}") from None
        except urllib.error.URLError as e:
            if attempt or not isinstance(getattr(e, "reason", None), ConnectionRefusedError):
                raise
            time.sleep(1)


def wait_health(port: int, deadline_s: float) -> dict:
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        try:
            return http(port, "GET", "/health", timeout=10)
        except Exception:
            time.sleep(2)
    raise RuntimeError(f"server on :{port} never became healthy in {deadline_s:.0f}s")


class RssSampler(threading.Thread):
    """Samples the engine process's RSS/PSS/anon every 0.5 s into a list."""

    def __init__(self, exe: str):
        super().__init__(daemon=True)
        self.exe = exe
        self.samples: list[dict] = []
        self.stop = threading.Event()

    @staticmethod
    def _read(pid: int) -> dict | None:
        try:
            fields = {}
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith(("VmRSS:", "VmHWM:")):
                    k, v = line.split(":", 1)
                    fields[k] = int(v.split()[0]) * 1024
            roll = Path(f"/proc/{pid}/smaps_rollup").read_text()
            m = re.search(r"^Pss:\s+(\d+) kB", roll, re.M)
            a = re.search(r"^Anonymous:\s+(\d+) kB", roll, re.M)
            if m: fields["Pss"] = int(m.group(1)) * 1024
            if a: fields["Anon"] = int(a.group(1)) * 1024
            return fields or None
        except OSError:
            return None

    def engine_pid(self) -> int | None:
        out = subprocess.run(["pgrep", "-f", f"^{self.exe} "], capture_output=True, text=True).stdout
        pids = [int(x) for x in out.split()]
        return pids[0] if pids else None

    def run(self):
        while not self.stop.is_set():
            pid = self.engine_pid()
            if pid:
                s = self._read(pid)
                if s:
                    s["t"] = round(time.time(), 1)
                    self.samples.append(s)
            self.stop.wait(0.5)

    def peak(self, key: str) -> int:
        return max((s[key] for s in self.samples if key in s), default=0)


class Harness:
    def __init__(self, config_path: Path, port: int, log_name: str):
        self.cfg_path = config_path
        self.cfg = json.loads(config_path.read_text())
        self.port = port
        self.log_path = REPO / "run" / "logs" / f"{log_name}-server.log"
        self.proc: subprocess.Popen | None = None
        self.helper: subprocess.Popen | None = None
        self.rss = RssSampler(self.cfg["exe"])

    def start_helper(self):
        """The expert helper exits when the engine disconnects, so each run starts its own.
        Configured by run/helper.json: {"exe": ..., "args": [...]} (prod-faithful parameters)."""
        hcfg = REPO / "run" / "helper.json"
        if not hcfg.exists() or not self.cfg.get("expert_helper_socket"):
            return
        h = json.loads(hcfg.read_text())
        log = open(REPO / "run" / "logs" / "helper.log", "ab")
        env = {"STRATA_HELPER_REPLY_TIMEOUT_MS": "600000", "PATH": os.environ["PATH"],
               "HOME": os.environ["HOME"], "TERM": "dumb"}
        self.helper = subprocess.Popen([h["exe"], *h["args"]], env=env,
                                       stdout=log, stderr=subprocess.STDOUT)
        hlog = REPO / "run" / "logs" / "helper.log"
        for _ in range(240):   # ~40 GiB of experts to load
            try:
                if "listening on" in hlog.read_text(errors="replace").rsplit("listening on", 1)[-1] or \
                   hlog.read_text(errors="replace").count("listening on") >= 1:
                    break
            except OSError:
                pass
            if self.helper.poll() is not None:
                raise RuntimeError(f"helper exited early with {self.helper.returncode}")
            time.sleep(2)
        print(f"[harness] helper up pid={self.helper.pid}", flush=True)

    def stop_helper(self):
        if self.helper and self.helper.poll() is None:
            self.helper.terminate()
            try:
                self.helper.wait(30)
            except subprocess.TimeoutExpired:
                self.helper.kill()

    def start(self):
        # fail fast: a stale server on the port would answer health checks and steal the run
        probe = subprocess.run(["ss", "-H", "-ltn", f"sport = :{self.port}"], capture_output=True, text=True)
        if probe.stdout.strip():
            raise RuntimeError(f"port {self.port} is already bound - refuse to run against a stale server")
        self.start_helper()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = ":".join(self.cfg.get("lib_dirs", []))
        self.log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [sys.executable, str(REPO / "serve" / "server.py"), "--engine", "strata",
             "--config", str(self.cfg_path), "--port", str(self.port)],
            cwd=str(REPO), env=env, stdout=self.log, stderr=subprocess.STDOUT)
        if self.rss.stop.is_set():   # a restart (s5): threads are single-use, carry the samples over
            carried = self.rss.samples
            self.rss = RssSampler(self.cfg["exe"])
            self.rss.samples = carried
        self.rss.start()
        h = wait_health(self.port, 1800)
        print(f"[harness] server up pid={self.proc.pid} health={h.get('status')}", flush=True)
        return h

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(60)
            except subprocess.TimeoutExpired:
                self.proc.kill(); self.proc.wait(30)
        self.rss.stop.set()
        # the engine child must be gone too; it exits with the server's channel
        for _ in range(120):
            if self.rss.engine_pid() is None:
                break
            time.sleep(1)
        self.stop_helper()
        print(f"[harness] server down; engine gone={self.rss.engine_pid() is None}", flush=True)

    # ---- engine log evidence -------------------------------------------------
    def engine_log(self) -> str:
        p = Path(self.cfg["log"])
        return p.read_text(errors="replace") if p.exists() else ""

    def grep(self, pattern: str, since_len: int = 0) -> list[str]:
        return re.findall(pattern, self.engine_log()[since_len:])

    # ---- requests ------------------------------------------------------------
    def chat(self, messages: list[dict], max_tokens: int, tag: str) -> dict:
        t0 = time.time()
        r = http(self.port, "POST", "/v1/chat/completions", {
            "messages": messages, "max_tokens": max_tokens, "temperature": 0.0, "stream": False,
            "chat_template_kwargs": {"enable_thinking": False}})
        dt = time.time() - t0
        msg = r["choices"][0]["message"]
        text = msg.get("content") or msg.get("reasoning_content") or ""
        usage = r.get("usage", {})
        slots = http(self.port, "GET", "/slots") or {}
        ptoks = sum(s.get("n_prompt_tokens", 0) for s in slots) if isinstance(slots, list) else \
            (slots.get("slots", [{}]) or [{}])[0].get("n_prompt_tokens", 0)
        print(f"[harness] {tag}: {dt:.1f}s prompt_toks={usage.get('prompt_tokens')} "
              f"slot_prompt={ptoks} completion={usage.get('completion_tokens')} "
              f"rss_hwm={self.rss.peak('VmHWM') >> 20}MiB", flush=True)
        return {"tag": tag, "seconds": round(dt, 2), "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"), "slot_prompt_tokens": ptoks,
                "text": text}


def big_message(tokens: int, seed_word: str) -> str:
    # ~3.2 chars/token on this tokenizer; stable content, deterministic
    words = [f"{seed_word}{i:07d}" for i in range(tokens)]
    return " ".join(words)


def conv(messages: list[dict]) -> list[dict]:
    return messages


SCENARIOS: dict[str, callable] = {}


def scenario(name):
    def deco(fn):
        SCENARIOS[name] = fn
        return fn
    return deco


@scenario("s3_abab")
def s3(h: Harness, args):
    """A -> B -> A -> B at the requested depth, disk-only: reuse, parity, RAM."""
    depth = args.depth
    A = [{"role": "user", "content": big_message(depth, "alpha") + "\nReply with the single word OK."}]
    B = [{"role": "user", "content": big_message(depth, "beta") + "\nReply with the single word OK."}]
    log0 = len(h.engine_log())
    a1 = h.chat(A, 8, f"A1@{depth}")
    b1 = h.chat(B, 8, f"B1@{depth}")
    a2 = h.chat(A + [{"role": "assistant", "content": a1["text"]},
                     {"role": "user", "content": "Reply with the single word YES."}], 8, f"A2@{depth}")
    b2 = h.chat(B + [{"role": "assistant", "content": b1["text"]},
                     {"role": "user", "content": "Reply with the single word NO."}], 8, f"B2@{depth}")
    new = h.engine_log()[log0:]
    return {
        "requests": [a1, b1, a2, b2],
        "parked": re.findall(r"conversation cache: disk park streamed (\d+) tokens[^\n]*", new),
        "dcache": re.findall(r"DCACHE (\w+) (\d+) (\d+)[^\n]*", new)[:40],
        "restored_or_reuse": re.findall(r"conversation cache[^\n]*(restored|reuse|resume|hit)[^\n]*", new)[:20],
        "prompt_eval": re.findall(r"prompt eval[^\n]*", new)[:12],
        "rss_peaks": {k: h.rss.peak(k) >> 20 for k in ("VmRSS", "VmHWM", "Pss", "Anon")},
    }


@scenario("s4_rewind")
def s4(h: Harness, args):
    """Same conversation, re-render the last reply beyond the draft ring horizon."""
    depth = args.depth
    m1 = [{"role": "user", "content": big_message(depth, "gamma") + "\nCount from 1 to 5."}]
    r1 = h.chat(m1, 48, "G1")
    # a long second turn pushes the ring far past the first turn's window
    m2 = m1 + [{"role": "assistant", "content": r1["text"]},
               {"role": "user", "content": big_message(depth, "delta") + "\nCount from 10 to 15."}]
    r2 = h.chat(m2, 48, "G2")
    # rewind: replace the second reply -> resume must come from a PAIRED checkpoint, not a full reread
    m3 = m1 + [{"role": "assistant", "content": r1["text"] + " (edited)"},
               {"role": "user", "content": big_message(depth, "delta") + "\nCount from 20 to 25."}]
    log0 = len(h.engine_log())
    r3 = h.chat(m3, 48, "G3-rewind")
    new = h.engine_log()[log0:]
    return {
        "requests": [r1, r2, r3],
        "pair_hits": re.findall(r"conversation cache[^\n]*pair[^\n]*", new)[:20],
        "ring_retained": re.findall(r"[^\n]*context_retained[^\n]*|ring[^\n]*retention[^\n]*", new)[:10],
        "reread": re.findall(r"[^\n]*reread[^\n]*|read the prompt again[^\n]*", new)[:10],
        "prompt_eval": re.findall(r"prompt eval[^\n]*", new)[:10],
    }


@scenario("s5_restart")
def s5(h: Harness, args):
    """Park A, restart the whole disposable pair, continue without manual restore."""
    depth = args.depth
    A = [{"role": "user", "content": big_message(depth, "epsilon") + "\nReply with the single word OK."}]
    a1 = h.chat(A, 8, "A1")
    log0 = len(h.engine_log())
    h.stop()
    h.start()
    a2 = h.chat(A + [{"role": "assistant", "content": a1["text"]},
                     {"role": "user", "content": "Reply with the single word YES."}], 8, "A2-after-restart")
    new = h.engine_log()[log0:]
    return {
        "requests": [a1, a2],
        "restart_reuse": re.findall(r"conversation cache[^\n]*(disk|restore|reuse|resume)[^\n]*", new)[:20],
        "prompt_eval": re.findall(r"prompt eval[^\n]*", new)[:10],
    }


@scenario("s6_misses")
def s6(h: Harness, args):
    """Edited early history: must be a clean miss, never stale reuse."""
    depth = args.depth
    A = [{"role": "user", "content": big_message(depth, "zeta") + "\nReply with the single word OK."}]
    a1 = h.chat(A, 8, "A1")
    A_edited = [{"role": "user", "content": big_message(depth, "zetaX") + "\nReply with the single word OK."}]
    log0 = len(h.engine_log())
    a2 = h.chat(A_edited, 8, "A2-edited-prefix")
    new = h.engine_log()[log0:]
    return {
        "requests": [a1, a2],
        "misses": re.findall(r"conversation cache[^\n]*(miss|mismatch|refus)[^\n]*", new)[:10],
        "prompt_eval": re.findall(r"prompt eval[^\n]*", new)[:6],
    }


@scenario("s7_corrupt")
def s7(h: Harness, args):
    """Corrupt one byte of A's parked file: the restore must refuse safely, reread, and still answer."""
    cache_dir = Path(args.cache_dir) if args.cache_dir else next(
        (Path(a) for a in h.cfg.get("args", []) if isinstance(a, str) and a.rstrip("/").endswith("disk-cache")),
        REPO / "run" / "disk-cache")
    depth = args.depth
    A = [{"role": "user", "content": big_message(depth, "alpha") + "\nReply with the single word OK."}]
    B = [{"role": "user", "content": big_message(depth, "beta") + "\nReply with the single word OK."}]
    a1 = h.chat(A, 8, "A1")   # A's conversation parks when B's request takes the slot
    h.chat(B, 8, "B1")
    files = sorted(cache_dir.glob("*.sess"))
    if not files:
        return {"error": "no parked .sess found to corrupt", "files": []}
    victim = files[0]   # conv-1.sess: A's parked file
    with open(victim, "r+b") as f:
        f.seek(max(0, victim.stat().st_size // 2))
        b = f.read(1)
        f.seek(max(0, victim.stat().st_size // 2))
        f.write(bytes([b[0] ^ 0xFF]))
    log0 = len(h.engine_log())
    r = h.chat(A + [{"role": "assistant", "content": a1["text"]},
                    {"role": "user", "content": "Reply with the single word YES."}], 8, "A2-after-corrupt")
    new = h.engine_log()[log0:]
    return {
        "corrupted": str(victim),
        "requests": [r],
        "refusals": re.findall(r"conversation cache[^\n]*(corrupt|checksum|refus|miss|mismatch)[^\n]*", new)[:10],
        "prompt_eval": re.findall(r"prompt \d+ tokens = [^\n]*", new)[:6],
    }


@scenario("s0_flags_off")
def s0(h: Harness, args):
    """Baseline, cache flags off: every switch is a cold prefill (parity reference)."""
    depth = args.depth
    A = [{"role": "user", "content": big_message(depth, "theta") + "\nReply with the single word OK."}]
    B = [{"role": "user", "content": big_message(depth, "iota") + "\nReply with the single word OK."}]
    a1 = h.chat(A, 8, f"A1@{depth}")
    b1 = h.chat(B, 8, f"B1@{depth}")
    a2 = h.chat(A + [{"role": "assistant", "content": a1["text"]},
                     {"role": "user", "content": "Reply with the single word YES."}], 8, f"A2@{depth}")
    log0 = len(h.engine_log())
    return {"requests": [a1, b1, a2],
            "prompt_eval": re.findall(r"prompt eval[^\n]*", h.engine_log()[log0:])[:12],
            "rss_peaks": {k: h.rss.peak(k) >> 20 for k in ("VmRSS", "VmHWM", "Pss", "Anon")}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--depth", type=int, default=16000)
    ap.add_argument("--cache-dir", default="")
    ap.add_argument("--keep-up", action="store_true", help="leave the server up after the scenario")
    ap.add_argument("--already-up", action="store_true", help="server already running; skip start/stop")
    args = ap.parse_args()

    h = Harness(args.config, args.port, f"{args.scenario}-{args.depth}")
    out_path = REPO / "run" / "logs" / f"{args.scenario}-{args.depth}-result.json"
    result: dict = {"error": "scenario did not run"}
    if not args.already_up:
        h.start()
    try:
        result = SCENARIOS[args.scenario](h, args)
    finally:
        m: dict = {}
        try:
            card = http(args.port, "GET", "/metrics") or {}
            m = card.get("conversation_cache", card if isinstance(card, dict) and "conversation_cache" in card else {})
        except Exception as e:
            m = {"error": str(e)}
        result["metrics_conversation_cache"] = m
        result["engine_log_tail"] = h.engine_log()[-4000:]
        out_path.write_text(json.dumps(result, indent=1))
        print(f"[harness] result -> {out_path}", flush=True)
        if not args.keep_up and not args.already_up:
            h.stop()
    print(json.dumps({k: v for k, v in result.items() if k != "engine_log_tail"}, indent=1)[:4000])


if __name__ == "__main__":
    main()

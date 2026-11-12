#!/usr/bin/env python3
"""ab9 load driver: ONE leg per call, single request in flight (concurrency 1).

Deterministic prompt builder: fixed seed, NO timestamps, exact UTF-8 byte length per
context (8000 / 80000), a unique per-leg tag line so no cache reuse is possible — the
judge line must read "N reused = 0". Requests: temperature 0, thinking off
(enable_thinking false) so the parity gate compares byte-identical completions.

Records byte offsets in the engine log and helper log around the request; the collector
reads ONLY that slice (no cross-leg contamination). Emits one JSON per leg to --out:
  <out>/<arm>-<leg>.json with walls, completion sha256, log slice offsets.

kinds:
  warm    tiny prompt, max_tokens 16 — gate-2 warmup (helper profile lines must appear)
  prefill fresh-prefill leg, 1 request (judge = "prompt N tokens = 0 reused + N read in X ms")
  tg      cached-rerun sanity: SAME prompt, max_tokens 1024/512, run twice; wall2 = cached
"""
import argparse
import glob
import hashlib
import json
import os
import random
import subprocess
import threading
import time
import urllib.request

URL = "http://127.0.0.1:8009/v1/chat/completions"
WORDS = ("cache eviction policy ring buffer page table slot arena transfer prefill layer "
         "expert token stream socket reply frame claim row vector hidden tensor block copy "
         "engine helper window pinned queue lock begin finish prompt tier device stream "
         "kernel copy engine token expert layer prefill socket reply claim row hidden block").split()


def build_prompt(arm: str, leg: str, ctx: int) -> str:
    """Seeded filler + unique tag, padded to EXACTLY ctx bytes (UTF-8)."""
    rng = random.Random(1729)
    tag = f"[ab9 arm={arm} leg={leg} ctx={ctx}]\n"
    n = ctx - len(tag.encode())
    parts: list[str] = []
    total = 0
    while total < n:
        line = " ".join(rng.choice(WORDS) for _ in range(9)) + "\n"
        parts.append(line)
        total += len(line.encode())
    text = "".join(parts)[: n - 1]  # filler clipped to leave room for the tag
    text += "\n"
    prompt = text + tag
    prompt = prompt.encode()[: ctx].decode("utf-8", errors="ignore")
    pad = ctx - len(prompt.encode())
    prompt += "." * pad  # dots tokenize as ordinary text; exact byte length guaranteed
    assert len(prompt.encode()) == ctx, (arm, leg, ctx, len(prompt.encode()))
    return prompt


def post(url: str, prompt: str, max_tokens: int, timeout: int) -> tuple[float, str, str]:
    body = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "enable_thinking": False,
        "reasoning_effort": "low",
        "stream": False,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    wall = time.time() - t0
    m = resp["choices"][0]["message"]
    # reasoning models can return content=null (budget eaten by reasoning_content, the
    # promo_check precedent): never crash, fall back, and record where the text came from.
    if m.get("content"):
        return wall, m["content"], "content"
    if m.get("reasoning_content"):
        return wall, m["reasoning_content"], "reasoning"
    return wall, "", "empty"


def size(path: str) -> int:
    return os.stat(path).st_size if os.path.exists(path) else 0


def find_engine_pid():
    """Locate the strata engine process: first argv element ends with /strata
    and --serve appears as its own NUL-delimited argument. Matching the bare
    substring 'strata --serve' would hit any wrapper whose cmdline merely
    contains that text (shells, this driver's own callers)."""
    me = os.getpid()
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == me:
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                cmd = f.read()
        except OSError:
            continue
        first = cmd.split(b"\x00", 1)[0]
        if first.endswith(b"/strata") and b"\x00--serve\x00" in cmd:
            return int(d)
    return None


class MemSampler(threading.Thread):
    """Per-leg memory cost: engine peak RSS (VmHWM, reset at leg start when
    permitted so each leg owns its peak), min MemAvailable, max swap in use,
    max VRAM per GPU (amdgpu sysfs cards + nvidia-smi). Operator directive
    2026-11-12: measurements must track max RAM/VRAM so speed claims can
    never again be read apart from their memory cost."""
    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid = pid
        self.stop_evt = threading.Event()
        self.max_rss_mib = 0
        self.min_avail_mib = 1 << 30
        self.max_swap_used_mib = 0
        self.max_vram_mib = {}

    def _poll(self):
        if self.pid:
            try:
                with open(f"/proc/{self.pid}/status") as f:
                    for line in f:
                        if line.startswith("VmHWM:"):
                            self.max_rss_mib = max(self.max_rss_mib, int(line.split()[1]) // 1024)
                            break
            except OSError:
                pass
        try:
            with open("/proc/meminfo") as f:
                mi = {k: int(v.split()[0]) for k, v in (l.split(":", 1) for l in f)}
            self.min_avail_mib = min(self.min_avail_mib, mi.get("MemAvailable", 1 << 30) // 1024)
            self.max_swap_used_mib = max(self.max_swap_used_mib,
                                         (mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) // 1024)
        except (OSError, ValueError):
            pass
        for p in glob.glob("/sys/class/drm/card*/device/mem_info_vram_used"):
            card = p.split("/")[4]
            try:
                mib = int(open(p).read()) // (1024 * 1024)
                self.max_vram_mib[card] = max(self.max_vram_mib.get(card, 0), mib)
            except (OSError, ValueError):
                pass
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5).stdout
            for i, line in enumerate(l for l in out.splitlines() if l.strip()):
                key = f"nvidia{i}"
                self.max_vram_mib[key] = max(self.max_vram_mib.get(key, 0), int(line.strip()))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    def run(self):
        if self.pid:  # reset the kernel's peak-RSS high-water mark for a per-leg peak
            try:
                with open(f"/proc/{self.pid}/clear_refs", "w") as f:
                    f.write("5")
            except OSError:
                pass
        while not self.stop_evt.is_set():
            self._poll()
            self.stop_evt.wait(1.0)
        self._poll()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True)
    ap.add_argument("--leg", required=True)
    ap.add_argument("--kind", required=True, choices=["warm", "prefill", "tg"])
    ap.add_argument("--ctx", type=int, required=True, help="exact prompt byte length")
    ap.add_argument("--max-tokens", type=int, default=8)
    ap.add_argument("--out", required=True)
    ap.add_argument("--elog", required=True)
    ap.add_argument("--hlog", required=True)
    ap.add_argument("--url", default=URL)
    a = ap.parse_args()

    prompt = build_prompt(a.arm, a.leg, a.ctx)
    max_tokens = 64 if a.kind == "warm" else a.max_tokens
    runs = 2 if a.kind == "tg" else 1
    e0, h0 = size(a.elog), size(a.hlog)
    sampler = MemSampler(find_engine_pid())
    sampler.start()
    t0 = time.time()
    walls: list[float] = []
    content = ""
    content_from = "empty"
    for _ in range(runs):
        wall, content, content_from = post(a.url, prompt, max_tokens, timeout=1800 if a.ctx >= 80000 else 900)
        walls.append(round(wall, 3))
    wall = round(time.time() - t0, 3)
    sampler.stop_evt.set()
    sampler.join()
    e1, h1 = size(a.elog), size(a.hlog)

    rec = {
        "arm": a.arm, "leg": a.leg, "kind": a.kind, "ctx": a.ctx,
        "max_tokens": max_tokens, "ts": round(time.time(), 3),
        "walls": walls, "wall_s": wall, "content_from": content_from,
        "prompt_bytes": len(prompt.encode()),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "elog": a.elog, "elog_start": e0, "elog_end": e1,
        "hlog": a.hlog, "hlog_start": h0, "hlog_end": h1,
        "mem": {
            "max_rss_mib": sampler.max_rss_mib,
            "min_avail_mib": None if sampler.min_avail_mib == 1 << 30 else sampler.min_avail_mib,
            "max_swap_used_mib": sampler.max_swap_used_mib,
            "max_vram_mib": sampler.max_vram_mib,
        },
    }
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f"{a.arm}-{a.leg}.json")
    with open(path, "w") as f:
        json.dump(rec, f, indent=1)
    print(json.dumps(rec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

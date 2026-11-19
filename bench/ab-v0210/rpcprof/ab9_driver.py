#!/usr/bin/env python3
"""ab9 load driver: ONE leg per call, single request in flight (concurrency 1).

Deterministic prompt builder: fixed seed, NO timestamps, exact UTF-8 byte length per
context (8000 / 80000), a unique per-leg tag line so no cache reuse is possible — the
judge line must read "N reused = 0". Requests: temperature 0, thinking off
(enable_thinking false) so the parity gate compares byte-identical completions.

Those parity settings are the DEFAULT, not a law: --request-template points at a frozen
manifest file that pins the sampling and reasoning effort a speed comparison is meant to
measure under (production's own), and then the record carries the template digest and the
effective settings. Pass --max-tokens explicitly: the template owns neither the prompt nor
the generation cap.

The tag sits at the TAIL, which does NOT force a fresh prefill: the leading tokens still
match, so a "prefill" leg can be served from the conversation cache. Callers that need a
genuinely cold prompt pass --prompt-file, whose bytes were built with the identity tag at
the HEAD (see champions_prepare.build_paired_prompt). With --prompt-file the prompt is read
from disk, must match --ctx byte for byte and, when given, --prompt-sha256; the digest goes
into the record so the arms can be proven to have read identical input.

Records byte offsets in the engine log and helper log around the request; the collector
reads ONLY that slice (no cross-leg contamination). Every request of a multi-request leg
keeps its own window, wall and response digest — a leg is more than "the last of two".
Emits one JSON per leg to --out:
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
import pathlib
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


def build_body(prompt: str, max_tokens: int, template: dict | None) -> dict:
    """The request body to send.

    No template means today's parity body, key for key: temperature 0, thinking off,
    low reasoning — the settings the byte-identical-completion gate needs.

    A template is a frozen profile's sampling and reasoning pins. The driver adds only
    the two fields it owns — the corpus prompt and the shape's generation cap — so a
    speed leg runs on the settings production actually serves instead of inheriting the
    parity defaults. Omitted fields stay the server's to decide (enable_thinking and the
    rest inherit the current server behaviour).
    """
    if template is None:
        return {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
            "enable_thinking": False,
            "reasoning_effort": "low",
            "stream": False,
        }
    body = dict(template)
    body["messages"] = [{"role": "user", "content": prompt}]
    body["max_tokens"] = max_tokens
    return body


def post(url: str, prompt: str, max_tokens: int, timeout: int,
         template: dict | None = None) -> tuple[float, str, str, str]:
    body = build_body(prompt, max_tokens, template)
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    wall = time.time() - t0
    m = resp["choices"][0]["message"]
    # reasoning models can return content=null (budget eaten by reasoning_content, the
    # promo_check precedent): never crash, fall back, and record where the text came from.
    model = str(resp.get("model", ""))
    if m.get("content"):
        return wall, m["content"], "content", model
    if m.get("reasoning_content"):
        return wall, m["reasoning_content"], "reasoning", model
    return wall, "", "empty", model


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
    ap.add_argument("--prompt-file", help="read the prompt from this file instead of build_prompt()")
    ap.add_argument("--prompt-sha256", help="reject the run unless the prompt digest matches")
    ap.add_argument("--champs-shape", help="champions panel: workload shape name")
    ap.add_argument("--champs-pair", type=int, help="champions panel: pair index within the shape")
    ap.add_argument("--champs-measured", action="store_true",
                    help="champions panel: this pair counts toward the medians")
    ap.add_argument("--champs-retry", type=int, default=0,
                    help="champions panel: bounded retry number of this pair (0 = first attempt)")
    ap.add_argument("--request-template",
                    help="JSON object of request-body pins from the frozen manifest; the driver "
                         "adds messages + max_tokens only. Without it the legacy parity body is sent.")
    ap.add_argument("--request-template-sha256",
                    help="reject the run unless the template file digests to this")
    a = ap.parse_args()

    prompt, prompt_sha = load_prompt(a)
    template, template_sha = load_template(a)
    max_tokens = 64 if a.kind == "warm" else a.max_tokens
    runs = 2 if a.kind == "tg" else 1
    e0, h0 = size(a.elog), size(a.hlog)
    sampler = MemSampler(find_engine_pid())
    sampler.start()
    t0 = time.time()
    walls: list[float] = []
    requests: list[dict] = []
    content = ""
    content_from = "empty"
    for i in range(runs):
        start_e, start_h = size(a.elog), size(a.hlog)
        wall, content, content_from, model = post(a.url, prompt, max_tokens,
                                                  timeout=1800 if a.ctx >= 80000 else 900,
                                                  template=template)
        walls.append(round(wall, 3))
        requests.append({
            "index": i, "wall_s": round(wall, 3), "content_from": content_from,
            "response_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "elog_start": start_e, "elog_end": size(a.elog),
            "hlog_start": start_h, "hlog_end": size(a.hlog),
        })
        if template:
            # What the server says it served, per request, next to what we asked for.
            requests[-1]["model"] = model
    wall = round(time.time() - t0, 3)
    sampler.stop_evt.set()
    sampler.join()
    e1, h1 = size(a.elog), size(a.hlog)

    rec = {
        "arm": a.arm, "leg": a.leg, "kind": a.kind, "ctx": a.ctx,
        "max_tokens": max_tokens, "ts": round(time.time(), 3),
        "walls": walls, "wall_s": wall, "content_from": content_from,
        "prompt_bytes": len(prompt.encode()), "prompt_sha256": prompt_sha,
        "requests": requests,
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
    if template:
        # The body as actually sent, with the prompt replaced by its digest so the
        # settings hash is comparable across arms instead of 88 KB of filler.
        sent = settings_echo(template, prompt_sha, max_tokens)
        rec["request"] = {
            "template_path": a.request_template,
            "template_sha256": template_sha,
            "settings": sent,
            "settings_sha256": hashlib.sha256(
                json.dumps(sent, sort_keys=True).encode()).hexdigest(),
            "url": a.url,
        }
    if a.champs_shape and a.champs_pair is None:
        raise SystemExit("--champs-shape requires --champs-pair")
    if a.champs_shape:
        rec["champs"] = {"shape": a.champs_shape, "pair": a.champs_pair,
                         "measured": a.champs_measured, "retries": a.champs_retry}
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f"{a.arm}-{a.leg}.json")
    with open(path, "w") as f:
        json.dump(rec, f, indent=1)
    print(json.dumps(rec))
    return 0


def load_template(a) -> tuple[dict | None, str]:
    """--request-template wins; without it the legacy parity body is sent unchanged.

    The file is a frozen manifest artifact, so it is pinned by digest and validated
    before anything goes on the wire: a template that carried messages or max_tokens
    would silently override the corpus and the shape it is supposed to be paired with.
    """
    if not a.request_template:
        return None, ""
    path = pathlib.Path(a.request_template)
    blob = path.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    if a.request_template_sha256 and digest != a.request_template_sha256:
        raise SystemExit(f"request-template sha256 {digest} != pinned {a.request_template_sha256}")
    try:
        template = json.loads(blob)
    except ValueError as error:
        raise SystemExit(f"request-template is not JSON: {path}: {error}") from error
    if not isinstance(template, dict):
        raise SystemExit(f"request-template must be a JSON object: {path}")
    owned = sorted({"messages", "max_tokens"} & set(template))
    if owned:
        raise SystemExit(f"request-template must not pin {owned}: the corpus and the "
                         f"shape own them ({path})")
    return template, digest


def settings_echo(template: dict, prompt_sha: str, max_tokens: int) -> dict:
    """The effective request settings, with the prompt represented by its digest."""
    return {**template, "messages": [{"role": "user", "content_sha256": prompt_sha}],
            "max_tokens": max_tokens}


def load_prompt(a) -> tuple[str, str]:
    """--prompt-file wins; the legacy seeded builder is the default and is unchanged.

    A file prompt is byte-authoritative: it must match --ctx exactly and, when the
    caller pins it, --prompt-sha256. A length mismatch is a harness bug and fails closed.
    """
    if not a.prompt_file:
        prompt = build_prompt(a.arm, a.leg, a.ctx)
        return prompt, hashlib.sha256(prompt.encode()).hexdigest()
    blob = pathlib.Path(a.prompt_file).read_bytes()
    if len(blob) != a.ctx:
        raise SystemExit(f"prompt-file length {len(blob)} != --ctx {a.ctx}: {a.prompt_file}")
    digest = hashlib.sha256(blob).hexdigest()
    if a.prompt_sha256 and digest != a.prompt_sha256:
        raise SystemExit(f"prompt-file sha256 {digest} != pinned {a.prompt_sha256}")
    return blob.decode("utf-8"), digest


if __name__ == "__main__":
    raise SystemExit(main())

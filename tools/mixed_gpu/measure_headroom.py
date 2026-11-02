#!/usr/bin/env python3
"""tools/mixed_gpu/measure_headroom.py - the P0 split-SERVE measurement runner (plan default).

STATUS OF THE PREVIOUS DESIGN: the one-shot generate-mode runner this file used to hold is RETIRED AND
INVALID as production evidence.  Generate mode refuses --layer-split outside --serve (measured 2026-10-27,
rc=2: build-mixed-gpu/p0/logs/incident-4k-2026-10-27T07:45*.log), so it strips the production split (32/47
in-process stages) - a single-process path is NOT production-representative.  The split-topology gate
(.slim/deepwork/mixed-cuda-finish/split-topology.md §4) accepted the replacement: measure the incumbent
server in serve mode with the env-only counters STRATA_DECODE_TIMING=1, STRATA_VERIFY_PROFILE=1,
STRATA_LOOKAHEAD_STATS=1 (present in the live binary; generate.cpp:7427/14762/15862-15883,
verify.cpp:940) - NO engine instrumentation, NO candidate build.

WHAT THIS SCRIPT DOES
  plan (default)   print the isolated-config diff, the input provenance, the request shape, the exact
                   PRIMARY lifecycle commands (stop/launch/ready/restore) and the exact --execute lines.
                   Opens no sockets, starts nothing.
  --execute        measure against the ISOLATED server (--url, default http://127.0.0.1:8019), one cell,
                   --runs 1..3.  Refused (exit 2) before any request if: the --url port is the production
                   port 8009 (never measured) or differs from the isolated config's port 8019; /health is
                   not service=strata + loaded=true + max_context=262144; /props build_info/model_alias do
                   not match the isolated config; the config's engine exe sha256 is not the primary
                   identity (waive with --expect-sha256 none); the cell inputs' digests/counts do not
                   match cells.sha256.  For every request it writes the RAW result JSON (usage, timings,
                   text, first/total) to build-mixed-gpu/p0/split/<cell>-run<n>.json, the request body +
                   its digest to split/<cell>-run<n>.request.json, and a summary.json with digests and
                   medians.  SIZE POLICY (parent, 2026-10-27): a request whose usage.prompt_tokens differs
                   from the cell size (4096/32768/131072) DOWNGRADES THE LABEL ONLY - the exact-count label
                   becomes "inexact" and the measured count is recorded; sizing proceeds, it does not block
                   the cell (the server's effort patch / template merge may legitimately shift counts:
                   serve/server.py:4466-4469, 4693-4709).  Only a transport/API failure fails a run.
  --selftest       CPU-only mock of every gate and the full request path against an IN-PROCESS stdlib
                   mock server on 127.0.0.1 with an EPHEMERAL port (canned /health, /props and
                   chat-completions).  NO engine, server, GPU, production, production port 8009, real
                   HTTP service - the mock is the test, exactly as the lane's validation allows.

IT NEVER: starts/stops/signals any engine or server, edits the live config, touches port 8009, runs the
engine (not even --help), polls with sleeps, or supervises anything.  Lifecycle is the parent's alone.

INPUTS: the generated cells (build-mixed-gpu/p0/cells, provenance cells.json + cells.sha256) are
reconstructed as MESSAGES: [RECORD.format(i+1) for i < records] + fill_lines + SUMMARY_QUESTION, wrapped
by build_standard_corpus.user_message("P0-<cell>", body) - byte-identical to what p0_generate_cells.py
tokenized, so the server's own render (same pack template + chat_template_kwargs = TEMPLATE_KWARGS,
verified at request build by tools/strata_bench chat + the pack tokenizer when present) can be compared
with the intended token count.  Request: stream, max_tokens 64, temperature 0, seed 1,
reasoning_budget_tokens 0 (overrides the config's 55000 for the measurement), chat_template_kwargs =
TEMPLATE_KWARGS - via tools.strata_bench.chat plus one narrow body-key wrapper.

LIMITS (stated, not promised): the counters are overlapped host-internal per-window means read from the
ISOLATED server's engine log (engine stderr -> the config's log), NOT from this client; the client's
digest() figures (engine-labelled decode_tps/prompt_tps) are per-request sizes.  Sizing gate only -
end-to-end gain stays R3's paired off/on ladder (split-topology.md §4/§7).
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import statistics
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # the worktree root
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from build_standard_corpus import (RECORD, SUMMARY_QUESTION, TEMPLATE_KWARGS, Counter,   # noqa: E402
                                   user_message)
import strata_bench                                  # noqa: E402  chat() + digest()

CELLS = {"4k": 4096, "32k": 32768, "128k": 131072}
PROD_CONFIG = Path("/mnt/user/Strata/prod-config-swift15-dflash-w4-q4_0.json")
PROD_PORT = 8009
ISO_PORT = 8019
ISO_CONFIG = ROOT / "build-mixed-gpu/p0/config-split-timing.json"
CELLS_DIR = ROOT / "build-mixed-gpu/p0/cells"
OUT_DIR = ROOT / "build-mixed-gpu/p0"
READY_FILE = ROOT / "build-mixed-gpu/p0/ready-8019.json"
TIMING_ENV = {"STRATA_DECODE_TIMING": "1", "STRATA_VERIFY_PROFILE": "1", "STRATA_LOOKAHEAD_STATS": "1"}
PRIMARY_ENGINE_SHA256 = "15c8d0b719c87df34505abdf841e1b9e57527ad78515fa8d9e8f86e4b9afbb92"
EXPECTED_CTX = 262144                               # full production budget, no proxy


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def cell_body(info_cell: dict, cell: str) -> str:
    """The exact body p0_generate_cells.py wrote: builder records 1..r, the frozen fill lines, the question."""
    lines = [RECORD.format(i + 1) for i in range(int(info_cell["records"]))]
    lines.extend(info_cell["fill_lines"])
    lines.append(SUMMARY_QUESTION)
    return "\n".join(lines)


def request_messages(cell: str, info_cell: dict) -> list[dict]:
    return user_message(f"P0-{cell}", cell_body(info_cell, cell))


class _BodyJson:
    """Module-level proxy: strata_bench.chat builds its body with json.dumps; this wrapper adds exactly the
    three extra request keys the lane requires (seed, reasoning_budget_tokens, chat_template_kwargs)."""

    def __init__(self, extra: dict):
        self.extra = extra

    @staticmethod
    def loads(s):
        return json.loads(s)

    def dumps(self, obj, *a, **k):
        if isinstance(obj, dict) and "messages" in obj:
            obj = {**obj, **self.extra}
        return json.dumps(obj, *a, **k)


def chat_with_flags(base: str, messages: list[dict], max_tokens: int, timeout: float) -> dict:
    """strata_bench.chat plus the narrow wrapper: the extra request keys the lane requires."""
    extra = {"seed": 1, "reasoning_budget_tokens": 0, "chat_template_kwargs": dict(TEMPLATE_KWARGS)}
    prev = strata_bench.json
    strata_bench.json = _BodyJson(extra)
    try:
        return strata_bench.chat(base, "", messages, max_tokens, think=True, timeout=timeout)
    finally:
        strata_bench.json = prev


def local_token_count(cell: str, info_cell: dict, tokenizer_dir: Path) -> int | None:
    """CPU check with the pack tokenizer: the rendered request must equal the cell size here too."""
    tpl = tokenizer_dir / "chat_template.jinja"
    if not (tokenizer_dir / "vocab.json").is_file() and not tpl.is_file():
        return None
    c = Counter(tokenizer_dir, tpl)
    return len(c.ids(request_messages(cell, info_cell), None))


def load_cells() -> tuple[dict, list[str]]:
    problems: list[str] = []
    try:
        info = json.loads((CELLS_DIR / "cells.json").read_text(encoding="utf-8"))
    except OSError as e:
        return {}, [f"cells.json unreadable: {e}"]
    for line in (CELLS_DIR / "cells.sha256").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        p = CELLS_DIR / name
        if not p.exists():
            problems.append(f"missing {p}")
            continue
        if sha256_file(p) != digest:
            problems.append(f"{p.name}: digest != cells.sha256")
        n = len(p.read_text(encoding="utf-8").replace(",", " ").split())
        cell = name[len("tokens-"):-len(".txt")]
        if cell in CELLS and n != CELLS[cell]:
            problems.append(f"{p.name}: {n} tokens, expected {CELLS[cell]}")
    return info, problems


def http_json(base: str, path: str, timeout: float = 20):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.loads(r.read())


def gates(base: str, iso: dict, expect_sha: str) -> list[str]:
    """Read-only checks.  The ONLY network access in --execute is HTTP to `base` (never port 8009)."""
    problems: list[str] = []
    try:
        h = http_json(base, "/health")
    except Exception as e:                              # noqa: BLE001
        return [f"/health unreachable at {base}: {e!r}"]
    if h.get("service") != "strata" or not h.get("loaded"):
        problems.append(f"/health: service={h.get('service')!r} loaded={h.get('loaded')!r} - the isolated "
                        f"server must be up and the model loaded")
    if h.get("max_context") != EXPECTED_CTX:
        problems.append(f"/health max_context={h.get('max_context')!r}, expected {EXPECTED_CTX} "
                        f"(full 262144 budget, no tiny-context proxy)")
    try:
        pr = http_json(base, "/props")
    except Exception as e:                              # noqa: BLE001
        pr = {}
        problems.append(f"/props unreachable: {e!r}")
    want_build = f"Strata {iso.get('strata_version', '0.1.48')}"
    if pr and "build_info" in pr and pr["build_info"] != want_build:
        problems.append(f"/props build_info={pr['build_info']!r} != {want_build!r}")
    if pr and pr.get("model_alias") not in (None, iso.get("model_name")):
        problems.append(f"/props model_alias={pr.get('model_alias')!r} != config model_name="
                        f"{iso.get('model_name')!r}")
    exe = Path(iso.get("exe", ""))
    if not exe.exists():
        problems.append(f"config engine {exe} does not exist")
    else:
        got = sha256_file(exe)
        if expect_sha == "none":
            print(f"WARNING: engine identity check waived; sha256 = {got}")
        elif got != expect_sha:
            problems.append(f"engine sha256 {got} != primary {expect_sha} (--expect-sha256 none waives)")
    return problems


def run_cell(base: str, cell: str, runs: int, info: dict, timeout: float) -> dict:
    info_cell = info["cells"][cell]
    target = CELLS[cell]
    msgs = request_messages(cell, info_cell)
    rows = []
    (OUT_DIR / "split").mkdir(parents=True, exist_ok=True)
    for n in range(runs):
        body = {"model": "strata", "stream": True, "max_tokens": 64, "temperature": 0, "messages": msgs}
        body_digest = sha256_bytes(json.dumps({**body, "seed": 1, "reasoning_budget_tokens": 0,
                                              "chat_template_kwargs": TEMPLATE_KWARGS},
                                             sort_keys=True).encode())
        r = chat_with_flags(base, msgs, 64, timeout)
        d = strata_bench.digest(r)
        raw = OUT_DIR / "split" / f"{cell}-run{n}.json"
        raw.write_text(json.dumps({"request_body_digest_sha256": body_digest, "target_prompt_tokens": target,
                                   "result": r}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        (OUT_DIR / "split" / f"{cell}-run{n}.request.json").write_text(
            json.dumps({**body, "seed": 1, "reasoning_budget_tokens": 0,
                        "chat_template_kwargs": TEMPLATE_KWARGS}, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        pt = (r.get("usage") or {}).get("prompt_tokens")
        exact = pt == target
        d.update(run=n, prompt_tokens_target=target, exact_size=exact, raw_sha256=sha256_file(raw),
                 request_digest=body_digest)
        rows.append(d)
        status = "ok exact" if d["ok"] and exact else ("ok INEXACT size (label downgraded, recorded)"
                                                       if d["ok"] else "FAILED")
        print(f"  {cell} run {n + 1}: prompt_tokens={pt} (target {target}) generated={d['generated_tokens']} "
              f"ttft={d['ttft_s']}s decode_tps={d['decode_tps']} -> {status}")
    ok_rows = [d for d in rows if d["ok"]]
    all_ok = bool(ok_rows) and len(ok_rows) == len(rows)
    exact_all = all_ok and all(d["exact_size"] for d in rows)
    return {"cell": cell, "target_prompt_tokens": target, "requests": rows,
            "verdict": ("PASS (exact-size)" if exact_all
                        else "PASS (inexact: prompt counts recorded, sizing proceeds)" if all_ok
                        else "FAILED"),
            "size_label": "exact" if exact_all else ("inexact" if all_ok else "failed"),
            "prompt_tokens": stat([d["prompt_tokens"] for d in ok_rows]),
            "ttft_s": stat([d["ttft_s"] for d in ok_rows]),
            "decode_tps": stat([d["decode_tps"] for d in ok_rows]),
            "prompt_tps": stat([d["prompt_tps"] for d in ok_rows])}


def stat(vals):
    v = [x for x in vals if isinstance(x, (int, float))]
    if not v:
        return None
    return {"median": round(statistics.median(v), 3), "min": round(min(v), 3), "max": round(max(v), 3),
            "n": len(v)}


# ---------------------------------------------------------------- the primary's lifecycle (printed, never run)
LIFECYCLE = """# PRIMARY ONLY (the fixer never runs these; no children during the outage):
# 0. measure with production fully UP (isolated server owns the GPUs; 8009 must be free):
#    graceful stop of the CURRENT wrapper by identity (python server.py, production config, 8009) -
#    SIGTERM to the PYTHON wrapper only, never a direct engine kill (the engine is its child):
(sst | grep -F "serve/server.py --engine strata --config %(prod)s --port 8009")   # -> PID (today 3528550; re-check)
kill -TERM <that PID>
#    wait for it and its engine child to exit (their own exit; no kill -9, no signals to the engine):
ss -ltn "sport = :8009"                                                            # empty
# 1. launch the ISOLATED server, same cwd, inherited env; ready-file publish = deterministic readiness:
cd /mnt/user/Strata && .venv/bin/python serve/server.py --engine strata \\
    --config %(iso)s --port 8019 --ready-file %(ready)s
cat %(ready)s      # instance_id/pid/api_url/model_id; then confirm LIVE (stale file is never proof):
#    (curl -s http://127.0.0.1:8019/health)  -> service=strata loaded=true max_context=262144
# 2. measure (the runner prints the exact lines, one cell per call, runs 1..3):
python tools/mixed_gpu/measure_headroom.py --execute --cell 4k   --runs 3
python tools/mixed_gpu/measure_headroom.py --execute --cell 32k  --runs 3
python tools/mixed_gpu/measure_headroom.py --execute --cell 128k --runs 3
#    read the counters in %(log)s (engine stderr): "strata decode timing ... ms/window ... [plan a actq b
#    jobs c CPU d]", per-stage profiles (every stage printed), "actual non-resident experts/layer".
# 3. graceful stop of the isolated wrapper (same recipe, port 8019), remove the ready file if left,
#    then EXACT restore of production:
cd /mnt/user/Strata && .venv/bin/python serve/server.py --engine strata --config %(prod)s --port 8009
"""


def print_plan(iso: dict, problems: list[str]) -> None:
    print("# P0 split-SERVE plan (split-topology.md §4; incumbent binary + env counters, no new build)")
    print(f"# isolated config: {ISO_CONFIG} sha256 {sha256_file(ISO_CONFIG)}")
    print("#   diff vs production config: port 8009->8019, log -> build log, env += "
          + ", ".join(sorted(TIMING_ENV)) + "  (all 17 other keys identical: exe,args,cwd,tokenizer,"
          "model_name,lib_dirs,host,gpu,layer_split,backend,sampling,vision,mcp_servers,reasoning_*)")
    print(f"# engine: {iso.get('exe')} (sha256 gate at --execute; primary identity "
          f"{PRIMARY_ENGINE_SHA256[:12]}…; /health+/props gates below)")
    print(f"# inputs: {CELLS_DIR} (cells.json + cells.sha256; message reconstruction from records+fill)")
    info, probs = load_cells()
    for p in problems + probs:
        print(f"# INPUT/CONFIG PROBLEM: {p}")
    tok_dir = Path(iso.get("tokenizer", ""))
    for cell, meta in info.get("cells", {}).items():
        n = local_token_count(cell, meta, tok_dir) if tok_dir.is_dir() else None
        print(f"# cell {cell}: target {CELLS[cell]} tokens, records {meta['records']} + "
              f"{len(meta['fill_lines'])} fill lines, local render count "
              f"{n if n is not None else 'tokenizer dir missing'}")
    print(f"# request: stream, max_tokens 64, temperature 0, seed 1, reasoning_budget_tokens 0, "
          f"chat_template_kwargs={json.dumps(TEMPLATE_KWARGS, sort_keys=True)} (strata_bench.chat + wrapper)")
    print(f"# gates: --url port must be {ISO_PORT} (never {PROD_PORT}); /health loaded + max_context "
          f"{EXPECTED_CTX}; /props build_info/model_alias; engine sha256; input digests")
    print(f"# outputs: {OUT_DIR}/split/<cell>-run<n>.json (raw) + .request.json + summary.json (digests, medians)")
    print("# a run whose usage.prompt_tokens != target downgrades the exact-size LABEL (counts recorded;")
    print("#   sizing proceeds) - only a transport/API failure fails a run")
    print("# counters are read from the isolated engine log; sizing gate only (no speed promise, R3 owns it)")
    print(LIFECYCLE % {"prod": PROD_CONFIG, "iso": ISO_CONFIG, "ready": READY_FILE,
                       "log": ISO_CONFIG.parent / "engine-split-timing.log"})
    for cell in CELLS:
        print(f"python tools/mixed_gpu/measure_headroom.py --execute --cell {cell} --runs 3 \\")
        print(f"    --url http://127.0.0.1:{ISO_PORT}")


# ------------------------------------------------------------------------- mock selftest (CPU-only proof)
class _Mock(http.server.BaseHTTPRequestHandler):
    exp: dict = {}

    def log_message(self, *a):                          # silence
        pass

    def _send(self, code, payload: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, json.dumps({"status": "ok", "service": "strata", "loaded": self.exp["loaded"],
                                        "max_context": self.exp["ctx"], "model": "swift-1.5-iq3_xxs",
                                        "images": False, "api_key": False, "image_generation": False}).encode())
        elif self.path == "/props":
            self._send(200, json.dumps({"build_info": self.exp["build_info"],
                                        "model_alias": self.exp["model_alias"]}).encode())
        else:
            self._send(404, b"{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        self.exp["last_prompt"] = req
        pt = self.exp["prompt_tokens"]
        chunks = [
            {"choices": [{"delta": {"content": "ok"}}], "usage": None},
            {"choices": [{"delta": {"content": "!"}}],
             "usage": {"prompt_tokens": pt, "completion_tokens": 64,
                       "prompt_tokens_details": {"cached_tokens": 0}},
             "timings": {"prompt_ms": 1000.0, "prompt_per_second": float(pt),
                         "predicted_ms": 2000.0, "predicted_per_second": 32.0,
                         "prompt_n": pt, "draft_n": 300, "draft_n_accepted": 200}},
            {"choices": [], "delta": {}},
        ]
        body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"
        self._send(200, body, "text/event-stream")


def selftest(iso: dict) -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"mock {name}: {'PASS' if cond else 'FAIL'}")

    try:
        _Mock.exp = {"loaded": False, "ctx": EXPECTED_CTX, "build_info": "Strata 0.1.48",
                     "model_alias": "swift-1.5-iq3_xxs"}
        p = gates(base, iso, PRIMARY_ENGINE_SHA256)
        check("gate refuses unloaded /health", any("loaded" in x for x in p))

        _Mock.exp = {"loaded": True, "ctx": 32768, "build_info": "Strata 0.1.48",
                     "model_alias": "swift-1.5-iq3_xxs"}
        p = gates(base, iso, PRIMARY_ENGINE_SHA256)
        check("gate refuses tiny-context /health", any("max_context" in x for x in p))

        _Mock.exp = {"loaded": True, "ctx": EXPECTED_CTX, "build_info": "Strata 9.9.9",
                     "model_alias": "swift-1.5-iq3_xxs"}
        p = gates(base, iso, PRIMARY_ENGINE_SHA256)
        check("gate refuses wrong /props build_info", any("build_info" in x for x in p))

        _Mock.exp = {"loaded": True, "ctx": EXPECTED_CTX, "build_info": "Strata 0.1.48",
                     "model_alias": "swift-1.5-iq3_xxs"}
        p = gates(base, iso, "0" * 64)
        check("gate refuses wrong engine sha256", any("sha256" in x for x in p))
        p = gates(base, iso, PRIMARY_ENGINE_SHA256)
        check("gate accepts healthy mock + primary identity", not p)

        info, probs = load_cells()
        check("cell inputs match cells.sha256", not probs and set(info["cells"]) == set(CELLS))

        local = local_token_count("4k", info["cells"]["4k"], Path(iso["tokenizer"]))
        check("local render of reconstructed 4k message == 4096", local == 4096)

        _Mock.exp["prompt_tokens"] = 4096
        out = run_cell(base, "4k", 2, info, 30)
        check("mock 4k: exact-size PASS, prompt_tokens 4096 both runs",
              out["verdict"] == "PASS (exact-size)"
              and all(d["prompt_tokens"] == 4096 and d["exact_size"] for d in out["requests"]))
        req = _Mock.exp["last_prompt"]
        sent = json.loads((OUT_DIR / "split" / "4k-run0.request.json").read_text())
        check("request carries seed/budget/template kwargs + exact TEMPLATE_KWARGS",
              req.get("seed") == 1 and req.get("reasoning_budget_tokens") == 0
              and req.get("chat_template_kwargs") == TEMPLATE_KWARGS
              and req.get("temperature") == 0 and req.get("max_tokens") == 64 and sent == req)
        check("request tag matches generated cell tag",
              req["messages"][0]["content"].startswith("[case P0-4k]\nrecord000001:"))
        raw = json.loads((OUT_DIR / "split" / "4k-run0.json").read_text())
        check("raw JSON file holds usage + timings + digest fields",
              raw["result"]["usage"]["prompt_tokens"] == 4096 and raw["result"]["timings"]
              and len(raw["raw_sha256"] if "raw_sha256" in raw else raw["request_body_digest_sha256"]) == 64)

        _Mock.exp["prompt_tokens"] = 4095
        out = run_cell(base, "4k", 1, info, 30)
        check("mock size mismatch: label downgraded to inexact, sizing proceeds (not blocked)",
              out["verdict"].startswith("PASS (inexact") and out["size_label"] == "inexact"
              and out["requests"][0]["exact_size"] is False and out["requests"][0]["prompt_tokens"] == 4095)
    finally:
        srv.shutdown()
    fails = [n for n, ok in results if not ok]
    print(f"selftest: {len(results) - len(fails)} pass, {len(fails)} fail")
    return 0 if not fails else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=ISO_CONFIG, help="the ISOLATED config (read only)")
    ap.add_argument("--url", default=f"http://127.0.0.1:{ISO_PORT}", help="the isolated server address")
    ap.add_argument("--cell", choices=list(CELLS), help="cell to measure with --execute")
    ap.add_argument("--runs", type=int, default=3, help="1..3 requests per cell")
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--expect-sha256", default=PRIMARY_ENGINE_SHA256, help="'none' waives (loud warning)")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="CPU mock of the gates + request path")
    a = ap.parse_args(argv)

    iso = json.loads(a.config.read_text(encoding="utf-8"))
    cfg_problems = []
    if int(iso.get("port", 0)) != ISO_PORT:
        cfg_problems.append(f"isolated config port={iso.get('port')} != {ISO_PORT}")
    for k in TIMING_ENV:
        if iso.get("env", {}).get(k) != "1":
            cfg_problems.append(f"isolated config env missing {k}=1")

    if a.selftest:
        return selftest(iso)
    if not a.execute:
        print_plan(iso, cfg_problems)
        return 0

    if a.cell is None:
        print("--execute needs --cell 4k|32k|128k", file=sys.stderr)
        return 2
    if not (1 <= a.runs <= 3):
        print("--runs must be 1..3 (outage budget)", file=sys.stderr)
        return 2
    url_port = int(a.url.rsplit(":", 1)[-1])
    if url_port == PROD_PORT:
        print(f"REFUSED: port {PROD_PORT} is production - the P0 measurement never runs there.", file=sys.stderr)
        return 2
    if url_port != ISO_PORT:
        print(f"REFUSED: --url port {url_port} is not the isolated port {ISO_PORT}", file=sys.stderr)
        return 2
    for p in cfg_problems:
        print(f"REFUSED: {p}", file=sys.stderr)
        return 2
    info, probs = load_cells()
    for p in probs:
        print(f"REFUSED: {p}", file=sys.stderr)
        return 2
    problems = gates(a.url.rstrip("/"), iso, a.expect_sha256)
    if problems:
        for p in problems:
            print(f"REFUSED: {p}", file=sys.stderr)
        return 2

    out = run_cell(a.url.rstrip("/"), a.cell, a.runs, info, a.timeout)
    (OUT_DIR / "split" / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n",
                                                    encoding="utf-8")
    print(f"{a.cell}: {out['verdict']}; medians: prompt_tokens {out['prompt_tokens']}, ttft {out['ttft_s']} s, "
          f"decode_tps {out['decode_tps']} (engine-labelled), prompt_tps {out['prompt_tps']}")
    print(f"summary: {OUT_DIR / 'split' / 'summary.json'}; read the stage counters from "
          f"{ISO_CONFIG.parent / 'engine-split-timing.log'}")
    return 0 if out["verdict"].startswith("PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())

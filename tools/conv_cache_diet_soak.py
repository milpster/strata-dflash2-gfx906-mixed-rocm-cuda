#!/usr/bin/env python3
"""Round-6 diet-lane soak: bound the conversation-cache side, keep the tps floor.

Compressed (~10-12 min), operator-run against a booted engine.  It does NOT boot anything.
Phases, in order, each with its own memory leg (the ab9 driver's measures: max VmHWM after a
clear_refs reset = lower-better max_rss, min MemAvailable = higher-better, max swap in use):
  a) grow ONE conversation to 120k+ ctx through ~12 sequential 8-10k-token deltas, tiny gens
  b) ~10 idle->request cycles at full size (park/restore hammering)
  c) LRU pass: two extra small conversations interleaved, then touched out of order
  d) tg probe at full ctx, then the assertions

Gate 1 (performance): tg probe >= --tg-floor at full ctx, and no `slow read context` event.
Gate 2 (memory): engine RSS (VmHWM sum) <= --rss-budget-gib at the 120k+ legs, host avail floor
>= --avail-floor-gib, the parked generation's resident pages ~0, the RAM tier's parked bytes
<= the ceiling (the tier holds no over-ceiling snapshot: "parked to disk only" lines cover those).
Restore beats re-read: every cycle after a park shows reused>0 and NO "reading the prompt again".

Usage:
  python3 conv_cache_diet_soak.py --url http://127.0.0.1:8019/v1/chat/completions \
      --elog <serve stderr log> --cache-dir <conversation-cache dir> --ram-ceiling-mib 512 \
      [--rss-budget-gib 40] [--avail-floor-gib 15] [--tg-floor 34] [--dry-run]
"""
import argparse, json, os, random, re, sys, threading, time, urllib.request
from pathlib import Path

WORDS = ("cache eviction policy ring buffer page table slot arena transfer prefill layer "
         "expert token stream socket reply frame claim row vector hidden tensor block copy "
         "engine helper window pinned queue lock begin finish prompt tier device stream "
         "kernel copy engine token expert layer prefill socket reply claim row hidden block").split()

URL = "http://127.0.0.1:8009/v1/chat/completions"  # overridden by --url; never the default in a run


def build_delta(seed: int, n_bytes: int) -> str:
    """Seeded filler padded to EXACTLY n_bytes UTF-8 (unique per seed -> no cache reuse)."""
    rng = random.Random(seed)
    parts, total = [], 0
    while total < n_bytes:
        line = " ".join(rng.choice(WORDS) for _ in range(9)) + "\n"
        parts.append(line); total += len(line.encode())
    text = "".join(parts)[: n_bytes - 1]
    text = text.encode()[: n_bytes].decode("utf-8", errors="ignore")
    text += "." * (n_bytes - len(text.encode()))
    assert len(text.encode()) == n_bytes
    return text


def post(url, prompt, max_tokens, timeout=300):
    body = json.dumps({"messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": 0, "enable_thinking": False,
                       "reasoning_effort": "low", "stream": False}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    wall = time.time() - t0
    ch = resp["choices"][0]
    m = ch["message"]
    text = m.get("content") or m.get("reasoning_content") or ""
    usage = resp.get("usage") or {}
    ct = usage.get("completion_tokens")
    pt = usage.get("prompt_tokens")
    return wall, text, ct, pt


def mem_available_gib():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1048576.0
    return float("nan")


def swap_used_gib():
    total = free = 0
    for line in open("/proc/meminfo"):
        if line.startswith("SwapTotal:"):
            total = int(line.split()[1])
        elif line.startswith("SwapFree:"):
            free = int(line.split()[1])
    return (total - free) / 1048576.0


def engine_pids(patterns):
    pids = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "ignore")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if any(p in cmd for p in patterns):
            pids.append(pid)
    return pids


def reset_hwm(patterns):
    """Reset every engine process's peak-RSS counter (VmHWM) so the leg measures its own max."""
    for pid in engine_pids(patterns):
        try:
            with open(f"/proc/{pid}/clear_refs", "w") as f:
                f.write("5")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass


def rss_of(patterns, hwm=False):
    """Sum VmRSS (or the peak VmHWM) in GiB of every process whose cmdline matches a pattern."""
    key = "VmHWM:" if hwm else "VmRSS:"
    total = 0.0
    for pid in engine_pids(patterns):
        try:
            for line in open(f"/proc/{pid}/status"):
                if line.startswith(key):
                    total += int(line.split()[1]) / 1048576.0
                    break
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return total


def resident_bytes(path):
    """Bytes resident in the page cache for `path` (mincore over its pages)."""
    import ctypes
    PAGE = 4096
    size = os.path.getsize(path)
    if size == 0:
        return 0
    fd = os.open(path, os.O_RDONLY)
    try:
        mmap = ctypes.CDLL("libc.so.6", use_errno=True).mmap
        mmap.restype = ctypes.c_void_p
        addr = mmap(None, size, 0x1, 0x02, fd, 0)   # PROT_READ, MAP_SHARED
        if addr in (None, ctypes.c_void_p(-1).value):
            return 0
        nvec = (size + PAGE - 1) // PAGE
        vec = (ctypes.c_ubyte * nvec)()
        m = ctypes.CDLL("libc.so.6", use_errno=True).mincore
        if m(ctypes.c_void_p(addr), ctypes.c_size_t(size), vec) != 0:
            return 0
        return sum(1 for i in range(nvec) if vec[i] & 1) * PAGE
    finally:
        os.close(fd)


def newest_parked(cache_dir):
    p = Path(cache_dir)
    cands = sorted(p.rglob("*.sess"), key=lambda f: f.stat().st_mtime, reverse=True)
    return str(cands[0]) if cands else None


class Sampler(threading.Thread):
    def __init__(self, patterns, every=0.5):
        super().__init__(daemon=True); self.patterns = patterns; self.every = every
        self.rss = []; self.avail = []; self.swap = []; self.stop = False
    def run(self):
        while not self.stop:
            self.rss.append(rss_of(self.patterns)); self.avail.append(mem_available_gib())
            self.swap.append(swap_used_gib())
            time.sleep(self.every)


def log_tail_slice(path, since_bytes):
    """Read the log from the byte offset recorded at phase start (no cross-phase bleed)."""
    if not os.path.exists(path):
        return ""
    with open(path, "r", errors="ignore") as f:
        f.seek(since_bytes); return f.read()


PROMPT_RE = re.compile(r"prompt (\d+) tokens = (\d+) reused \+ (\d+) read")
PARK_RE = re.compile(r"conversation cache: parked (\d+) tokens.*bytes=(\d+)")
DISK_ONLY_RE = re.compile(r"parked to disk only")
REREAD_RE = re.compile(r"reading the prompt again")
COLD_RE = re.compile(r"mounting target-only with a cold drafter")
RESTORE_RE = re.compile(r"conversation cache: restored (\d+) tokens")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=URL)
    ap.add_argument("--elog", required=True, help="serve engine stderr log")
    ap.add_argument("--cache-dir", required=True, help="conversation-cache dir (parked .sess files)")
    ap.add_argument("--rss-pattern", action="append", default=[],
                    help="substring of the engine/helper cmdline to sum RSS over (default: strata + strata-expert-helper)")
    ap.add_argument("--rss-budget-gib", type=float, default=40.0)
    ap.add_argument("--avail-floor-gib", type=float, default=15.0)
    ap.add_argument("--tg-floor", type=float, default=34.0)
    ap.add_argument("--ram-ceiling-mib", type=int, default=512,
                    help="the engine's --conversation-cache-ram-mib: the RAM tier's bytes must stay under it")
    ap.add_argument("--grow-steps", type=int, default=24, help="max deltas; stops once --grow-target-tokens is measured")
    ap.add_argument("--grow-bytes", type=int, default=26000, help="bytes per delta (~8-10k tokens at the iq3xxs ratio)")
    ap.add_argument("--grow-target-tokens", type=int, default=120000)
    ap.add_argument("--cycles", type=int, default=10)
    ap.add_argument("--park-page-gib-limit", type=float, default=0.6,
                    help="max GiB of the parked file allowed to stay resident after its park write")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit (no requests)")
    a = ap.parse_args()
    patterns = a.rss_pattern or ["strata", "strata-expert-helper"]

    if a.dry_run:
        print("dry-run: request plan")
        print(f"  grow: {a.grow_steps} deltas x {a.grow_bytes} B -> ~{a.grow_steps*a.grow_bytes} B "
              f"(~{a.grow_steps*a.grow_bytes//4} tok) at max_tokens 1-8")
        print(f"  cycles: {a.cycles} idle->request at full ctx (park/restore hammering)")
        print(f"  LRU: two small conversations interleaved, touched out of order")
        print(f"  budget: RSS<= {a.rss_budget_gib} GiB, avail>= {a.avail_floor_gib} GiB, tg>= {a.tg_floor} tps, "
              f"zero-gen==0, RAM tier bytes <= {a.ram_ceiling_mib} MiB, reread lines == 0")
        return 0

    sampler = Sampler(patterns); sampler.start()
    report = {"phases": {}, "tg_tps": None, "parked_file": None, "park_page_resident_gib": None,
              "avail_min_gib": mem_available_gib(), "rss_max_gib": 0.0, "zero_gen": 0,
              "slow_reads": 0, "rereads": 0, "cold_mounts": 0, "restores": 0, "disk_only_parks": 0,
              "tier_bytes_max_mib": 0, "cycles_reused": [], "boot_lines": [], "park_lines": []}
    ok = True
    def note_fail(msg):
        nonlocal ok; ok = False; print("FAIL:", msg)

    def leg(name, log_off):
        """Memory leg for one phase: peak RSS (VmHWM since the reset), min avail, max swap."""
        hwm = rss_of(patterns, hwm=True)
        report["phases"].setdefault(name, {})["max_rss_mib"] = round(hwm * 1024, 1)
        report["phases"][name]["min_avail_mib"] = round(min(sampler.avail[-40:] or [mem_available_gib()]) * 1024, 1)
        report["phases"][name]["max_swap_used_mib"] = round(max(sampler.swap[-40:] or [0.0]) * 1024, 1)
        tail = log_tail_slice(a.elog, log_off)
        return tail

    # (a) grow to 120k+ ctx: ~12 sequential deltas, tiny gens; keep appending while under target
    log_off = os.path.getsize(a.elog) if os.path.exists(a.elog) else 0
    reset_hwm(patterns)
    grow_off = log_off
    prompt = ""
    full_prompt = ""
    pt = 0
    for i in range(a.grow_steps):
        prompt += build_delta(1000 + i, a.grow_bytes)
        mt = 1 + (i % 8)
        wall, text, ct, pt = post(a.url, prompt, mt)
        report["phases"].setdefault("grow", {}).setdefault("requests", []).append(
            {"i": i, "bytes": len(prompt.encode()), "pt": pt, "ct": ct, "wall": round(wall, 2)})
        if ct == 0: report["zero_gen"] += 1
        rss = rss_of(patterns)
        report["rss_max_gib"] = max(report["rss_max_gib"], rss)
        report["avail_min_gib"] = min(report["avail_min_gib"], mem_available_gib())
        print(f"  grow[{i}] ctx={len(prompt.encode())} pt={pt} ct={ct} wall={wall:.2f}s "
              f"rss={rss:.1f}GiB avail={mem_available_gib():.1f}GiB")
        if pt >= a.grow_target_tokens:
            break
    full_prompt = prompt
    small_a, small_b = build_delta(9001, 3000), build_delta(9002, 3000)

    grow_tail = leg("grow", grow_off)
    for m in PARK_RE.finditer(grow_tail):
        report["tier_bytes_max_mib"] = max(report["tier_bytes_max_mib"], int(m.group(2)) >> 20)
    report["disk_only_parks"] += len(DISK_ONLY_RE.findall(grow_tail))
    report["park_lines"] = [l for l in grow_tail.splitlines() if "parked" in l][-6:]
    report["slow_reads"] += sum(1 for l in grow_tail.splitlines() if "slow read context" in l)

    # (b) idle->request cycles at full size; watch the newest parked file's resident pages
    parked = newest_parked(a.cache_dir)
    report["parked_file"] = parked
    park_resident = []
    cycle_off = os.path.getsize(a.elog) if os.path.exists(a.elog) else 0
    reset_hwm(patterns)
    for i in range(a.cycles):
        wall, text, ct, pt = post(a.url, full_prompt, 2)
        if ct == 0: report["zero_gen"] += 1
        pf = newest_parked(a.cache_dir)
        if pf: park_resident.append(resident_bytes(pf) / 1073741824.0)
        rss = rss_of(patterns)
        report["rss_max_gib"] = max(report["rss_max_gib"], rss)
        report["avail_min_gib"] = min(report["avail_min_gib"], mem_available_gib())
        print(f"  cycle[{i}] pt={pt} ct={ct} wall={wall:.2f}s rss={rss:.1f}GiB avail={mem_available_gib():.1f}GiB")
    if park_resident:
        report["park_page_resident_gib"] = max(park_resident)

    cycle_tail = leg("cycles", cycle_off)
    for m in PROMPT_RE.finditer(cycle_tail):
        report["cycles_reused"].append(int(m.group(2)))
    report["rereads"] += len(REREAD_RE.findall(cycle_tail))
    report["cold_mounts"] += len(COLD_RE.findall(cycle_tail))
    report["restores"] += len(RESTORE_RE.findall(cycle_tail))
    report["slow_reads"] += sum(1 for l in cycle_tail.splitlines() if "slow read context" in l)
    report["zero_gen"] += sum(1 for l in cycle_tail.splitlines() if "0 generated in 0 ms" in l)
    for m in PARK_RE.finditer(cycle_tail):
        report["tier_bytes_max_mib"] = max(report["tier_bytes_max_mib"], int(m.group(2)) >> 20)
    report["disk_only_parks"] += len(DISK_ONLY_RE.findall(cycle_tail))

    # (c) LRU pass: two small conversations interleaved, touched out of order
    lru_off = os.path.getsize(a.elog) if os.path.exists(a.elog) else 0
    report["phases"].setdefault("lru", {})
    report["drop_lines"] = []
    reset_hwm(patterns)
    for i in range(3):
        for small in (small_a, small_b):
            wall, text, ct, pt = post(a.url, small + full_prompt[:4000], 2)
            if ct == 0: report["zero_gen"] += 1
        report["phases"]["lru"].setdefault("rounds", []).append({"i": i})
        rss = rss_of(patterns)
        report["rss_max_gib"] = max(report["rss_max_gib"], rss)
        report["avail_min_gib"] = min(report["avail_min_gib"], mem_available_gib())
    # touch out of order: the older small conversation, then the active full one
    post(a.url, small_b + full_prompt[:4000], 2)
    post(a.url, full_prompt, 4)
    lru_tail = leg("lru", lru_off)
    report["rereads"] += len(REREAD_RE.findall(lru_tail))
    report["drop_lines"] = [l for l in lru_tail.splitlines() if "dropped" in l][-4:]

    # (d) tg probe at full ctx: same prompt twice, cached run -> tokens/sec
    probe_off = os.path.getsize(a.elog) if os.path.exists(a.elog) else 0
    reset_hwm(patterns)
    post(a.url, full_prompt, 8)  # warm
    t0 = time.time()
    wall, text, ct, pt = post(a.url, full_prompt, 128)
    report["tg_tps"] = (ct / wall) if (wall > 0 and ct) else 0.0
    report["rss_max_gib"] = max(report["rss_max_gib"], rss_of(patterns))
    report["avail_min_gib"] = min(report["avail_min_gib"], mem_available_gib())
    probe_tail = leg("probe", probe_off)
    report["rereads"] += len(REREAD_RE.findall(probe_tail))
    sampler.stop = True

    # the serve-log lines the judge reads
    tail = log_tail_slice(a.elog, log_off)
    report["boot_lines"] = [l for l in tail.splitlines()
                            if "conversation cache: RAM ceiling" in l or "file page-cache release ON" in l
                            or "kv-resident-mib" in l][:6]
    report["park_lines"] += [l for l in tail.splitlines() if "parked" in l][-4:]

    print("\n=== soak report ===")
    print(json.dumps(report, indent=2))

    # gate 2: the memory bound
    if report["avail_min_gib"] < a.avail_floor_gib:
        note_fail(f"avail floor {report['avail_min_gib']:.1f} GiB < {a.avail_floor_gib}")
    if report["rss_max_gib"] > a.rss_budget_gib:
        note_fail(f"engine RSS {report['rss_max_gib']:.1f} GiB > budget {a.rss_budget_gib}")
    for name, leg_data in report["phases"].items():
        if isinstance(leg_data, dict) and leg_data.get("max_rss_mib", 0) > a.rss_budget_gib * 1024:
            note_fail(f"leg {name} max_rss {leg_data['max_rss_mib']:.0f} MiB > {a.rss_budget_gib * 1024:.0f}")
    if report["park_page_resident_gib"] is not None and report["park_page_resident_gib"] > a.park_page_gib_limit:
        note_fail(f"parked-file page-cache resident {report['park_page_resident_gib']:.2f} GiB > "
                  f"{a.park_page_gib_limit} (release not effective)")
    if report["tier_bytes_max_mib"] > a.ram_ceiling_mib + 64:
        note_fail(f"RAM tier bytes peaked {report['tier_bytes_max_mib']} MiB > ceiling {a.ram_ceiling_mib} "
                  f"(the tier accumulated snapshots: park-once is broken)")
    # gate 1: the performance floor
    if report["tg_tps"] < a.tg_floor:
        note_fail(f"tg probe {report['tg_tps']:.1f} tps < {a.tg_floor}")
    if report["slow_reads"] > 0:
        note_fail(f"slow read context events = {report['slow_reads']} (expected 0)")
    if report["rereads"] > 0:
        note_fail(f"'reading the prompt again' events = {report['rereads']} (restore must beat re-read)")
    if any(r == 0 for r in report["cycles_reused"][1:]):
        note_fail(f"a re-parked conversation re-read its prompt: cycles reused = {report['cycles_reused']}")
    if report["zero_gen"] != 0:
        note_fail(f"zero-gen events = {report['zero_gen']} (expected 0)")

    print("\nSOAK: PASS" if ok else "\nSOAK: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

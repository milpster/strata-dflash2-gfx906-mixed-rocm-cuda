#!/usr/bin/env python3
"""Port the fork's serve-side mixed-lane behaviors onto upstream 0.2.18's split server.

Upstream 0.2.18 rewrote serve/server.py (+1,324/-2,941) and moved part of it into
serve/server_parts/{base,engines}.py, so the fork's +217-line delta cannot be applied as a
patch. Every change below is anchored to upstream's own text and asserted: an anchor that
appears other than exactly once stops the script instead of guessing. Re-running is a no-op
(each edit checks its own marker first).

Usage: python3 tools-port/port-server.py [--check]   (--check reports without writing)
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "terminal/serve/server.py"
BASE = ROOT / "terminal/serve/server_parts/base.py"
ENG = ROOT / "terminal/serve/server_parts/engines.py"
CHECK = "--check" in sys.argv

DISK_SNIPPET = (ROOT / "tools-port/snips/convcache_disk.py").read_text(encoding="utf-8")
HELPER_SNIPPET = (ROOT / "tools-port/snips/expert_helper_args.py").read_text(encoding="utf-8")


class Edit:
    def __init__(self, path, marker, old, new, why):
        self.path, self.marker, self.old, self.new, self.why = path, marker, old, new, why


def apply(path, edits):
    text = path.read_text(encoding="utf-8")
    changed = 0
    for e in edits:
        if e.marker in text:
            print(f"  already present: {e.why}")
            continue
        n = text.count(e.old)
        if n != 1:
            sys.exit(f"ANCHOR NOT UNIQUE ({n}x) in {path.name}: {e.why}\n  anchor: {e.old[:90]!r}")
        text = text.replace(e.old, e.new, 1)
        changed += 1
        print(f"  applied: {e.why}")
    if changed and not CHECK:
        path.write_text(text, encoding="utf-8")
    return changed


def import_insert(path, block_header, name):
    """Insert `name` into a parenthesized `from ... import (...)` block, keeping its sort order."""
    text = path.read_text(encoding="utf-8")
    before = text.count("\n") + 1
    first = text.split("\n")[0]
    if f"\n    {name},\n" in text:
        print(f"  already present: import {name}")
        return 0
    i = text.index(block_header)
    j = text.index("\n)\n", i)
    block, tail = text[i:j], text[j:]
    lines = block.split("\n")
    if f"    {name}," in lines:
        print(f"  already present: import {name}")
        return 0
    body = [ln for ln in lines[1:] if ln.strip()]
    entry = f"    {name},"
    # Sorted-insert, never a re-sort: upstream's own order stays byte-identical, only the new line moves in.
    pos = len(body)
    for k, ln in enumerate(body):
        if ln.strip().rstrip(",") > name:
            pos = k
            break
    body.insert(pos, entry)
    assert len(body) == sum(1 for ln in lines[1:] if ln.strip()) + 1, (name, "a body line was lost")
    if not CHECK:
        new = text[:i] + "\n".join([lines[0]] + body) + tail
        assert new.split("\n")[0] == first and new.count("\n") + 1 == before + 1, (name, "file shape changed")
        path.write_text(new, encoding="utf-8")
    print(f"  applied: import {name}")
    return 1


# ---------------------------------------------------------------- server_parts/base.py
apply(BASE, [Edit(
    BASE, "DCACHE_PHASES = frozenset",
    'SESSION_WAIT_MAX_S = 3600\n',
    'SESSION_WAIT_MAX_S = 3600\n\n'
    "# The automatic disk tier's typed progress lines (`DCACHE <phase> <done> <total> [<secs>]`): disk bytes on\n"
    "# stdout, understood by the request parser and the manual SAVE/RESTORE parser.  They are heartbeats and never\n"
    "# prompt evaluation: only PP and RESUME feed the server's prompt statistics.\n"
    'DCACHE_PHASES = frozenset(("write", "flush", "publish", "validate", "transfer", "done"))\n',
    "DCACHE_PHASES constant")])

# ------------------------------------------------------------- server_parts/engines.py
helper_src = HELPER_SNIPPET.rstrip("\n")
apply(ENG, [
    Edit(ENG, "something else (a hostapp, another",
         "something else (a game, another",
         "something else (a hostapp, another",
         "GpuBusy wording"),
    Edit(ENG, "the same for the automatic disk tier",
         "    --conversation-cache-mib: budget, slots, what its log says) and how much of the prompts the cache gave back.\"\"\"",
         "    --conversation-cache-mib: budget, slots, what its log says), the same for the automatic disk tier (its own\n"
         "    budget, folder, what it holds and its counters, all from the engine's log) and how much of the prompts the\n"
         "    cache gave back.\"\"\"",
         "conversation_cache_view docstring (the disk state rides through **parked)"),
    Edit(ENG, "def expert_helper_args(",
         '    return ["--pool-cores", text]\n\n\ndef profile_shape(',
         '    return ["--pool-cores", text]\n\n\n' + helper_src + '\n\n\ndef profile_shape(',
         "expert_helper_args next to parallel_args/pool_cores_args"),
])

# ------------------------------------------------------------------------- server.py
DCACHE_LOOP = (
    '                elif line.startswith("DCACHE "):        # the disk tier\'s transfer: disk bytes, a heartbeat only\n'
    "                    self._note_disk_line(line)\n"
    "                    yield None\n")

apply(S, [
    # --- the conversation-cache card learns the disk tier -------------------------------
    Edit(S, "DISK_ON = re.compile",
         '    which is the log): read on from where it was last read, from the start of the engine\'s current run."""',
         '    which is the log): read on from where it was last read, from the start of the engine\'s current run.\n\n'
         "    The automatic DISK tier's lines are read here too: its ON/off line names the folder, transfer mode,\n"
         "    budget and what the restart inventory found; its park/restore lines the generations, their bytes, the\n"
         "    evictions and supersessions, the pair hits and refusals.  Only counts and short reason codes - never\n"
         '    prompt contents."""',
         "ConvCacheLog docstring"),
    Edit(S, "DISK_ON = re.compile",
         '    REASONS = (("gave no memory back", "ram_no_progress"), ("physical RAM admission", "ram_floor"),\n'
         '               ("RAM floor after capture", "ram_after_capture"), ("exceeds available budget", "budget"),\n'
         '               ("below the", "short"), ("stage CUDA", "estimate"))\n',
         '    REASONS = (("gave no memory back", "ram_no_progress"), ("physical RAM admission", "ram_floor"),\n'
         '               ("RAM floor after capture", "ram_after_capture"), ("exceeds available budget", "budget"),\n'
         '               ("below the", "short"), ("stage CUDA", "estimate"))\n'
         "    # the disk tier: the tier's own words, matched by their shape, and the tier's concise refusal code\n"
         '    DISK_ON = re.compile(r"conversation cache: disk tier ON at (\\S+) \\((direct I/O|buffered), budget (\\d+) MiB\\): "\n'
         '                         r"generations=(\\d+) bytes=(\\d+) sidecars=(\\d+) orphan temps=(\\d+) orphan sidecars=(\\d+) "\n'
         '                         r"corrupt=(\\d+) foreign=(\\d+)")\n'
         '    DISK_OFF = re.compile(r"conversation cache: the disk tier stays off \\((.*)\\)")\n'
         '    DISK_PARK = re.compile(r"conversation cache: disk park streamed (\\d+) tokens(.*?) to (\\S+) in ([\\d.]+) ms; "\n'
         '                           r"generations=(\\d+) bytes=(\\d+) evicted=(\\d+) superseded=(\\d+) sidecars=(\\d+) "\n'
         '                           r"anchor=(-?\\d+) pair=(-?\\d+) tail-draft=([01])")\n'
         '    DISK_RESTORE = re.compile(r"conversation cache: restored (\\d+) tokens from disk (\\S*) in ([\\d.]+) ms: (.*)$")\n'
         '    DISK_SKIP = re.compile(r"conversation cache: disk (park|restore) skipped \\((.*)\\)")\n'
         '    DISK_REFUSAL = re.compile(r"disk cache: ([a-z-]+) refusal")\n'
         '    DISK_SERIAL = re.compile(r"conv-(\\d+)\\.sess")\n',
         "ConvCacheLog disk regexes"),
    Edit(S, "def disk_state()",
         "    def __init__(self):\n",
         DISK_SNIPPET + "\n    def __init__(self):\n",
         "disk_state() / snapshot()"),
    Edit(S, '"disk": self.disk_state()}',
         '                      "last_tokens": None, "last_at": None, "refused": {}, "refused_total": 0, "last_refused": None}',
         '                      "last_tokens": None, "last_at": None, "refused": {}, "refused_total": 0, "last_refused": None,\n'
         '                      "disk": self.disk_state()}',
         "reset() carries the disk state"),
    Edit(S, "            return self.snapshot()\n        if self.key",
         "        if not path or start is None:\n            return dict(self.state)",
         "        if not path or start is None:\n            return self.snapshot()",
         "_poll returns a copy (1/2)"),
    Edit(S, "        except OSError:\n            return self.snapshot()",
         "        except OSError:\n            return dict(self.state)",
         "        except OSError:\n            return self.snapshot()",
         "_poll returns a copy (2/2)"),
    # Upstream moved the #879 CLEARED test into the "not a cache line" branch, so the disk hook goes at the top of
    # the loop: the tier's lines are claimed before the RAM-cache regexes, and CLEARED keeps working unchanged.
    Edit(S, "            if self._disk_line(line, now):",
         "        for line in data[:end].decode(\"utf-8\", \"replace\").splitlines():\n"
         '            if "conversation cache:" not in line:\n',
         "        for line in data[:end].decode(\"utf-8\", \"replace\").splitlines():\n"
         "            if self._disk_line(line, now):\n                continue\n"
         '            if "conversation cache:" not in line:\n',
         "_poll folds the disk lines"),
    Edit(S, "    def _disk_line(",
         "                self._refused(next((r for k, r in self.REASONS if k in text), \"estimate\" if text else \"other\"))\n"
         "        return dict(self.state)",
         "                self._refused(next((r for k, r in self.REASONS if k in text), \"estimate\" if text else \"other\"))\n"
         "        return self.snapshot()\n\n"
         + (ROOT / "tools-port/snips/convcache_diskline.py").read_text(encoding="utf-8").rstrip("\n") + "\n",
         "_disk_line() / _refusal()"),
    # --- the engine's DCACHE progress line ----------------------------------------------
    Edit(S, "self.disk_io = None              #",
         "        self.reused = 0                  # RESUME: prompt tokens not read again (a client takes them out of the work)\n",
         "        self.reused = 0                  # RESUME: prompt tokens not read again (a client takes them out of the work)\n"
         "        self.disk_io = None              # the disk tier's last DCACHE line: {\"phase\",\"done\",\"total\",\"ms\"}\n",
         "StrataEngine.disk_io"),
    Edit(S, "def _note_disk_line(self",
         "    def _parse_done(self, line):\n",
         (ROOT / "tools-port/snips/note_disk_line.py").read_text(encoding="utf-8").rstrip("\n")
         + "\n\n    def _parse_done(self, line):\n",
         "_note_disk_line()"),
    Edit(S, '                elif line.startswith("DCACHE "):',
         "                elif line.startswith(\"DONE\"):\n                    self._parse_done(line)\n",
         DCACHE_LOOP + "                elif line.startswith(\"DONE\"):\n                    self._parse_done(line)\n",
         "DCACHE accepted in the request read loop"),
    Edit(S, "        self.disk_io = None\n        born = self.gen",
         "        self.progress, self.progress_ms, self.reused = None, 0, 0\n        born = self.gen",
         "        self.progress, self.progress_ms, self.reused = None, 0, 0\n        self.disk_io = None\n        born = self.gen",
         "the disk line is cleared per request"),
    # --- the manual SAVE/RESTORE watcher understands DCACHE too ------------------------
    Edit(S, "        dcache: dict = {}",
         "        last_done = -1\n        allow = 0.0",
         "        last_done = -1\n        dcache: dict = {}        # the disk tier's last `done` per phase (its progress must be monotonic)\n        allow = 0.0",
         "the save/restore watcher's dcache state"),
    Edit(S, "            if head == \"DCACHE\":",
         "            if head == \"SWAIT\":                            # a blocking step starts: its bounded allowance\n",
         "            if head == \"DCACHE\":                       # the automatic disk tier's transfer (a park's write/flush/\n"
         "                # publish, a restore's validate/transfer): progress and a heartbeat, with SESSION's rules - the\n"
         "                # tier's own phase, done never past total and never backwards within it, a finite time.  It buys\n"
         "                # no extra allowance: a disk step that stops is timed out like any other silence.\n"
         "                try:\n"
         "                    if len(f) not in (4, 5) or f[1] not in DCACHE_PHASES:\n"
         "                        raise ValueError\n"
         "                    done, total = count(f[2]), count(f[3])\n"
         "                    secs = float(f[4]) if len(f) == 5 else 0.0\n"
         "                    if done > total or done < dcache.get(f[1], -1) or not math.isfinite(secs) or secs < 0.0:\n"
         "                        raise ValueError\n"
         "                except ValueError:\n"
         "                    raise out_of_step(line) from None\n"
         "                dcache[f[1]] = done\n"
         "                self._note_disk_line(line)\n"
         "                heard, allow = time.monotonic(), 0.0\n"
         "                continue\n"
         "            if head == \"SWAIT\":                            # a blocking step starts: its bounded allowance\n",
         "DCACHE in the save/restore watcher"),
    # --- the drafter's per-request block length -----------------------------------------
    Edit(S, 'keys += f" dflash_block={block}"',
         "                keys += f\" aux_cpus={int(ac)}\"\n",
         "                keys += f\" aux_cpus={int(ac)}\"\n"
         "            block = tune.get(\"dflash_block\")\n"
         "            if isinstance(block, int) and not isinstance(block, bool) and 1 <= block <= 7:\n"
         "                keys += f\" dflash_block={block}\"\n",
         "the dflash_block request key"),
    # --- what the status and the log show ----------------------------------------------
    Edit(S, "            live[\"disk_io\"] = disk_io",
         "        if state == \"reading\" and progress:\n            live[\"prompt_read\"], live[\"prompt_total\"] = progress\n",
         "        if state == \"reading\" and progress:\n            live[\"prompt_read\"], live[\"prompt_total\"] = progress\n"
         "        disk_io = getattr(self.engine, \"disk_io\", None)\n"
         "        if disk_io and s.get(\"busy\"):                   # the disk tier is moving bytes for this request\n"
         "            live[\"disk_io\"] = disk_io\n",
         "the disk transfer shows in the live status"),
    Edit(S, "helper_at = done_line.find(\" helper=\")",
         "                            rate_txt = f\"{rate:.1f} tok/s\" if rate is not None else \"no speed\"\n"
         "                            print(f\"[strata] done: {n} tokens in {el:.0f} s ({rate_txt}) \"\n"
         "                                  f\"({finish}, cancel={cancel.is_set()}){hit_msg}\", flush=True)\n",
         "                            rate_txt = f\"{rate:.1f} tok/s\" if rate is not None else \"no speed\"\n"
         "                            # the mixed-GPU helper's per-request evidence rides the engine's DONE line as\n"
         "                            # \" helper=reqs=.. rows=.. bytes=.. timeouts=.. fallbacks=..\" at its tail -\n"
         "                            # surface it here so the counters are observable in the log.\n"
         "                            done_line = getattr(self.engine, \"_last_done\", \"\") or \"\"\n"
         "                            helper_at = done_line.find(\" helper=\")\n"
         "                            helper_msg = done_line[helper_at:] if helper_at >= 0 else \"\"\n"
         "                            print(f\"[strata] done: {n} tokens in {el:.0f} s ({rate_txt}) \"\n"
         "                                  f\"({finish}, cancel={cancel.is_set()}){hit_msg}{helper_msg}\", flush=True)\n",
         "the helper's evidence in the done line"),
    # --- engine arguments ---------------------------------------------------------------
    Edit(S, "dflash_spec_min_p = None",
         "    args = list(cfg[\"args\"])\n",
         "    args = list(cfg[\"args\"])\n"
         "    # The --dflash drafter reads --spec-min-p (the window gate), and that flag sits in\n"
         "    # strata_control/draft_layer.py's MTP_FLAGS set: both \"no draft layer\" paths below - \"drafting\": \"off\"\n"
         "    # (which the chooser turns into \"mtp\": false) and a plain \"mtp\": false - would take it with the MTP draft\n"
         "    # layer.  A config that serves --dflash keeps it in every path: those switches are about the MTP layer,\n"
         "    # never about the drafter the config chose.\n"
         "    dflash_spec_min_p = None\n"
         "    if \"--dflash\" in args and \"--spec-min-p\" in args:\n"
         "        _i = args.index(\"--spec-min-p\")\n"
         "        if _i + 1 < len(args) and not str(args[_i + 1]).startswith(\"--\"):\n"
         "            dflash_spec_min_p = str(args[_i + 1])\n",
         "--spec-min-p survives the draft-layer switches"),
    Edit(S, 'if dflash_spec_min_p is not None and "--spec-min-p" not in args:',
         '            print(f\'[strata] "mtp": false - starting without the draft layer ({" ".join(removed)} left out)\', flush=True)\n'
         "    # #1322: a config with a \"vision\" section",
         '            print(f\'[strata] "mtp": false - starting without the draft layer ({" ".join(removed)} left out)\', flush=True)\n'
         '    if dflash_spec_min_p is not None and "--spec-min-p" not in args:\n'
         '        args += ["--spec-min-p", dflash_spec_min_p]\n'
         "        print('[strata] the draft layer is out: --spec-min-p goes back, this config serves --dflash and its window gate reads the flag',\n"
         "              flush=True)\n"
         "    # #1322: a config with a \"vision\" section",
         "--spec-min-p goes back after the draft layer is stripped"),
    Edit(S, "    args += expert_helper_args(cfg, args)",
         "    args += pool_cores_args(cfg, args)\n",
         "    args += pool_cores_args(cfg, args)\n    args += expert_helper_args(cfg, args)\n",
         "engine_args composes expert_helper_args"),
])

import_insert(S, "from serve.server_parts.base import (", "DCACHE_PHASES")
import_insert(S, "from serve.server_parts.engines import (", "expert_helper_args")
print("done")

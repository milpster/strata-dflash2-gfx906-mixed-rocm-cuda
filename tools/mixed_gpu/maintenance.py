#!/usr/bin/env python3
"""Read-only baseline capture and guarded maintenance for the mixed HIP/CUDA window.

This tool has two jobs and one safety rule: nothing changes unless you pass --execute.

  capture : read-only inventory of the live host (git tree, binary/config/launcher hashes,
            CMake settings, process identities, memory, swap, NBD state, service state,
            sysctls, bounded NVIDIA/AMD reads) written to one file you name.
  drain   : take the confirmed NVIDIA-backed NBD swap offline in the safe order:
            swapoff the one device -> verify it is gone from /proc/swaps -> only then
            disconnect the device, stop the unit, set the captured sysctls.  A failed
            swapoff, or a device that stays in /proc/swaps, aborts with no disconnect, no
            stop, no kill and no cleanup, so the operator can free RAM and retry.
  restore : bring back only what this tool offlined, from the snapshot plus this tool's own
            durable operation record: swapon when the backing daemon is alive and the device
            is connected, otherwise start the unit (its own connect script connects, mkswaps
            and swaps on).  Re-derives the live device afterwards, because the unit's connect
            script re-picks the first free /dev/nbdN.
  verify  : read-only postcondition check for "drained" or "restored".

Fail-closed rules, all enforced in code and covered by tests:

  * Unknown is never success.  A read that fails (permission, truncation, a procfs file that
    vanished), a command that exits unexpectedly, an empty stdout where a state string is
    required, a blank swap table, a malformed swap row, a unit whose `is-active` and
    `ActiveState` disagree, an unreadable /proc enumeration: all are UNKNOWN and the tool
    refuses.  Only `ENOENT` counts as a known-absent file, and only `rc=3` with
    stdout "inactive" counts as a known-stopped unit.
  * An empty /proc/swaps header-only table is a *known* empty table.  It is not the same as
    an unreadable table, and absence is only ever inferred from a successfully read table.
  * Every mutating command's exit status is checked, and every mutation is followed by a
    re-probe of the fact it was supposed to change.  Success is claimed only from the
    re-probe, never from a command's exit status.  A cleanup that half-succeeded reports a
    partial phase and a non-zero exit, and the operation record keeps enough detail to
    resume from exactly that step.
  * The operation record is written to disk BEFORE the first mutating command, and updated
    after each step.  Nothing is cleaned up in a `finally` clause.
  * Drain never disconnects a device whose detachment it cannot prove, and never removes
    /run/nbd-vram-dev before that proof.  Restore never consumes its record before the
    restored state is proven, and never swapons a device with no verified backing.
  * Starting the unit needs provable free VRAM on the captured NVIDIA UUID: the unit's own
    reservation (VRAM_SETUP_SIZE_MB) plus a margin must fit in the measured free memory of
    that card.  --allow-compute-pid cannot relax this: it names expected clients, it does
    not create capacity.
  * No shell=True, no shell strings, no sudo, no hooks.  Commands are argv lists built by
    this module, drawn from a fixed set of shapes.  is_mutating() classifies those shapes for
    the tests and the report; it is a classifier, not an enforcement layer, and is described
    as such.
  * The snapshot is data.  Device names and unit names are validated against fixed patterns
    and an allowed-unit set before any command is built; the snapshot is never executed.
  * Secrets are not serialized: process environments are never read, and command lines are
    stored with flag values redacted except for a whitelisted set of plain engine settings.
  * Provenance: hashes record the bytes of the file captured at capture time.  They are
    evidence about that file, not byte-proven source provenance of a build.

Exit codes: 0 done, 2 refused (a prerequisite is missing, stale, mismatched or unknown -
nothing mutated), 3 drain aborted at swapoff (safe to retry, swap backing left running),
4 verification did not pass, 5 partial cleanup/restore (steps verified incomplete: the record
says exactly which step; re-run the same command).

Known limits, stated because they are real: the kernel NBD client pid in
/sys/block/<dev>/pid is a kernel record, not a userspace process the operator can inspect
(measured on this host: pid 4569 with no readable /proc entry), so the client is bound by
presence plus the daemon's identity (unit MainPID + start time + argv[0]) and the unit's
socket path.  Proof of that binding is presence-level, not process-level.

Usage (see tools/mixed_gpu/README.md for the operator sequence):

  python3 tools/mixed_gpu/maintenance.py capture --out build-mixed-gpu/baseline.json \
      --live-root DIR --binary PATH --config PATH --launcher PATH --build-dir DIR
  python3 tools/mixed_gpu/maintenance.py drain   --snapshot FILE --mem-budget-kib N      # plan
  python3 tools/mixed_gpu/maintenance.py drain   --snapshot FILE --mem-budget-kib N --execute
  python3 tools/mixed_gpu/maintenance.py restore --snapshot FILE --opstate FILE \
      --execute --confirm-cuda-released --expect-prod-stopped
  python3 tools/mixed_gpu/maintenance.py verify  --snapshot FILE --expect drained

Python 3 stdlib only.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SNAPSHOT_SCHEMA = "strata-mixed-gpu-snapshot/3"
OPSTATE_SCHEMA = "strata-mixed-gpu-opstate/3"
STATE_FILE = "/run/nbd-vram-dev"
SOCK_FILE = "/run/nbd-vram.sock"
SWAPS_FILE = "/proc/swaps"
ALLOWED_UNITS = ("vram-swap-nbd.service",)
UNIT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\.service$")
NBD_RE = re.compile(r"^/dev/nbd(?:[0-9]|1[0-5])$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_MARGIN_KIB = 2 * 1024 * 1024      # 2 GiB above swap in use + staging
DEFAULT_VRAM_MARGIN_MIB = 512             # headroom above the unit's VRAM reservation
DEFAULT_TIMEOUT = 60.0
DRAIN_TIMEOUT = 1800.0

E_OK = 0
E_REFUSED = 2
E_DRAIN_ABORTED = 3
E_VERIFY_FAIL = 4
E_PARTIAL = 5
E_INTERNAL = 6

# Read statuses.  MISSING means ENOENT and nothing else; every other failure is UNKNOWN.
READ_OK = "ok"
READ_MISSING = "missing"
READ_UNKNOWN = "unknown"


class Refuse(Exception):
    """A prerequisite is missing, stale, mismatched or unknown.

    This is the PRE-action refusal class: the checks that raise it run before the first
    mutating command, so exit 2 genuinely means nothing was mutated.  A failure discovered
    after a mutation is handled as a recorded partial (exit 5) with the honest message that
    the mutation happened - never as a refusal.
    """


class ParseFail(Exception):
    """A proc file was readable but not in the shape the parser requires."""


@dataclasses.dataclass
class Cmd:
    argv: list
    rc: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0


# ----------------------------------------------------------------------------------
# Backend: files and argv commands.  Read-mostly; only write_private and remove mutate.
# ----------------------------------------------------------------------------------
class RealBackend:
    """Files and argv commands on the real host.

    Every read reports a status: READ_OK with the text, READ_MISSING for ENOENT only,
    READ_UNKNOWN for anything else (permission denied, I/O error, is-a-directory, device
    state).  Callers must not treat READ_UNKNOWN as absence.
    """

    def read(self, path):
        try:
            with open(path, "rb") as fh:
                return READ_OK, fh.read().decode("utf-8", "replace")
        except FileNotFoundError:
            return READ_MISSING, None
        except (IsADirectoryError, PermissionError, OSError):
            return READ_UNKNOWN, None

    def read_bytes(self, path):
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def read_text(self, path):
        """Lenient read for non-critical sites (sysfs extras, informational fields)."""
        return self.read(path)[1]

    def exists(self, path):
        return READ_OK, os.path.lexists(path)

    def readlink(self, path):
        try:
            return READ_OK, os.readlink(path)
        except FileNotFoundError:
            return READ_MISSING, None
        except OSError:
            return READ_UNKNOWN, None

    def listdir(self, path):
        try:
            return READ_OK, sorted(os.listdir(path))
        except FileNotFoundError:
            return READ_MISSING, []
        except OSError:
            return READ_UNKNOWN, []

    def socket_stat(self, path):
        try:
            st = os.stat(path)
            return READ_OK, (st.st_size, int(st.st_mtime), st.st_uid, st.st_mode)
        except FileNotFoundError:
            return READ_MISSING, None
        except OSError:
            return READ_UNKNOWN, None

    def size_mtime_uid(self, path):
        try:
            st = os.stat(path)
            return READ_OK, (st.st_size, int(st.st_mtime), st.st_uid)
        except FileNotFoundError:
            return READ_MISSING, None
        except OSError:
            return READ_UNKNOWN, None

    def run(self, argv, timeout: float = DEFAULT_TIMEOUT, cwd=None) -> Cmd:
        argv = [str(a) for a in argv]
        try:
            p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                               check=False, shell=False)
            return Cmd(argv, p.returncode, p.stdout, p.stderr)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"")
            err = (e.stderr or b"")
            return Cmd(argv, 124, out.decode() if isinstance(out, bytes) else str(out),
                       err.decode() if isinstance(err, bytes) else str(err), timed_out=True)
        except FileNotFoundError:
            return Cmd(argv, 127, "", "command not found")
        except OSError as e:
            return Cmd(argv, 126, "", f"os error: {e}")

    def write_private(self, path, text: str) -> None:
        """Atomic private write: temp sibling in the same directory, flush + fsync,
        os.replace, then fsync of the parent directory.  A crash can therefore never tear a
        record that the design says is kept for recovery.  Concrete persistence failures are
        reported as refusals; the previous file is left intact."""
        p = Path(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise Refuse(f"cannot prepare the directory for {p}: {e}")
        tmp = p.parent / (p.name + ".tmp")
        fd = None
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fd = None
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(str(tmp), 0o600)
            os.replace(str(tmp), str(p))
        except FileNotFoundError as e:
            raise Refuse(f"cannot write {p}: the path vanished ({e})")
        except PermissionError as e:
            raise Refuse(f"cannot write {p}: permission denied ({e}); the previous file is intact")
        except OSError as e:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
            raise Refuse(f"cannot write {p}: {e}; the previous file is intact")
        try:
            dfd = os.open(str(p.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as e:
            raise Refuse(f"{p} was written but its directory could not be synced ({e}) - "
                         f"recovery state is not durable, refusing to continue")

    def remove(self, path) -> bool:
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            raise Refuse(f"cannot remove {path}: permission/I/O error (unknown state)")

    def clock_ticks(self) -> int:
        return os.sysconf("SC_CLK_TCK")

    def epoch(self) -> float:
        return time.time()


# The command shapes this module can build, and how each is classified.  is_mutating() is a
# CLASSIFIER for the tests and the report: RealBackend.run() runs whatever argv this module
# builds, so the guarantee is that this module builds nothing outside these shapes - there is
# no enforcement layer that would stop a rogue caller of run().  nvidia-smi -pm 1 is a write
# (it changes persistence mode); this tool never needs it, and it is classified mutating.
MUTATING_SHAPES = (
    ("swapoff",), ("swapon",), ("mkswap",), ("nbd-client",), ("sysctl", "-w"),
    ("systemctl", "start"), ("systemctl", "stop"), ("nvidia-smi", "-pm"),
)
READONLY_SHAPES = (
    ("git",), ("ss",), ("systemctl", "show"), ("systemctl", "is-active"),
    ("systemctl", "is-enabled"), ("nvidia-smi", "--query-gpu=index,name,uuid,memory.total,memory.used",
     "--format=csv,noheader"),
    ("nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"),
    ("rocm-smi",), ("uname",),
)


def is_mutating(argv) -> bool:
    a = [str(x) for x in argv]
    return any(a[:len(pre)] == list(pre) for pre in MUTATING_SHAPES)


def is_readonly(argv) -> bool:
    a = [str(x) for x in argv]
    if is_mutating(a):
        return False
    return any(a[:len(pre)] == list(pre) for pre in READONLY_SHAPES)


# ----------------------------------------------------------------------------------
# Total parsers: readable-but-wrong raises ParseFail, absence is never guessed.
# ----------------------------------------------------------------------------------
SWAP_HEAD = ("Filename", "Type", "Size", "Used", "Priority")


def parse_swaps(text) -> list:
    """Parse /proc/swaps.  Raises ParseFail on anything malformed.  A valid header-only
    table returns [] - that is a KNOWN empty table, distinct from an unreadable one."""
    if text is None:
        raise ParseFail("no text")
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        raise ParseFail("missing header line")
    head = lines[0].split()
    if tuple(head[:5]) != SWAP_HEAD:
        raise ParseFail(f"unexpected header {head[:5]!r}")
    rows = []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        parts = ln.split()
        if len(parts) < 5:
            raise ParseFail(f"short row {ln!r}")
        try:
            size, used, prio = int(parts[2]), int(parts[3]), int(parts[4])
        except ValueError:
            raise ParseFail(f"non-numeric Size/Used/Priority in row {ln!r}")
        if size < 0 or used < 0 or used > size:
            raise ParseFail(f"implausible sizes in row {ln!r}")
        rows.append({"filename": parts[0], "type": parts[1], "size_kib": size,
                     "used_kib": used, "priority": prio})
    return rows


def read_swaps(backend):
    """(status, rows, why): status ok | unknown | malformed.  Callers act only on ok."""
    st, txt = backend.read(SWAPS_FILE)
    if st != READ_OK:
        return "unknown", [], f"{SWAPS_FILE} read status {st}"
    if not txt.strip():
        return "unknown", [], f"{SWAPS_FILE} is blank (procfs is never blank)"
    try:
        return "ok", parse_swaps(txt), None
    except ParseFail as e:
        return "malformed", [], f"{SWAPS_FILE}: {e}"


def row_for(rows, dev):
    return next((r for r in rows if r["filename"] == dev), None)


MEMORY_KEYS = ("MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree", "Dirty",
               "Cached", "Committed_AS")
SYSCTL_KEYS = (("vm.swappiness", 0, 200), ("vm.min_free_kbytes", 16384, 8388608))
SERVICE_PROPS = ("MainPID", "ActiveState", "SubState", "KillMode", "SendSIGKILL",
                 "TimeoutStopUSec", "Restart", "FragmentPath", "Environment")


def parse_meminfo(text) -> dict:
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"^(\w+):\s+(\d+)\s*kB", line)
        if m and m.group(1) in MEMORY_KEYS:
            out[f"{m.group(1)}_kib"] = int(m.group(2))
    return out


def read_meminfo(backend):
    st, txt = backend.read("/proc/meminfo")
    if st != READ_OK:
        raise Refuse(f"/proc/meminfo read status {st} - unknown memory, refusing")
    info = parse_meminfo(txt)
    if not isinstance(info.get("MemAvailable_kib"), int):
        raise Refuse("/proc/meminfo has no parseable MemAvailable - refusing")
    return info


def sysctl_values(backend):
    """(values, unknown): a key whose file cannot be read is reported, never assumed."""
    vals, unknown = {}, []
    for key, lo, hi in SYSCTL_KEYS:
        st, txt = backend.read("/proc/sys/" + key.replace(".", "/"))
        if st != READ_OK:
            unknown.append(f"{key} read status {st}")
            continue
        try:
            vals[key] = int(txt.strip())
        except ValueError:
            unknown.append(f"{key} not an integer: {txt.strip()!r}")
    return vals, unknown


def validate_sysctl_bounds(vals, where):
    for key, lo, hi in SYSCTL_KEYS:
        v = vals.get(key)
        if not isinstance(v, int) or not (lo <= v <= hi):
            raise Refuse(f"{where}: {key}={v!r} is outside the sane range {lo}..{hi}")


def btime(backend) -> int:
    st, txt = backend.read("/proc/stat")
    if st != READ_OK:
        raise Refuse(f"/proc/stat read status {st} - process start times unknowable")
    for line in txt.splitlines():
        if line.startswith("btime "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                raise ParseFail("/proc/stat btime is not an integer")
    raise ParseFail("/proc/stat has no btime line")


# ----------------------------------------------------------------------------------
# Process identity: redaction, /proc reads, PID-reuse-aware comparison.
# ----------------------------------------------------------------------------------
KEEP_VALUE_FLAGS = {
    "config", "engine", "port", "gpu", "model", "mmproj", "pack", "native",
    "native-dense-gguf", "ple-gguf", "expert-profile", "expert-cache", "dflash",
    "dflash-block", "threads", "max-context", "max-tokens", "kv", "kv-resident",
    "prefill", "spec", "spec-min-p", "pcie-frac", "layer-split", "vram-reserve-mib",
    "vram-reserve-later-mib", "prompt-cache-spread", "expert-cache-cpu-order",
    "conversation-cache-mib", "conversation-cache-min-tokens",
    "conversation-cache-park-gain", "suffix-draft", "vision", "serve",
}
SECRET_WORDS = {"key", "apikey", "token", "tokens", "secret", "password", "passwd", "pass",
                "auth", "bearer", "credential", "credentials", "cookie", "session", "jwt"}
# Numeric engine settings whose name mentions a "token" word but carries a count, not a
# credential.  Listed explicitly so redaction is predictable, not a regex accident.
NON_SECRET_FLAGS = {"conversation-cache-min-tokens", "max-tokens", "max-context",
                    "conversation-cache-mib", "spec-min-p"}


def is_secret_flag(name: str) -> bool:
    """Whole-word test, so a substring cannot decide redaction and a whitelist entry cannot
    be silently shadowed.  Splits on '-', '_' and camelCase boundaries."""
    n = str(name)
    if n.lower() in NON_SECRET_FLAGS:
        return False
    return any(p.lower() in SECRET_WORDS for p in re.split(r"[-_]+|(?<=[a-z])(?=[A-Z])", n))


SCRIPT_EXT = (".py", ".sh", ".bash", ".js", ".rb")


def _is_flag(tok: str) -> bool:
    return tok.startswith("-") and len(tok) > 1


def redact_cmdline(raw: str) -> str:
    """Keep argv[0] and every flag name.  Keep a flag's value only when the flag is a
    whitelisted plain engine setting and is not secret-named.  A positional value is kept
    only as the basename of a script (so the wrapper stays identifiable); everything else
    becomes <redacted>.  A flag never swallows the following flag as its value."""
    toks = raw.split("\0") if "\0" in raw else raw.split()
    toks = [t for t in toks if t != ""]
    if not toks:
        return ""
    out = [toks[0]]
    i = 1
    while i < len(toks):
        tok = toks[i]
        if _is_flag(tok):
            if "=" in tok:
                flag, val = tok.split("=", 1)
                name = flag.lstrip("-")
                keep = name in KEEP_VALUE_FLAGS and not is_secret_flag(name)
                out.append(f"{flag}={val if keep else '<redacted>'}")
                i += 1
                continue
            name = tok.lstrip("-")
            out.append(tok)
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if nxt is not None and not _is_flag(nxt):
                keep = name in KEEP_VALUE_FLAGS and not is_secret_flag(name)
                out.append(nxt if keep else "<redacted>")
                i += 2
                continue
            i += 1
            continue
        base = os.path.basename(tok.rstrip("/"))
        out.append(base if base.endswith(SCRIPT_EXT) else "<redacted>")
        i += 1
    return " ".join(out)


def proc_start_ticks(text) -> int:
    """Field 22 of /proc/<pid>/stat (1-based) = start time in clock ticks.  Raises ParseFail
    when the line is not in the kernel's shape, so a bad read cannot silently mean 'gone'."""
    if text is None:
        raise ParseFail("no stat text")
    tail = text.rsplit(")", 1)[-1]
    fields = tail.split()
    if len(fields) < 21:
        raise ParseFail(f"stat tail has {len(fields)} fields, expected >= 21")
    state = fields[0]
    if state not in ("R", "S", "D", "T", "Z", "X", "I"):
        raise ParseFail(f"stat tail starts with {state!r}")
    try:
        return int(fields[19])
    except ValueError:
        raise ParseFail("stat starttime is not an integer")


def proc_identity(backend, pid, bt, hz):
    """Full identity of a live pid, or raises Refuse when a required read fails."""
    st, cmd = backend.read(f"/proc/{pid}/cmdline")
    if st == READ_MISSING:
        return None
    if st != READ_OK:
        raise Refuse(f"/proc/{pid}/cmdline read status {st} - process identity unknown")
    st, stat = backend.read(f"/proc/{pid}/stat")
    if st != READ_OK:
        raise Refuse(f"/proc/{pid}/stat read status {st} - start time unknown for pid {pid}")
    try:
        ticks = proc_start_ticks(stat)
    except ParseFail as e:
        raise Refuse(f"/proc/{pid}/stat unparseable: {e}")
    stl, exe = backend.readlink(f"/proc/{pid}/exe")
    stw, cwd = backend.readlink(f"/proc/{pid}/cwd")
    rss = None
    stx, status = backend.read(f"/proc/{pid}/status")
    if stx == READ_OK:
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                n = line.split()[1:2]
                rss = int(n[0]) if n and n[0].isdigit() else None
    return {"pid": pid, "cmdline": redact_cmdline(cmd), "starttime_ticks": ticks,
            "start_epoch": bt + ticks / hz if bt is not None and hz else None,
            "exe": exe if stl == READ_OK else None,
            "exe_status": stl, "cwd": cwd if stw == READ_OK else None,
            "vm_rss_kib": rss}


DEFAULT_PROCESS_MATCH = (
    ("wrapper", r"\bserver\.py\b"),
    ("engine", r"(^|/)strata(\s|$)"),
)


def scan_processes(backend, patterns):
    """All live pids whose redacted command line matches a pattern.  Raises Refuse when
    /proc cannot be enumerated or a required per-pid read fails."""
    st, names = backend.listdir("/proc")
    if st != READ_OK:
        raise Refuse(f"/proc enumeration read status {st} - cannot prove what is running")
    bt, hz = btime(backend), backend.clock_ticks()
    out = []
    for name in names:
        if not name.isdigit():
            continue
        ent = proc_identity(backend, int(name), bt, hz)
        if ent is None:
            continue
        for label, pat in patterns:
            if re.search(pat, ent["cmdline"]):
                ent["label"] = label
                ent["match"] = pat
                out.append(ent)
                break
    return out


def prod_processes_gone(backend, snap):
    """Recorded prod must be gone.  A live pid with the recorded start time is the SAME
    process (refuse).  A live pid with a different start time is a NEW process that reused
    the pid - also an unexpected live prod-identity process, never silently skipped."""
    patterns = tuple((str(a), str(b)) for a, b in snap.get("process_match", DEFAULT_PROCESS_MATCH))
    live = scan_processes(backend, patterns)
    live_by_pid = {e["pid"]: e for e in live}
    recorded = {r["pid"]: r for r in snap.get("processes", [])}
    alive, reused = [], []
    for pid, rec in recorded.items():
        ent = live_by_pid.get(pid)
        if ent is not None:
            if rec.get("starttime_ticks") is None:
                raise Refuse(f"snapshot has no start time for pid {pid} - cannot tell a live "
                             f"process from a reused pid")
            if ent["starttime_ticks"] == rec["starttime_ticks"]:
                alive.append(ent)
            else:
                reused.append(ent)
            continue
        st, txt = backend.read(f"/proc/{pid}/stat")
        if st == READ_OK:
            try:
                ticks = proc_start_ticks(txt)
            except ParseFail as e:
                raise Refuse(f"/proc/{pid}/stat unparseable: {e}")
            if rec.get("starttime_ticks") == ticks:
                raise Refuse(f"pid {pid} is still the recorded {rec.get('label')} process "
                             f"(same start time) - TERM the wrapper first")
            raise Refuse(f"pid {pid} exists with a different start time - a live process "
                         f"reused the recorded pid, refusing to assume prod is down")
        if st != READ_MISSING:
            raise Refuse(f"/proc/{pid}/stat read status {st} - cannot prove pid {pid} is gone")
    extras = [e for e in live if e["pid"] not in recorded]
    if alive:
        raise Refuse("refusing: recorded prod process still running: "
                     + ", ".join(f"{e['label']}(pid {e['pid']})" for e in alive)
                     + " - TERM the wrapper first and let the engine drain")
    if reused:
        raise Refuse("refusing: a live process reuses a recorded prod pid: "
                     + ", ".join(f"{e['label']}(pid {e['pid']} start {e['starttime_ticks']})"
                                 for e in reused))
    if extras:
        raise Refuse("refusing: an unexpected process matches the prod identity patterns: "
                     + ", ".join(f"{e['label']}(pid {e['pid']})" for e in extras))
    return live


def port_listening(backend, port) -> bool:
    r = backend.run(["ss", "-H", "-ltn", f"sport = :{int(port)}"], timeout=20)
    if not r.ok:
        raise Refuse(f"cannot check endpoint port {port}: ss rc={r.rc} "
                     f"({r.stderr.strip()[:120]}) - endpoint state unknown")
    return bool(r.stdout.strip())


# ----------------------------------------------------------------------------------
# Capture (read-only).  Writes only the file named by --out.
# ----------------------------------------------------------------------------------
def sha256_of(backend, path) -> str:
    data = backend.read_bytes(path)
    if data is None:
        return ""
    return hashlib.sha256(data).hexdigest()


def file_identity(backend, path, label) -> dict:
    ent = {"label": label, "path": str(path) if path else None}
    if not path:
        ent["missing"] = True
        return ent
    st, meta = backend.size_mtime_uid(path)
    if st == READ_MISSING:
        ent["missing"] = True
        return ent
    if st != READ_OK:
        ent["unreadable"] = True
        return ent
    size, mt, uid = meta
    ent["size_bytes"], ent["uid"] = size, uid
    ent["mtime_iso"] = datetime.fromtimestamp(mt, timezone.utc).isoformat()
    ent["sha256"] = sha256_of(backend, path)
    return ent


def git_identity(backend, live_root) -> dict:
    if not live_root:
        return {"missing": True, "note": "no --live-root given"}
    sha = backend.run(["git", "-C", str(live_root), "rev-parse", "HEAD"], timeout=20)
    por = backend.run(["git", "-C", str(live_root), "status", "--porcelain=v1"], timeout=60)
    out = {"live_root": str(live_root)}
    head = sha.stdout.strip() if sha.ok else ""
    out["head"] = head
    out["head_ok"] = bool(GIT_RE.match(head))
    files = por.stdout.splitlines() if por.ok else []
    out["dirty"] = {"tracked_changed": sum(1 for f in files if not f.startswith("?? ")),
                    "untracked": sum(1 for f in files if f.startswith("?? ")),
                    "files": [f.strip() for f in files][:200]}
    if not por.ok:
        out["dirty"] = {"tracked_changed": None, "untracked": None, "files": [],
                        "unknown": f"git status rc={por.rc}"}
    if not sha.ok:
        out["git_error"] = {"rc": sha.rc, "stderr": sha.stderr.strip()[:400]}
    return out


CMAKE_KEYS = (
    "CMAKE_BUILD_TYPE", "CMAKE_HOME_DIRECTORY", "CMAKE_CXX_COMPILER", "CMAKE_CXX_FLAGS",
    "CMAKE_CXX_FLAGS_RELEASE", "CMAKE_CXX_FLAGS_DEBUG", "CMAKE_HIP_ARCHITECTURES",
    "CMAKE_HIP_COMPILER", "CMAKE_CUDA_ARCHITECTURES", "CMAKE_CUDA_COMPILER",
    "CMAKE_CUDA_FLAGS", "STRATA_ENABLE_CUDA", "STRATA_ENABLE_HIP", "STRATA_ENABLE_SYCL",
    "CMAKE_SYSTEM_PROCESSOR", "CMAKE_INSTALL_PREFIX", "CMAKE_GENERATOR",
    "CMAKE_OSX_ARCHITECTURES", "BUILD_SHARED_LIBS",
)


def cmake_settings(backend, build_dir) -> dict:
    if not build_dir:
        return {"missing": True, "note": "no --build-dir given"}
    cache = str(Path(build_dir) / "CMakeCache.txt")
    st, text = backend.read(cache)
    if st == READ_MISSING:
        return {"build_dir": str(build_dir), "missing": True, "note": "no CMakeCache.txt"}
    if st != READ_OK:
        return {"build_dir": str(build_dir), "unreadable": True, "note": f"read status {st}"}
    vals = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_]+)(?::[A-Z]+)?=(.*)$", line)
        if m and m.group(1) in CMAKE_KEYS:
            vals[m.group(1)] = m.group(2)
    return {"build_dir": str(build_dir), "cache_path": cache,
            "cache_sha256": sha256_of(backend, cache), "keys": vals,
            "missing_keys": [k for k in CMAKE_KEYS if k not in vals]}


def service_state(backend, unit) -> dict:
    """Unit state, fail-closed.  state is 'active', 'inactive' or 'unknown' - never a guess.

    'inactive' requires is-active rc=3 with stdout 'inactive', or a successful show with
    ActiveState=inactive.  A non-zero is-active with empty or unexpected stdout is unknown.
    """
    out = {"unit": unit, "state": "unknown", "active": None, "main_pid": None,
           "main_cmdline": None, "main_start_ticks": None, "main_exe": None,
           "enabled": None, "props": {}, "unknown": []}
    if not UNIT_RE.match(str(unit)) or unit not in ALLOWED_UNITS:
        out["unknown"].append(f"unit {unit!r} is not in the allowed set {ALLOWED_UNITS}")
        return out
    act = backend.run(["systemctl", "is-active", unit], timeout=20)
    en = backend.run(["systemctl", "is-enabled", unit], timeout=20)
    show = backend.run(["systemctl", "show", unit, *(f"-p{p}" for p in SERVICE_PROPS)],
                       timeout=30)
    a_txt = act.stdout.strip()
    probe = None
    if act.ok and a_txt == "active":
        probe = True
    elif act.rc == 3 and a_txt == "inactive":
        probe = False
    else:
        out["unknown"].append(f"is-active rc={act.rc} stdout={a_txt!r}")
    shown = None
    if not show.ok:
        out["unknown"].append(f"show rc={show.rc}")
    else:
        props = {}
        for line in show.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        out["props"] = props
        ast = props.get("ActiveState")
        if ast == "active":
            shown = True
        elif ast == "inactive":
            shown = False
        else:
            out["unknown"].append(f"ActiveState={ast!r}")
        mp = props.get("MainPID")
        if mp is None or not mp.isdigit():
            out["unknown"].append(f"MainPID={mp!r}")
        else:
            out["main_pid"] = int(mp)
    if probe is not None and shown is not None and probe != shown:
        out["unknown"].append(f"is-active={probe} contradicts ActiveState={shown}")
        return out
    state = shown if shown is not None else probe
    if state is None:
        return out
    out["state"] = "active" if state else "inactive"
    out["active"] = state
    if en.ok and en.stdout.strip():
        out["enabled"] = en.stdout.strip()
    else:
        out["enabled_unknown"] = en.rc
    if state and out["main_pid"]:
        pid = out["main_pid"]
        stx, cmd = backend.read(f"/proc/{pid}/cmdline")
        if stx == READ_OK:
            out["main_cmdline"] = redact_cmdline(cmd)
        else:
            out["unknown"].append(f"MainPID {pid} cmdline read status {stx}")
        stt, stat = backend.read(f"/proc/{pid}/stat")
        if stt == READ_OK:
            try:
                bt = btime(backend)
                out["main_start_ticks"] = proc_start_ticks(stat)
                out["main_start_epoch"] = bt + out["main_start_ticks"] / backend.clock_ticks()
            except (ParseFail, Refuse) as e:
                out["unknown"].append(f"MainPID {pid} start time: {e}")
        else:
            out["unknown"].append(f"MainPID {pid} stat read status {stt}")
        stel, exe = backend.readlink(f"/proc/{pid}/exe")
        out["main_exe"] = exe if stel == READ_OK else None
        out["main_exe_status"] = stel
    return out


def require_unit(backend, unit, where=""):
    s = service_state(backend, unit)
    if s["state"] == "unknown":
        raise Refuse(f"unit state unknown{(' for ' + where) if where else ''}: "
                     + "; ".join(s["unknown"]))
    return s


def read_state_file(backend):
    """(status, device_or_None).  Only ENOENT is a known-absent marker."""
    st, txt = backend.read(STATE_FILE)
    if st != READ_OK:
        return st, None
    val = txt.strip()
    if not val:
        return "unknown", None
    try:
        return READ_OK, validate_device(val)
    except Refuse as e:
        return "malformed", str(e)


def client_pid(backend, dev):
    """The kernel NBD client record for a device.  Presence only: the recorded pid is a
    kernel-side record and is frequently not a userspace process the operator can read
    (measured: /sys/block/nbd0/pid = 4569 with no readable /proc entry)."""
    st, txt = backend.read(f"/sys/block/{os.path.basename(dev)}/pid")
    if st == READ_MISSING:
        return "missing", None
    if st != READ_OK:
        return "unknown", None
    val = txt.strip()
    if not val:
        return "empty", None
    return "connected", int(val) if val.isdigit() else val


def socket_identity(backend, path=SOCK_FILE) -> dict:
    """Type and ownership of the unit's unix socket, as far as an operator can read.

    Existence alone is not identity: the harness requires a socket object (S_IFSOCK) owned by
    uid 0 (the unit runs as root).  Anything else is a refusal, not a warning.
    """
    out = {"path": path, "status": READ_UNKNOWN, "is_socket": None, "uid": None, "mode": None}
    st, meta = backend.socket_stat(path)
    if st != READ_OK:
        out["status"] = st
        return out
    size, mtime, uid, mode = meta
    out.update({"status": READ_OK, "is_socket": bool(mode & 0o170000 == 0o140000), "uid": uid,
                "mode": oct(mode), "size_bytes": size,
                "mtime_iso": datetime.fromtimestamp(mtime, timezone.utc).isoformat()})
    return out


def require_socket_identity(backend, snap, where=""):
    s = socket_identity(backend)
    cap = (snap.get("nbd_socket") or {})
    if s["status"] != READ_OK:
        raise Refuse(f"{s['path']} identity read status {s['status']} - the unit's socket is "
                     f"unproven{(' for ' + where) if where else ''}, refusing")
    if not s["is_socket"]:
        raise Refuse(f"{s['path']} is not a socket object (mode {s['mode']}) - refusing")
    if s["uid"] != 0:
        raise Refuse(f"{s['path']} is owned by uid {s['uid']}, not uid 0 (the unit's daemon "
                     f"runs as root) - refusing")
    if cap.get("status") != READ_OK:
        raise Refuse(f"the snapshot's socket identity is not proven (status "
                     f"{cap.get('status')!r}) - refusing")
    if cap.get("uid") != s["uid"] or cap.get("is_socket") != s["is_socket"]:
        raise Refuse(f"socket identity changed since capture: captured uid={cap.get('uid')} "
                     f"is_socket={cap.get('is_socket')}, live uid={s['uid']} "
                     f"is_socket={s['is_socket']} - refusing")
    return s


def require_boot_identity(backend, snap, where=""):
    """The snapshot's facts are only comparable inside the same boot: process start times are
    btime-relative, so a reboot makes 'same tick offset' meaningless."""
    want = (snap.get("boot") or {}).get("btime")
    try:
        got = btime(backend)
    except ParseFail as e:
        raise Refuse(f"/proc/stat is unparseable ({e}) - boot identity unknown, refusing")
    if not isinstance(want, int):
        raise Refuse("the snapshot records no boot identity (btime) - process start times are "
                     f"not comparable{(' for ' + where) if where else ''}, refusing")
    if want != got:
        raise Refuse(f"the snapshot was captured in a different boot (btime {want}, live "
                     f"{got}) - recorded PIDs, start times and daemon identity are not "
                     f"comparable{(' for ' + where) if where else ''}; capture a fresh baseline")
    return got


def probe_causality(backend, pid) -> dict:
    """Can the operator prove that THIS process is the daemon serving /run/nbd-vram.sock?

    Two supported read-only proofs are attempted:
      * the daemon's own descriptor table (`/proc/<pid>/fd/*` links pointing at the socket);
        readable only to the owner of that process (root), measured on this host: Permission
        denied;
      * `ss -x -p`, which shows the owning pid of a unix socket only for the user running it.
    Presence of a client record and existence of the socket are NOT causal proof: the kernel
    client record is not a resolvable process, and the socket has no readable peer from a
    non-root operator.  If neither proof succeeds the result is proven=False, and the action
    paths refuse (unsupported in V1) instead of trusting a guess.
    """
    out = {"proven": False, "pid": pid, "socket": SOCK_FILE, "proofs": {}}
    st, fds = backend.listdir(f"/proc/{pid}/fd")
    if st == READ_OK:
        targets = []
        for fd in fds:
            s, t = backend.readlink(f"/proc/{pid}/fd/{fd}")
            if s == READ_OK and t:
                targets.append(t)
            elif s == READ_UNKNOWN:
                out["proofs"]["proc_fd"] = {"status": "partial", "reason": "a descriptor link is "
                                            "unreadable", "targets": sorted(targets)}
                break
        else:
            out["proofs"]["proc_fd"] = {"status": "ok", "targets": sorted(targets),
                                        "socket_link": SOCK_FILE in targets}
            if SOCK_FILE in targets:
                out["proven"] = True
                out["proof"] = "proc_fd"
    else:
        out["proofs"]["proc_fd"] = {"status": st}
    if not out["proven"]:
        r = backend.run(["ss", "-x", "-p", "-n", "state", "listening", "path", SOCK_FILE],
                        timeout=20)
        if r.ok and SOCK_FILE in r.stdout:
            pids = re.findall(r"pid=(\d+)", r.stdout)
            out["proofs"]["ss_xp"] = {"status": "ok", "pids": [int(p) for p in pids]}
            if int(pid) in [int(p) for p in pids]:
                out["proven"] = True
                out["proof"] = "ss_xp"
        else:
            out["proofs"]["ss_xp"] = {"status": "unavailable", "rc": r.rc,
                                      "reason": r.stderr.strip()[:120] or "no socket owner visible"}
    return out


def require_backing_causality(backend, pid, where=""):
    p = probe_causality(backend, pid)
    if not p["proven"]:
        raise Refuse("unsupported in V1: the causal link between the swap device, "
                     f"{SOCK_FILE} and the unit daemon (MainPID {pid}) cannot be proved with "
                     f"supported read-only facts (proofs: "
                     f"{json.dumps(p['proofs'], sort_keys=True)}). /proc/<daemonpid>/fd is "
                     f"root-only and a unix socket has no readable peer for this operator, so "
                     f"client presence + socket existence are proximate, not causal. Refusing "
                     f"to mutate{(' for ' + where) if where else ''}; capture/plan review stays "
                     f"available, and a run as the daemon's owner (root) makes the proof")
    return p


def parse_nvidia_gpus(text) -> list:
    gpus = []
    for line in (text or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            raise ParseFail(f"nvidia-smi row {line!r}")
        m = re.match(r"^(\d+)\s*MiB$", parts[3])
        u = re.match(r"^(\d+)\s*MiB$", parts[4])
        if not m or not u:
            raise ParseFail(f"nvidia-smi MiB fields in row {line!r}")
        gpus.append({"index": parts[0], "name": parts[1], "uuid": parts[2],
                     "memory_total_mib": int(m.group(1)), "memory_used_mib": int(u.group(1))})
    if not gpus:
        raise ParseFail("nvidia-smi reported no GPUs")
    return gpus


def parse_compute_apps(text) -> list:
    apps = []
    for line in (text or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            raise ParseFail(f"compute-apps row {line!r}")
        if not parts[0].isdigit():
            raise ParseFail(f"compute-apps pid not numeric in row {line!r}")
        m = re.match(r"^(\d+)\s*MiB$", parts[2])
        if not m:
            raise ParseFail(f"compute-apps used_memory not MiB in row {line!r}")
        apps.append({"pid": int(parts[0]), "name": parts[1], "used_mib": int(m.group(1))})
    return apps


def gpu_snapshot(backend, timeout: float) -> dict:
    out = {}
    nv = backend.run(["nvidia-smi", "--query-gpu=index,name,uuid,memory.total,memory.used",
                      "--format=csv,noheader"], timeout=timeout)
    if not nv.ok or not nv.stdout.strip():
        out["nvidia"] = {"available": False, "unknown": f"rc={nv.rc} timed_out={nv.timed_out}"}
    else:
        try:
            gpus = parse_nvidia_gpus(nv.stdout)
        except ParseFail as e:
            out["nvidia"] = {"available": False, "unknown": f"unparseable: {e}"}
        else:
            apps = backend.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                                "--format=csv,noheader"], timeout=timeout)
            if not apps.ok:
                out["nvidia"] = {"available": True, "gpus": gpus,
                                 "compute_apps_unknown": f"rc={apps.rc}"}
            else:
                try:
                    out["nvidia"] = {"available": True, "gpus": gpus,
                                     "compute_apps": parse_compute_apps(apps.stdout)}
                except ParseFail as e:
                    out["nvidia"] = {"available": True, "gpus": gpus,
                                     "compute_apps_unknown": str(e)}
    amd = backend.run(["rocm-smi", "--showmeminfo", "vram"], timeout=timeout)
    out["amd"] = ({"available": True, "raw": amd.stdout.strip()[:2000]} if amd.ok and amd.stdout.strip()
                  else {"available": False, "unknown": f"rc={amd.rc} timed_out={amd.timed_out}"})
    un = backend.run(["uname", "-r"], timeout=10)
    out["kernel"] = un.stdout.strip() if un.ok else None
    return out


def capture(args, backend) -> dict:
    patterns = tuple(DEFAULT_PROCESS_MATCH)
    swap_status, swap_rows, swap_why = read_swaps(backend)
    st_dev, state_dev = read_state_file(backend)
    sock = socket_identity(backend)
    unit_state = service_state(backend, args.unit)
    client_status, client = (client_pid(backend, state_dev) if st_dev == READ_OK
                             else (READ_MISSING, None))
    # Capture RECORDS unknowns; it never aborts and never guesses.  The actionable paths
    # (drain/restore/verify) are the ones that must refuse what capture wrote down.
    try:
        procs, procs_unknown = scan_processes(backend, patterns), None
    except (Refuse, ParseFail) as e:
        procs, procs_unknown = [], str(e)
    mem = parse_meminfo(backend.read_text("/proc/meminfo"))
    sysc, sysc_unknown = sysctl_values(backend)
    try:
        bt = btime(backend)
        boot_unknown = None
    except (Refuse, ParseFail) as e:
        bt, boot_unknown = None, str(e)
    causal = None
    if unit_state.get("state") == "active" and unit_state.get("main_pid"):
        causal = probe_causality(backend, unit_state["main_pid"])
    snap = {
        "schema": SNAPSHOT_SCHEMA,
        "captured_at_iso": datetime.fromtimestamp(backend.epoch(), timezone.utc).isoformat(),
        "boot": {"btime": bt, "unknown": boot_unknown,
                 "note": "process start times are btime-relative; a snapshot is comparable "
                         "only within the same boot"},
        "live_root": args.live_root,
        "unit": args.unit,
        "port": int(args.port),
        "state_file": STATE_FILE,
        "git": git_identity(backend, args.live_root),
        "files": [file_identity(backend, args.binary, "binary"),
                  file_identity(backend, args.config, "config"),
                  file_identity(backend, args.launcher, "launcher")]
        + [file_identity(backend, p, "extra") for p in (args.file or [])],
        "cmake": cmake_settings(backend, args.build_dir),
        "processes": procs,
        "processes_unknown": procs_unknown,
        "process_match": [list(p) for p in patterns],
        "memory": mem,
        "swaps": {"status": swap_status, "rows": swap_rows, "why": swap_why},
        "nbd_state_file": {"path": STATE_FILE, "status": st_dev, "device": state_dev},
        "nbd_socket": sock,
        "nbd_client": {"status": client_status, "pid": client,
                       "note": "kernel client record: presence only, not a resolvable process"},
        "nbd_causality": causal,
        "service": unit_state,
        "sysctls": sysc,
        "sysctls_unknown": sysc_unknown,
        "gpu": gpu_snapshot(backend, args.timeout),
        "provenance_note": (
            "Hashes are evidence of the bytes captured at capture time. They are not "
            "byte-proven source provenance of a build: no git metadata is baked into the "
            "binary and the toolchain state is not hash-pinned."),
        "redaction_note": (
            "Process command lines keep argv[0] and flag names; values are kept only for "
            "whitelisted engine flags and redacted otherwise. Process environments are "
            "never read, so secrets and raw config/auth args are not serialized."),
    }
    cfg = args.config
    snap["config_reference"] = {
        "path": str(cfg) if cfg else None,
        "sha256": sha256_of(backend, cfg) if cfg else "",
        "note": "config is referenced by path + full hash, not copied into the snapshot",
    }
    return snap


def write_snapshot(backend, snap, out_path) -> None:
    st, present = backend.exists(out_path)
    if st != READ_OK:
        raise Refuse(f"cannot check the target snapshot path ({st}): {out_path}")
    if present:
        raise Refuse(f"refusing to overwrite existing snapshot {out_path}; choose a new --out path")
    backend.write_private(str(out_path), json.dumps(snap, indent=2, sort_keys=True) + "\n")


def load_snapshot(backend, path) -> dict:
    st, text = backend.read(path)
    if st != READ_OK:
        raise Refuse(f"snapshot not readable ({st}): {path}")
    try:
        snap = json.loads(text)
    except json.JSONDecodeError as e:
        raise Refuse(f"snapshot is not valid JSON: {path}: {e}")
    if not isinstance(snap, dict):
        raise Refuse(f"snapshot must be a JSON object, got {type(snap).__name__}: {path}")
    if snap.get("schema") != SNAPSHOT_SCHEMA:
        raise Refuse(f"snapshot schema mismatch: got {snap.get('schema')!r} "
                     f"(this tool is {SNAPSHOT_SCHEMA})")
    snap["_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    return snap


# ----------------------------------------------------------------------------------
# F5: validate every critical fact before any mutation.
# ----------------------------------------------------------------------------------
def validate_timeouts(args) -> None:
    for name in ("timeout", "drain_timeout"):
        v = getattr(args, name, None)
        if v is not None and not (isinstance(v, (int, float)) and v > 0):
            raise Refuse(f"--{name} must be strictly positive, got {v!r}")


def validate_mutation_args(args, snap) -> dict:
    """Every number a mutation depends on, checked before the first mutating command."""
    validate_timeouts(args)
    port = snap.get("port")
    if not (isinstance(port, int) and 1 <= port <= 65535):
        raise Refuse(f"snapshot port {port!r} is not a usable port")
    if getattr(args, "port", None) and int(args.port) != port:
        raise Refuse(f"--port {args.port} does not match the snapshot's recorded port {port}")
    args.port = port
    if int(getattr(args, "margin_kib", DEFAULT_MARGIN_KIB)) < 0:
        raise Refuse("--margin-kib must not be negative")
    if int(getattr(args, "staging_kib", 0)) < 0:
        raise Refuse("--staging-kib must not be negative")
    if getattr(args, "mem_budget_kib", None) is not None:
        if int(args.mem_budget_kib) <= 0:
            raise Refuse("--mem-budget-kib must be strictly positive")
    vram_margin = getattr(args, "vram_margin_mib", None)
    if vram_margin is not None and int(vram_margin) < 0:
        raise Refuse("--vram-margin-mib must not be negative")

    head = (snap.get("git") or {}).get("head", "")
    if not GIT_RE.match(head):
        raise Refuse("snapshot has no full 40-hex git HEAD - refusing")
    if not isinstance((snap.get("boot") or {}).get("btime"), int):
        raise Refuse(f"snapshot records no boot identity ({(snap.get('boot') or {}).get('unknown')})"
                     " - process start times are not comparable, refusing")
    sock = snap.get("nbd_socket") or {}
    if sock.get("status") != READ_OK or not sock.get("is_socket") or sock.get("uid") != 0:
        raise Refuse(f"snapshot's socket identity is not proven ({json.dumps(sock, sort_keys=True)})"
                     " - refusing")
    for label in ("binary", "config", "launcher"):
        ent = next((f for f in snap.get("files", []) if f.get("label") == label), None)
        if ent is None:
            raise Refuse(f"snapshot has no {label} file entry - refusing")
        sha = ent.get("sha256") or ""
        if not SHA_RE.match(sha):
            raise Refuse(f"snapshot {label} hash missing, partial or unreadable "
                         f"({sha!r}) - refusing")
    procs = snap.get("processes")
    if snap.get("processes_unknown") or not isinstance(procs, list) or not procs:
        raise Refuse("snapshot has no proven recorded process identities "
                     f"({snap.get('processes_unknown')}) - cannot prove prod is down")
    for rec in procs:
        if not isinstance(rec.get("starttime_ticks"), int):
            raise Refuse(f"snapshot process pid {rec.get('pid')} has no start time - "
                         f"cannot detect a reused pid")
    if not isinstance((snap.get("memory") or {}).get("MemAvailable_kib"), int):
        raise Refuse("snapshot has no MemAvailable - refusing")
    swaps = snap.get("swaps") or {}
    if swaps.get("status") != "ok":
        raise Refuse(f"snapshot swap table status is {swaps.get('status')!r} "
                     f"({swaps.get('why')}) - refusing")
    st_dev = (snap.get("nbd_state_file") or {}).get("status")
    dev = (snap.get("nbd_state_file") or {}).get("device")
    if st_dev == READ_OK:
        validate_device(dev)
    elif st_dev != READ_MISSING:
        raise Refuse(f"snapshot's {STATE_FILE} read status was {st_dev!r} - the device "
                     f"identity is unknown, refusing")
    svc = snap.get("service") or {}
    if svc.get("state") not in ("active", "inactive"):
        raise Refuse("snapshot has no clear unit state - refusing")
    if not svc.get("main_pid"):
        raise Refuse("snapshot has no unit MainPID - the backing daemon is unidentified")
    if not isinstance(svc.get("main_start_ticks"), int):
        raise Refuse("snapshot has no MainPID start time - cannot bind the backing daemon")
    sysc = snap.get("sysctls") or {}
    validate_sysctl_bounds(sysc, "captured sysctls")
    if snap.get("sysctls_unknown"):
        raise Refuse(f"snapshot sysctls were partly unknown at capture: "
                     f"{snap['sysctls_unknown']} - refusing")
    prio = None
    for r in swaps.get("rows", []):
        if r["filename"] == dev:
            prio = r["priority"]
    if prio is None:
        row0 = next((f for f in svc.get("props", {}).get("Environment", "").split()
                     if f.startswith("VRAM_SWAP_PRIORITY=")), "")
        prio = int(row0.split("=")[1]) if row0.split("=")[-1].isdigit() else None
    if not (isinstance(prio, int) and 0 <= prio <= 32767):
        raise Refuse(f"recorded swap priority {prio!r} is unknown or outside 0..32767")
    snap["_priority"] = prio
    snap["_device"] = dev
    return snap


def validate_unit(unit) -> str:
    if not UNIT_RE.match(str(unit)) or unit not in ALLOWED_UNITS:
        raise Refuse(f"unit {unit!r} is not an allowed maintenance target {ALLOWED_UNITS}")
    return unit


def validate_device(dev) -> str:
    dev = str(dev).strip()
    if not NBD_RE.match(dev):
        raise Refuse(f"device {dev!r} is not a validated /dev/nbdN device")
    return dev


# ----------------------------------------------------------------------------------
# Durable operation record: written before the first mutation, updated after each step.
# ----------------------------------------------------------------------------------
OPSTATE_PHASES = ("intent", "drained", "drain-failed", "drain-disconnect-failed",
                  "drain-stop-failed", "drain-sysctl-failed",
                  "drain-abort-swap-table-unknown", "drain-postcheck-unknown",
                  "drain-postcheck-failed", "drain-precheck-unknown", "drain-precheck-renewed",
                  "cleaned", "restore-intent", "restore-swap-unproven", "restore-sysctl-failed",
                  "restore-unproven-final", "restored")


def load_opstate(backend, path, snap) -> dict:
    st, text = backend.read(path)
    if st == READ_MISSING:
        raise Refuse(f"no operation record at {path}: restore only undoes what this tool "
                     f"offlined, and it needs its own record")
    if st != READ_OK:
        raise Refuse(f"operation record unreadable ({st}): {path}")
    try:
        rec = json.loads(text)
    except json.JSONDecodeError as e:
        raise Refuse(f"operation record is not valid JSON: {e}")
    if not isinstance(rec, dict):
        raise Refuse(f"operation record must be a JSON object, got {type(rec).__name__} "
                     f"({text[:60]!r}) - refusing")
    if rec.get("schema") != OPSTATE_SCHEMA:
        raise Refuse(f"operation record schema mismatch: {rec.get('schema')!r} (this tool is "
                     f"{OPSTATE_SCHEMA}) - refusing")
    if rec.get("snapshot_sha256") != snap["_sha256"]:
        raise Refuse("operation record does not belong to this snapshot (hash mismatch) - "
                     "refusing to guess what to restore")
    validate_device(rec.get("device"))
    validate_unit(rec.get("unit"))
    if rec.get("phase") not in OPSTATE_PHASES:
        raise Refuse(f"operation record phase {rec.get('phase')!r} is not a phase this tool writes")
    if not isinstance(rec.get("priority"), int) or not (0 <= rec["priority"] <= 32767):
        raise Refuse(f"operation record priority {rec.get('priority')!r} is unusable - refusing")
    for key in ("started_iso", "started_btime"):
        if key in rec and not (isinstance(rec[key], (int, float, str))):
            raise Refuse(f"operation record field {key} is not a plain value - refusing")
    if "restored_device" in rec:
        validate_device(rec["restored_device"])
    if not isinstance(rec.get("steps", []), list):
        raise Refuse("operation record steps is not a list - refusing")
    return rec


def load_receipt(backend, path, snap) -> dict:
    """The restore receipt, validated the same way as the record it lives in.  Used by
    verify --expect restored: a foreign, unreadable, malformed or not-restored record can
    never name the device that verify checks."""
    rec = load_opstate(backend, path, snap)
    if rec.get("phase") != "restored":
        raise Refuse(f"operation record phase is {rec['phase']!r}, not 'restored' - there is no "
                     f"restore receipt to verify against")
    target = validate_device(rec.get("restored_device"))
    cap = validate_device(rec.get("device"))
    if rec.get("restored_priority") != rec.get("priority"):
        raise Refuse(f"restore receipt priority {rec.get('restored_priority')!r} does not match "
                     f"the recorded priority {rec.get('priority')!r} - refusing")
    return {"record": rec, "target": target, "snapshot_device": cap,
            "priority": int(rec["priority"])}


def save_opstate(backend, path, rec, step=None, phase=None) -> dict:
    if step is not None:
        rec.setdefault("steps", []).append(step)
    if phase is not None:
        rec["phase"] = phase
    rec["updated_iso"] = datetime.fromtimestamp(backend.epoch(), timezone.utc).isoformat()
    backend.write_private(path, json.dumps(rec, indent=2, sort_keys=True) + "\n")
    return rec


def persist(backend, path, rec, step=None, phase=None) -> bool:
    """Write the record, and report a persistence failure honestly.

    False means the caller must STOP: the on-disk recovery state is stale, and continuing
    from an in-memory record the disk does not have is exactly how a partial becomes
    untraceable.
    """
    try:
        save_opstate(backend, path, rec, step=step, phase=phase)
        return True
    except Refuse as e:
        print(f"[partial] the operation record could not be written ({e}). The recovery state "
              f"on disk is stale - do not continue, and do not clean up afterwards.",
              file=sys.stderr)
        return False


# ----------------------------------------------------------------------------------
# Guards
# ----------------------------------------------------------------------------------
def check_memory_budget(backend, row, budget_kib, staging_kib, margin_kib) -> dict:
    """RAM headroom after prod shutdown.  Free disk swap is not proof of capacity."""
    if budget_kib is None:
        raise Refuse("missing --mem-budget-kib: the caller must supply the post-shutdown "
                     "memory budget for the migration target")
    if int(budget_kib) <= 0:
        raise Refuse("--mem-budget-kib must be strictly positive")
    mem = read_meminfo(backend)
    avail = mem["MemAvailable_kib"]
    used = int(row["used_kib"]) if row else 0
    migrate_required = used + int(staging_kib) + int(margin_kib)
    required = max(migrate_required, int(budget_kib))
    detail = {"mem_available_kib": avail, "budget_kib": int(budget_kib),
              "swap_used_on_device_kib": used, "staging_kib": int(staging_kib),
              "margin_kib": int(margin_kib), "migrate_required_kib": migrate_required,
              "required_kib": required, "headroom_kib": avail - required,
              "other_swap_free_kib_not_proof": mem.get("SwapFree_kib")}
    if avail < required:
        raise Refuse(f"insufficient MemAvailable for drain: need {required} KiB (the larger "
                     f"of the caller budget {budget_kib} and swap-in-use {used} + staging "
                     f"{staging_kib} + margin {margin_kib}), have {avail} KiB. Free disk "
                     f"swap is not a substitute for RAM.")
    return detail


def require_daemon_shape(svc, where=""):
    """The unit's MainPID is a live nbd-vram process (argv[0] proven)."""
    pid = svc["main_pid"]
    if not pid:
        raise Refuse(f"unit {svc['unit']} is active with no MainPID - the backing daemon is "
                     f"unknown{(' for ' + where) if where else ''}")
    cmd = svc.get("main_cmdline") or ""
    exe0 = os.path.basename(cmd.split()[0]) if cmd.split() else ""
    if exe0 != "nbd-vram":
        raise Refuse(f"backing daemon identity mismatch: MainPID {pid} argv[0] is {exe0!r}, "
                     f"expected nbd-vram (cmdline {cmd!r})")
    return pid


def require_captured_daemon(svc, snap, bt=None, hz=None, where=""):
    """The live MainPID is the SAME daemon the snapshot captured: same process (pid), same
    start time in both ticks and absolute epoch, and the same boot.  A reboot with a
    coincident tick offset is a different daemon on a different filesystem timeline, so the
    epoch and boot identity are compared, not just the offset."""
    require_daemon_shape(svc, where)
    cap = snap.get("service") or {}
    pid = svc["main_pid"]
    if cap.get("main_pid") != pid:
        raise Refuse(f"backing daemon pid changed: snapshot captured MainPID {cap.get('main_pid')},"
                     f" live MainPID is {pid} - the unit was restarted since capture, which is a "
                     f"fresh CUDA context; refusing (use the unit-start restore path only after "
                     f"the unit is proven stopped)")
    want_ticks, want_epoch = cap.get("main_start_ticks"), cap.get("main_start_epoch")
    got_ticks, got_epoch = svc.get("main_start_ticks"), svc.get("main_start_epoch")
    if not isinstance(want_ticks, int) or got_ticks != want_ticks:
        raise Refuse(f"backing daemon start-time ticks mismatch: live {got_ticks!r}, snapshot "
                     f"{want_ticks!r} - refusing")
    if not isinstance(want_epoch, (int, float)) or not isinstance(got_epoch, (int, float)):
        raise Refuse(f"daemon start epoch unavailable (live {got_epoch!r}, snapshot "
                     f"{want_epoch!r}) - absolute start time cannot be compared, refusing")
    if abs(float(got_epoch) - float(want_epoch)) > 1.0:
        raise Refuse(f"backing daemon start epoch mismatch: live {got_epoch}, snapshot "
                     f"{want_epoch} - a coincident tick offset across a boot change is a "
                     f"different process, refusing")
    cap_btime = (snap.get("boot") or {}).get("btime")
    if isinstance(cap_btime, int) and isinstance(bt, int) and cap_btime != bt:
        raise Refuse(f"boot identity changed (snapshot btime {cap_btime}, live {bt}) - the live "
                     f"daemon at the same tick offset is not the captured daemon, refusing")
    return pid


def bind_restore_daemon(backend, svc, snap, rec, bt, where="") -> str:
    """Bind the live backing daemon to THIS restore.  Two legitimate identities:

      * the daemon the snapshot captured (the swapon path - no new VRAM was allocated), or
      * a NEW daemon the unit started after this tool's recorded restore intent, in the same
        boot (the unit-start path).

    Anything else - a restart that is neither, a daemon from a previous boot - refuses.  The
    causal proof that this process serves the unit's socket is part of the binding.
    """
    require_daemon_shape(svc, where)
    require_backing_causality(backend, svc["main_pid"], where)
    cap = snap.get("service") or {}
    got_epoch = svc.get("main_start_epoch")
    cap_epoch = cap.get("main_start_epoch")
    is_captured = (cap.get("main_pid") == svc.get("main_pid")
                   and cap.get("main_start_ticks") == svc.get("main_start_ticks")
                   and isinstance(cap_epoch, (int, float))
                   and isinstance(got_epoch, (int, float))
                   and abs(float(cap_epoch) - float(got_epoch)) <= 1.0
                   and isinstance((snap.get("boot") or {}).get("btime"), int)
                   and (snap.get("boot") or {}).get("btime") == bt)
    if is_captured:
        return "captured-daemon"
    want_epoch, want_btime = rec.get("restore_intent_epoch"), rec.get("restore_intent_btime")
    if not isinstance(want_epoch, (int, float)) or not isinstance(got_epoch, (int, float)):
        raise Refuse("cannot bind the live backing daemon to this restore (recorded "
                     f"restore-intent time {want_epoch!r} or live daemon start time "
                     f"{got_epoch!r} is unknown) - refusing")
    if not isinstance(want_btime, int) or want_btime != bt:
        raise Refuse(f"the restore intent was recorded in boot {want_btime!r}, the live system "
                     f"is in boot {bt} - a daemon started after a reboot is not the daemon this "
                     f"restore started, refusing")
    if got_epoch < want_epoch:
        raise Refuse(f"the live backing daemon (MainPID {svc['main_pid']}, start {got_epoch}) "
                     f"predates this tool's restore intent ({want_epoch}) - it is not the "
                     f"daemon this restore started, refusing")
    return "new-daemon-from-restore-intent"


def resolve_backing(backend, snap, unit, same_daemon=False) -> dict:
    """The confirmed swap device, its live swap row, and the live backing daemon.

    Every fact here is proven from a successful read.  A live daemon additionally requires the
    causal proof (probe_causality) that it is the process serving the unit's socket; without it
    the action paths refuse as unsupported in V1.
    """
    validate_unit(unit)
    dev = snap["_device"]
    bt = require_boot_identity(backend, snap, "backing resolution")
    st_state, live_dev = read_state_file(backend)
    if st_state not in (READ_OK, READ_MISSING):
        raise Refuse(f"{STATE_FILE} read status {st_state} - device identity unknown, refusing")
    if st_state == READ_OK and live_dev != dev:
        raise Refuse(f"stale device identity: snapshot says {dev}, {STATE_FILE} says {live_dev}")
    status, rows, why = read_swaps(backend)
    if status != "ok":
        raise Refuse(f"/proc/swaps is {status} ({why}) - swap presence is unknown, refusing")
    row = row_for(rows, dev)
    svc = require_unit(backend, unit, "backing daemon")
    hz = backend.clock_ticks()
    if svc["state"] == "active":
        if same_daemon:
            require_captured_daemon(svc, snap, bt=bt, hz=hz, where="backing daemon")
        else:
            require_daemon_shape(svc, "backing daemon")
    # Probe the client record unconditionally: the swapon path needs it precisely when the
    # swap row is gone.  An unreadable record is a refusal, never a "not connected".
    client_status, client = client_pid(backend, dev)
    if client_status == "unknown":
        raise Refuse(f"/sys/block/{os.path.basename(dev)}/pid read status unknown - the NBD "
                     f"client state is unknown, refusing")
    sock = require_socket_identity(backend, snap, "backing resolution")
    causal = {"proven": False, "pid": None, "proofs": {"no-daemon": "unit is not active"}}
    if svc["state"] == "active" and svc["main_pid"]:
        causal = require_backing_causality(backend, svc["main_pid"], "backing daemon")
    return {"unit": unit, "device": dev, "swap_row": row, "present_in_swaps": row is not None,
            "service": svc, "rows": rows, "client_status": client_status, "client_pid": client,
            "state_file_status": st_state, "socket": sock, "causality": causal, "btime": bt,
            "priority": snap["_priority"]}


def unit_reservation_mib(snap) -> int:
    """The unit's own VRAM reservation, read from its Environment property.  The unit states
    it as MB (decimal, per the installed unit file), so convert to MiB upward."""
    env = (snap.get("service") or {}).get("props", {}).get("Environment", "")
    val = None
    for token in env.split():
        if token.startswith("VRAM_SETUP_SIZE_MB="):
            rhs = token.split("=", 1)[1]
            val = int(rhs) if rhs.isdigit() else None
    if not val or val <= 0:
        raise Refuse("the captured unit does not record a usable VRAM_SETUP_SIZE_MB - the "
                     "VRAM reservation is unknown, refusing (unsupported in V1)")
    return math.ceil(val * 1000000 / 1048576)


def check_free_vram(backend, snap, margin_mib: int) -> dict:
    """Non-bypassable capacity gate for the unit start.  Uses the captured NVIDIA UUID."""
    nv = snap.get("gpu", {}).get("nvidia", {})
    if not nv.get("available"):
        raise Refuse(f"snapshot has no NVIDIA data ({nv.get('unknown')}) - the VRAM capacity "
                     f"gate cannot run, refusing (unsupported topology for V1)")
    want_uuids = [g.get("uuid") for g in nv.get("gpus", [])]
    if any(not u for u in want_uuids):
        raise Refuse("snapshot NVIDIA data has no UUID - refusing to reserve VRAM blind")
    probe = gpu_snapshot(backend, DEFAULT_TIMEOUT)
    live = probe.get("nvidia", {})
    if not live.get("available"):
        raise Refuse(f"live nvidia-smi unreadable ({live.get('unknown')}) - free VRAM unknown")
    if live.get("compute_apps_unknown"):
        raise Refuse(f"live compute clients unknown ({live['compute_apps_unknown']}) - "
                     f"refusing to reserve VRAM over an unknown allocation")
    live_by_uuid = {g["uuid"]: g for g in live.get("gpus", [])}
    missing = [u for u in want_uuids if u not in live_by_uuid]
    if missing:
        raise Refuse(f"captured NVIDIA UUID(s) {missing} are not present now - topology "
                     f"changed, refusing")
    reservation = unit_reservation_mib(snap)
    margin = int(margin_mib)
    detail = {"reservation_mib": reservation, "margin_mib": margin, "gpus": []}
    for u in want_uuids:
        g = live_by_uuid[u]
        free = g["memory_total_mib"] - g["memory_used_mib"]
        need = reservation + margin
        detail["gpus"].append({"uuid": u, "index": g["index"], "total_mib": g["memory_total_mib"],
                               "used_mib": g["memory_used_mib"], "free_mib": free,
                               "need_mib": need, "ok": free >= need})
    detail["compute_apps"] = live.get("compute_apps", [])
    bad = [g for g in detail["gpus"] if not g["ok"]]
    if bad:
        raise Refuse("insufficient free VRAM to start the unit: "
                     + "; ".join(f"{g['index']}({g['uuid'][:8]}) free {g['free_mib']} MiB < "
                                 f"reservation {reservation} MiB + margin {margin} MiB"
                                 for g in bad)
                     + f". Compute clients now: {detail['compute_apps']}. No flag relaxes "
                       f"this: capacity is capacity.")
    return detail


def require_compute_scope(backend, snap, allowed_extra_pids, daemon_pid=None) -> dict:
    """Which clients are on the NVIDIA device.  A helper is only allowed to be *named*,
    never to make room; capacity is checked by check_free_vram."""
    nv = gpu_snapshot(backend, DEFAULT_TIMEOUT).get("nvidia", {})
    if not nv.get("available"):
        raise Refuse(f"NVIDIA device state unknown ({nv.get('unknown')}) - refusing")
    if nv.get("compute_apps_unknown"):
        raise Refuse(f"compute clients unknown ({nv['compute_apps_unknown']}) - refusing")
    allowed = {int(p) for p in (allowed_extra_pids or [])}
    if daemon_pid:
        allowed.add(int(daemon_pid))
    foreign = [a for a in nv["compute_apps"] if a["pid"] not in allowed]
    if foreign:
        raise Refuse("unexpected compute clients on the NVIDIA device: "
                     + "; ".join(f"{a['pid']}({a['name']}) {a['used_mib']} MiB" for a in foreign)
                     + " - release CUDA, or name an expected client with --allow-compute-pid")
    return nv


def live_facts(backend, dev, unit) -> dict:
    """The proof facts, gathered in ONE place so drain, restore and verify cannot drift."""
    status, rows, why = read_swaps(backend)
    row = row_for(rows, dev) if status == "ok" else None
    svc = service_state(backend, unit)
    cstat, cpid = client_pid(backend, dev)
    st_mark, mark_dev = read_state_file(backend)
    sysc, sysc_unknown = sysctl_values(backend)
    return {"device": dev, "swaps_status": status, "swaps_why": why,
            "in_swaps": (row is not None) if status == "ok" else None,
            "priority": row["priority"] if row else None,
            "unit_state": svc["state"], "main_pid": svc.get("main_pid"),
            "main_start_ticks": svc.get("main_start_ticks"),
            "main_start_epoch": svc.get("main_start_epoch"), "unit_unknown": svc["unknown"],
            "client_status": cstat, "client_pid": cpid,
            "state_file": st_mark, "state_file_device": mark_dev,
            "sysctls": sysc, "sysctls_unknown": sysc_unknown}


def proof_missing(f, prio, captured_sysctls, expect) -> list:
    """What is NOT proven, given the gathered facts.  An empty list is a proof."""
    miss = []
    if f["swaps_status"] != "ok":
        miss.append(f"/proc/swaps status {f['swaps_status']} ({f['swaps_why']})")
    if f["unit_state"] == "unknown":
        miss.append("unit state unknown: " + "; ".join(f["unit_unknown"]))
    if f["client_status"] == "unknown":
        miss.append(f"/sys/block/{os.path.basename(f['device'])}/pid unreadable")
    if f["state_file"] not in (READ_OK, READ_MISSING):
        miss.append(f"{STATE_FILE} status {f['state_file']}")
    if f["sysctls_unknown"]:
        miss.append("sysctls unknown: " + "; ".join(f["sysctls_unknown"]))
    same_sys = all(f["sysctls"].get(k) == v for k, v in captured_sysctls.items())
    if expect == "drained":
        if f["in_swaps"] is not False:
            miss.append("device is still in /proc/swaps")
        if f["unit_state"] != "inactive":
            miss.append("unit is not proven inactive")
        if f["state_file"] != READ_MISSING:
            miss.append(f"{STATE_FILE} is not proven absent (status {f['state_file']}, "
                        f"names {f['state_file_device']})")
        if f["client_status"] not in ("missing", "empty"):
            miss.append(f"NBD client record is still present ({f['client_status']} "
                        f"pid {f['client_pid']})")
        if not same_sys:
            miss.append(f"sysctls {f['sysctls']} do not match the captured {captured_sysctls}")
    elif expect == "restored":
        if f["in_swaps"] is not True:
            miss.append("device is not in /proc/swaps")
        if f["priority"] != prio:
            miss.append(f"priority {f['priority']} is not the recorded {prio}")
        if f["unit_state"] != "active":
            miss.append("unit is not proven active")
        if f["client_status"] != "connected":
            miss.append(f"NBD client is not proven connected ({f['client_status']})")
        if f["state_file"] != READ_OK:
            miss.append(f"{STATE_FILE} is not proven present (status {f['state_file']})")
        elif f["state_file_device"] != f["device"]:
            miss.append(f"{STATE_FILE} names {f['state_file_device']}, not the verified target "
                        f"{f['device']}")
        if not same_sys:
            miss.append(f"sysctls {f['sysctls']} do not match the captured {captured_sysctls}")
    else:
        raise Refuse(f"unknown proof expectation {expect!r}")
    return miss


def open_drain_record(backend, args, snap, unit, dev, backing, budget, plan) -> dict:
    """Reuse the existing operation record, validated, instead of truncating it.

    A prior partial attempt's steps and start identity are preserved and appended to; a
    foreign, unreadable or malformed record refuses BEFORE any mutation.
    """
    st, _ = backend.read(args.opstate)
    if st not in (READ_OK, READ_MISSING):
        raise Refuse(f"the operation record at {args.opstate} is unreadable ({st}) - it may hold "
                     f"a prior receipt, and this tool never writes over a record it cannot read")
    base = {}
    if st == READ_OK:
        base = load_opstate(backend, args.opstate, snap)
        if base.get("device") != dev or base.get("unit") != unit:
            raise Refuse(f"the existing record targets {base.get('device')}/{base.get('unit')} "
                         f"while this run targets {dev}/{unit} - refusing to overwrite it")
    prior = base.get("steps", [])
    ops = list(base.get("operations", []))
    rec = {
        "schema": OPSTATE_SCHEMA, "phase": "intent", "device": dev, "unit": unit,
        "port": int(args.port), "priority": backing["priority"],
        "snapshot_path": str(args.snapshot), "snapshot_sha256": snap["_sha256"],
        "sysctls_captured": snap["sysctls"],
        "started_iso": base.get("started_iso")
            or datetime.fromtimestamp(backend.epoch(), timezone.utc).isoformat(),
        "started_btime": base.get("started_btime", backing["btime"]),
        "steps": list(prior), "memory_check": budget, "plan": plan,
        "prior_phase": base.get("phase"),
    }
    ops.append({"op": "drain", "started_iso": rec["started_iso"], "btime": rec["started_btime"],
                "snapshot_sha256": snap["_sha256"], "from_phase": base.get("phase")})
    rec["operations"] = ops
    return rec


# ----------------------------------------------------------------------------------
# Drain: swapoff first, verify absence, only then disconnect / stop / sysctls.
# ----------------------------------------------------------------------------------
def drain(args, backend) -> int:
    snap = load_snapshot(backend, args.snapshot)
    validate_mutation_args(args, snap)
    unit = validate_unit(args.unit or snap.get("unit"))
    prod_processes_gone(backend, snap)
    if port_listening(backend, args.port):
        raise Refuse(f"refusing: endpoint port {args.port} is still listening")
    backing = resolve_backing(backend, snap, unit, same_daemon=True)
    dev = backing["device"]
    budget = check_memory_budget(backend, backing["swap_row"], args.mem_budget_kib,
                                 args.staging_kib, args.margin_kib)
    captured_sysctls = snap["sysctls"]

    # What is already done, proven only from live facts (never from a marker this tool
    # removed itself): swap entry, unit state, state file, client.  The sysctls are decided
    # AFTER the stop, because the installed ExecStop resets them to 60/65536.
    need_swapoff = backing["present_in_swaps"]
    daemon_active = backing["service"]["state"] == "active"
    state_present = backing["state_file_status"] == READ_OK
    need_disconnect = state_present or backing["client_status"] == "connected"
    if not (need_swapoff or daemon_active or state_present):
        done = live_facts(backend, dev, unit)
        miss = proof_missing(done, backing["priority"], captured_sysctls, "drained")
        if not miss:
            print(f"[ok] already drained and cleaned: {dev} absent from /proc/swaps, unit {unit} "
                  f"proven inactive, {STATE_FILE} absent, client record absent, sysctls match. "
                  f"No command run.")
            return E_OK
        print(f"[plan] drain: the swap/unit/marker are already clean, and the record is not "
              f"needed; only the sysctls are left: {miss}")

    plan = [
        f"swapoff {dev}" if need_swapoff else f"skip swapoff: {dev} is absent from /proc/swaps",
        f"nbd-client -d {dev} + prove detachment, then remove {STATE_FILE}"
        if need_disconnect else f"skip disconnect: {STATE_FILE} absent and no client record",
        f"systemctl stop {unit}" if daemon_active else f"skip stop: unit {unit} is inactive",
        f"re-read sysctls AFTER the stop (the installed ExecStop sets 60/65536), then sysctl -w "
        f"vm.swappiness={captured_sysctls['vm.swappiness']} "
        f"vm.min_free_kbytes={captured_sysctls['vm.min_free_kbytes']} if they differ",
        "final proof from live facts (swap table, unit, client record, marker, sysctls)",
    ]
    print("[plan] drain, in this order:")
    for line in plan:
        print("  " + line)
    print(f"[plan] memory: {json.dumps(budget, sort_keys=True)}")
    if plan_only(args):
        print("[plan] drain: no command was run. Re-run with --execute to mutate.")
        return E_OK

    # Durable intent BEFORE the first mutating command, reusing any existing record.
    rec = open_drain_record(backend, args, snap, unit, dev, backing, budget, plan)
    save_opstate(backend, args.opstate, rec)

    def note(step=None, phase=None):
        """Persist a step/phase; a persistence failure is an honest partial, never a silent
        continue (see persist)."""
        return persist(backend, args.opstate, rec, step=step, phase=phase)

    if need_swapoff:
        r = backend.run(["swapoff", dev], timeout=args.drain_timeout)
        status, rows, why = read_swaps(backend)
        if status != "ok":
            rec["phase"] = "drain-abort-swap-table-unknown"
            note({"argv": ["swapoff", dev], "rc": r.rc,
                  "post_read": f"/proc/swaps {status}: {why}"})
            print(f"[abort] swapoff rc={r.rc} and /proc/swaps is {status} ({why}): the swap "
                  f"state is unknown, so nothing is disconnected, stopped, cleaned or removed. "
                  f"[abort] retry the same command once the table is readable.", file=sys.stderr)
            return E_DRAIN_ABORTED
        still = row_for(rows, dev)
        if not r.ok or still is not None:
            rec["phase"] = "drain-failed"
            note({"argv": ["swapoff", dev], "rc": r.rc, "stderr": r.stderr.strip()[:400],
                  "timed_out": r.timed_out, "entry_still_present": still is not None})
            rec["next_action"] = ("leave the backing daemon running; free RAM, then re-run the "
                                  "same drain command. Nothing was disconnected, stopped, killed "
                                  "or cleaned.")
            note()
            print(f"[abort] swapoff {dev} failed (rc={r.rc}, entry present={still is not None}).",
                  file=sys.stderr)
            print("[abort] NOT disconnecting, NOT stopping the unit, NOT killing anything, NOT "
                  "removing state. Swap backing is left running with its device.", file=sys.stderr)
            print("[abort] safe to retry: re-run the same drain command once RAM frees up.",
                  file=sys.stderr)
            return E_DRAIN_ABORTED
        note({"argv": ["swapoff", dev], "rc": 0,
              "verified": f"{dev} absent from /proc/swaps"}, phase="drained")
    else:
        note({"skipped": f"{dev} already absent from /proc/swaps"}, phase="drained")

    # Recheck immediately before a destructive follow-up: the swap row must still be absent
    # and the marker readable, or a renewed swap / changed marker cannot be disconnected.
    pre = live_facts(backend, dev, unit)
    if pre["swaps_status"] != "ok":
        note({"precheck": f"/proc/swaps {pre['swaps_status']} ({pre['swaps_why']})"},
             phase="drain-precheck-unknown")
        print("[partial] the swap table is unreadable right before the disconnect step: the "
              "swap state is unknown, so nothing is disconnected, stopped or removed. Re-run "
              "the same drain command.", file=sys.stderr)
        return E_PARTIAL
    if pre["in_swaps"] is True:
        note({"precheck": f"{dev} reappeared in /proc/swaps"}, phase="drain-precheck-renewed")
        print(f"[partial] {dev} is in /proc/swaps again (priority {pre['priority']}) before the "
              "disconnect step: refusing to disconnect a device with live swap pages. Free RAM "
              "and re-run.", file=sys.stderr)
        return E_PARTIAL
    if pre["state_file"] == "malformed" or pre["state_file"] == READ_UNKNOWN:
        note({"precheck": f"{STATE_FILE} status {pre['state_file']}"},
             phase="drain-precheck-unknown")
        print(f"[partial] {STATE_FILE} is unreadable/malformed before the disconnect step - "
              "device identity is unknown, so nothing is disconnected, stopped or removed.",
              file=sys.stderr)
        return E_PARTIAL

    # Cleanup order matters: the installed nbd-vram-disconnect.sh runs swapoff again (it fails
    # after a manual drain, exits 1, and systemd then SIGTERMs the daemon under
    # KillMode=process), so disconnect first, then remove the marker, then stop.
    if need_disconnect:
        r = backend.run(["nbd-client", "-d", dev], timeout=args.timeout)
        cstatus, cpid = client_pid(backend, dev)
        detached = r.ok and cstatus in ("missing", "empty")
        if not detached:
            note({"argv": ["nbd-client", "-d", dev], "rc": r.rc,
                  "client_status_after": cstatus, "client_pid_after": cpid, "verified": False},
                 phase="drain-disconnect-failed")
            print(f"[partial] nbd-client -d {dev} rc={r.rc}, client record after = {cstatus}: "
                  f"detachment is NOT proven. {STATE_FILE} kept, unit not stopped, sysctls "
                  f"untouched. Re-run the same drain command (it resumes at this step).",
                  file=sys.stderr)
            return E_PARTIAL
        note({"argv": ["nbd-client", "-d", dev], "rc": 0, "client_status_after": cstatus,
              "verified": True})
        st_now, dev_now = read_state_file(backend)
        if st_now == READ_OK and dev_now == dev:
            backend.remove(STATE_FILE)
            note({"removed": STATE_FILE})
        elif st_now == READ_OK:
            note({"kept": f"{STATE_FILE} names {dev_now}, not {dev}"})
        else:
            note({"note": f"{STATE_FILE} read status {st_now} after verified detach"})

    if daemon_active:
        r = backend.run(["systemctl", "stop", unit], timeout=args.drain_timeout)
        after = service_state(backend, unit)
        if not r.ok or after["state"] != "inactive":
            note({"argv": ["systemctl", "stop", unit], "rc": r.rc,
                  "stderr": r.stderr.strip()[:300], "state_after": after["state"]},
                 phase="drain-stop-failed")
            print(f"[partial] systemctl stop {unit} rc={r.rc}, state after = {after['state']}. "
                  f"The unit is NOT proven stopped. Nothing further is done. Re-run the same "
                  f"drain command (it resumes at this step).", file=sys.stderr)
            return E_PARTIAL
        note({"argv": ["systemctl", "stop", unit], "rc": 0, "state_after": "inactive",
              "verified": True})

    # The installed ExecStop sets vm.swappiness=60 vm.min_free_kbytes=65536, so the sysctl
    # decision comes from a POST-STOP re-read, never from a pre-stop cache.  The re-read uses
    # the shared facts gatherer: an unreadable value is an unproven state (partial), not a
    # "refused" after mutations have already run.
    post = live_facts(backend, dev, unit)
    if post["sysctls_unknown"]:
        note({"sysctl_read": post["sysctls_unknown"]}, phase="drain-sysctl-failed")
        print(f"[partial] the sysctl values are unreadable ({post['sysctls_unknown']}) after the "
              f"stop. The swap is drained; the sysctls are unproven. Re-run the same drain "
              f"command.", file=sys.stderr)
        return E_PARTIAL
    if any(post["sysctls"].get(k) != v for k, v in captured_sysctls.items()):
        argv = ["sysctl", "-w", f"vm.swappiness={captured_sysctls['vm.swappiness']}",
                f"vm.min_free_kbytes={captured_sysctls['vm.min_free_kbytes']}"]
        r = backend.run(argv, timeout=args.timeout)
        after = live_facts(backend, dev, unit)
        match = all(after["sysctls"].get(k) == v for k, v in captured_sysctls.items())
        if not r.ok or not match:
            note({"argv": argv, "rc": r.rc, "values_after": after["sysctls"], "verified": match},
                 phase="drain-sysctl-failed")
            print(f"[partial] sysctl write rc={r.rc}, live values {after['sysctls']} do not "
                  f"match the captured {captured_sysctls}. Swap is drained; the sysctls are not "
                  f"set. Re-run the same drain command (it resumes at this step).",
                  file=sys.stderr)
            return E_PARTIAL
        if not note({"argv": argv, "rc": 0, "values_after": after["sysctls"], "verified": True}):
            return E_PARTIAL

    # Final proof from live facts only, with the same predicate verify uses.
    final = live_facts(backend, dev, unit)
    miss = proof_missing(final, backing["priority"], captured_sysctls, "drained")
    ok = not miss
    if not note({"post_check": {k: final[k] for k in ("swaps_status", "in_swaps", "priority",
                                                      "unit_state", "client_status",
                                                      "client_pid", "state_file",
                                                      "state_file_device", "sysctls")},
                 "missing": miss},
                phase="cleaned" if ok else "drain-postcheck-failed"):
        return E_PARTIAL
    if not ok:
        print(f"[partial] drain is not proven complete: {miss}. Re-run the same drain command.",
              file=sys.stderr)
        return E_PARTIAL
    print(f"[ok] drained {dev}: absent from /proc/swaps, client record detached, unit {unit} "
          f"inactive, {STATE_FILE} absent, sysctls match the captured values. "
          f"Record phase=cleaned.")
    print(f"[next] verify: maintenance.py verify --snapshot {args.snapshot} --expect drained")
    return E_OK


# ----------------------------------------------------------------------------------
# Restore: only what this tool offlined, from the snapshot and its own record.
# ----------------------------------------------------------------------------------
def restore(args, backend) -> int:
    snap = load_snapshot(backend, args.snapshot)
    validate_mutation_args(args, snap)
    rec = load_opstate(backend, args.opstate, snap)
    unit = validate_unit(rec["unit"])
    dev = validate_device(rec["device"])
    if not args.expect_prod_stopped:
        raise Refuse("restore needs --expect-prod-stopped: starting the unit while prod can "
                     "grab VRAM again is the race this window is meant to avoid")
    prod_processes_gone(backend, snap)

    backing = resolve_backing(backend, snap, unit)
    rec.setdefault("operations", []).append({
        "op": "restore",
        "started_iso": datetime.fromtimestamp(backend.epoch(), timezone.utc).isoformat(),
        "btime": backing["btime"], "snapshot_sha256": snap["_sha256"],
        "from_phase": rec.get("phase")})
    daemon_pid = backing["service"].get("main_pid") if backing["service"]["state"] == "active" else None
    captured_sysctls = snap["sysctls"]
    prio = int(rec.get("priority") or snap["_priority"])
    if not (0 <= prio <= 32767):
        raise Refuse(f"recorded swap priority {prio} is outside 0..32767")

    if backing["present_in_swaps"]:
        # Already active: prove it is THIS tool's shape (recorded priority, proved backing),
        # then let the shared sysctl step and the final proof decide.  Never claim restored
        # from presence alone, and never refuse-then-lose-the-record over a sysctl delta that
        # this same command can repair.
        prio_now = backing["swap_row"]["priority"]
        if prio_now != prio:
            raise Refuse(f"{dev} is active at priority {prio_now}, not the recorded {prio} - "
                         f"this is not the entry this tool offlined, refusing")
        if not (backing["service"]["state"] == "active"
                and backing["client_status"] == "connected"):
            raise Refuse(f"{dev} is active but its backing is not proved (unit "
                         f"{backing['service']['state']}, client {backing['client_status']}) - "
                         f"refusing to claim restored")
        bind = bind_restore_daemon(backend, backing["service"], snap, rec, backing["btime"],
                                   "already-active re-probe")
        if not persist(backend, args.opstate, rec,
                       {"re_probe": {"device": dev, "priority": prio_now,
                                     "client_status": backing["client_status"],
                                     "unit_state": backing["service"]["state"],
                                     "daemon_pid": backing["service"]["main_pid"],
                                     "daemon_binding": bind,
                                     "causal_proof": backing["causality"].get("proof")}}):
            return E_PARTIAL
        restored_dev, restored_prio = dev, prio_now
        print(f"[plan] restore: {dev} is already active at priority {prio} with a proved "
              f"backing; only the sysctls and the final proof are left.")
    elif backing["service"]["state"] == "active" and backing["client_status"] == "connected":
        # The backing daemon is alive and the device is connected: re-attaching swap allocates
        # no new VRAM, so swapon alone is the safe path - provided it is the SAME daemon the
        # snapshot measured (same start time), not a unit restarted since capture.
        require_captured_daemon(backing["service"], snap, bt=backing["btime"],
                                hz=backend.clock_ticks(), where="swapon path")
        require_compute_scope(backend, snap, args.allow_compute_pid, daemon_pid=daemon_pid)
        argv = ["swapon", dev, "-p", str(prio)]
        print("[plan] restore (backing daemon alive and device connected):")
        print("  " + " ".join(argv))
        if plan_only(args):
            print("[plan] restore: no command was run. Re-run with --execute to mutate.")
            return E_OK
        rec["restore_intent_epoch"] = backend.epoch()
        rec["restore_intent_btime"] = backing["btime"]
        save_opstate(backend, args.opstate, rec, phase="restore-intent",
                     step={"path": "swapon", "device": dev, "priority": prio})
        r = backend.run(argv, timeout=args.timeout)
        status, rows, why = read_swaps(backend)
        row = row_for(rows, dev) if status == "ok" else None
        if not (r.ok and status == "ok" and row is not None and row["priority"] == prio):
            persist(backend, args.opstate, rec,
                    {"argv": argv, "rc": r.rc, "table": status, "entry_after": row,
                     "verified": False}, phase="restore-swap-unproven")
            print(f"[partial] swapon rc={r.rc}, table={status}, entry after={row}: swap is not "
                  f"proven at priority {prio}. The record is KEPT; re-run restore (or drain "
                  f"from the beginning) - it resumes from the live state.", file=sys.stderr)
            return E_PARTIAL
        if not persist(backend, args.opstate, rec,
                       {"argv": argv, "rc": 0, "priority_after": row["priority"],
                        "verified": True}):
            return E_PARTIAL
        restored_dev, restored_prio = dev, row["priority"]
    elif backing["service"]["state"] == "inactive" and backing["client_status"] != "connected":
        if not args.confirm_cuda_released:
            raise Refuse("restore needs --confirm-cuda-released before starting the unit: the "
                         "unit reserves VRAM at ExecStart, and that must not race a CUDA client "
                         "still holding the device")
        vram = check_free_vram(backend, snap, args.vram_margin_mib)
        require_compute_scope(backend, snap, args.allow_compute_pid)
        argv = ["systemctl", "start", unit]
        print("[plan] restore (unit stopped, device not connected): start the unit; its own "
              "connect script connects, mkswaps and swaps on, and re-picks a free /dev/nbdN.")
        print("  " + " ".join(argv))
        print(f"[plan] free VRAM: {json.dumps(vram, sort_keys=True)}")
        if plan_only(args):
            print("[plan] restore: no command was run. Re-run with --execute to mutate.")
            return E_OK
        rec["restore_intent_epoch"] = backend.epoch()
        rec["restore_intent_btime"] = backing["btime"]
        save_opstate(backend, args.opstate, rec, phase="restore-intent",
                     step={"path": "unit-start", "unit": unit, "free_vram": vram})
        r = backend.run(argv, timeout=args.drain_timeout)
        # The unit may renumber the device, so re-derive the live device from the live state
        # file - never assume the snapshot's old name.
        st_now, dev_now = read_state_file(backend)
        if st_now != READ_OK:
            persist(backend, args.opstate, rec,
                    {"argv": argv, "rc": r.rc, "state_file": st_now,
                     "verified": False}, phase="restore-swap-unproven")
            print(f"[partial] systemctl start rc={r.rc}; {STATE_FILE} read status {st_now}: "
                  f"the new device is unknown. Record KEPT.", file=sys.stderr)
            return E_PARTIAL
        dev_now = validate_device(dev_now)
        # All post-start facts come from the ONE facts gatherer: an unreadable fact here is an
        # unproven state (partial), never an exception after a mutation.
        f = live_facts(backend, dev_now, unit)
        if not (r.ok and f["swaps_status"] == "ok" and f["in_swaps"] is True
                and f["priority"] == prio and f["client_status"] == "connected"
                and f["unit_state"] == "active"):
            persist(backend, args.opstate, rec,
                    {"argv": argv, "rc": r.rc, "device_now": dev_now, "table": f["swaps_status"],
                     "entry_after": f["priority"], "client_status": f["client_status"],
                     "unit_state_after": f["unit_state"], "verified": False},
                    phase="restore-swap-unproven")
            print(f"[partial] systemctl start rc={r.rc}: device now {dev_now}, table "
                  f"{f['swaps_status']}, priority {f['priority']}, client {f['client_status']}, "
                  f"unit {f['unit_state']}. Swap is not proven - the record is KEPT so recovery "
                  f"can continue.", file=sys.stderr)
            return E_PARTIAL
        if not persist(backend, args.opstate, rec,
                       {"argv": argv, "rc": 0, "device_now": dev_now,
                        "priority_after": f["priority"], "client_pid": f["client_pid"],
                        "unit_state_after": "active", "verified": True}):
            return E_PARTIAL
        restored_dev, restored_prio = dev_now, f["priority"]
    else:
        raise Refuse(f"state is not one this tool created (unit {backing['service']['state']}, "
                     f"client {backing['client_status']}, swap present "
                     f"{backing['present_in_swaps']}) - refusing to guess")

    sysc_argv = ["sysctl", "-w", f"vm.swappiness={captured_sysctls['vm.swappiness']}",
                 f"vm.min_free_kbytes={captured_sysctls['vm.min_free_kbytes']}"]
    if plan_only(args):
        print("[plan] restore: the swap step planned above, then " + " ".join(sysc_argv[2:])
              + " if needed, then the final proof.  No command was run.")
        return E_OK

    # The sysctl step reads the live values through the shared facts gatherer: an unreadable
    # value here is an unproven state (partial), never a "refused, nothing mutated" message
    # after a mutation has already happened.
    f = live_facts(backend, restored_dev, unit)
    if f["sysctls_unknown"]:
        persist(backend, args.opstate, rec, {"sysctl_read": f["sysctls_unknown"]},
                phase="restore-sysctl-failed")
        print(f"[partial] the sysctl values are unreadable ({f['sysctls_unknown']}) after the "
              f"swap step on {restored_dev}. Record KEPT; resolve the read, then re-run "
              f"restore.", file=sys.stderr)
        return E_PARTIAL
    if any(f["sysctls"].get(k) != v for k, v in captured_sysctls.items()):
        r = backend.run(sysc_argv, timeout=args.timeout)
        after = live_facts(backend, restored_dev, unit)
        match = all(after["sysctls"].get(k) == v for k, v in captured_sysctls.items())
        if not r.ok or not match:
            persist(backend, args.opstate, rec,
                    {"argv": sysc_argv, "rc": r.rc, "values_after": after["sysctls"],
                     "verified": match}, phase="restore-sysctl-failed")
            print(f"[partial] swap is restored on {restored_dev} at priority {restored_prio}, "
                  f"but the sysctl write rc={r.rc} left {after['sysctls']} (captured "
                  f"{captured_sysctls}). Record KEPT; re-run restore - it resumes from the "
                  f"live state.", file=sys.stderr)
            return E_PARTIAL
        if not persist(backend, args.opstate, rec,
                       {"argv": sysc_argv, "rc": 0, "values_after": after["sysctls"],
                        "verified": True}):
            return E_PARTIAL

    # The receipt is written only from a re-probe of the live state with the SAME predicates
    # `verify --expect restored` uses, plus the daemon binding, the socket identity and the
    # causal proof.  A command's exit status proves nothing on its own.
    f = live_facts(backend, restored_dev, unit)
    miss = proof_missing(f, prio, captured_sysctls, "restored")
    try:
        bt_final = require_boot_identity(backend, snap, "restore final proof")
        bind = bind_restore_daemon(backend, service_state(backend, unit), snap, rec, bt_final,
                                  "restore final proof")
    except Refuse as e:
        miss.append(str(e))
        bind = None
        bt_final = None
    try:
        require_socket_identity(backend, snap, "restore final proof")
    except Refuse as e:
        miss.append(str(e))
    ok = not miss
    proof = {"device": restored_dev, "in_swaps": f["in_swaps"],
             "priority": f["priority"], "priority_recorded": prio,
             "unit_state": f["unit_state"], "daemon_pid": f["main_pid"],
             "daemon_binding": bind, "client_status": f["client_status"],
             "client_pid": f["client_pid"], "state_file": f["state_file"],
             "state_file_device": f["state_file_device"], "sysctls": f["sysctls"],
             "swaps_status": f["swaps_status"], "missing": miss}
    if ok:
        rec["restored_device"] = restored_dev
        rec["restored_priority"] = restored_prio
        rec["restored_daemon_pid"] = f["main_pid"]
        rec["restored_daemon_binding"] = bind
        rec["restored_btime"] = bt_final
    persist(backend, args.opstate, rec, {"final_proof": proof},
            phase="restored" if ok else "restore-unproven-final")
    if not ok:
        print(f"[partial] restore is not proven: {json.dumps(miss, sort_keys=True)}. The "
              f"record is KEPT as the recovery record; re-run restore - it resumes from the "
              f"live state.", file=sys.stderr)
        return E_PARTIAL
    print(f"[ok] restore proven: {restored_dev} active at priority {prio}, client connected, "
          f"marker names it, sysctls match the captured values, daemon bound ({bind}) with the "
          f"causal proof. Record KEPT as the receipt (phase=restored).")
    print(f"[next] verify: maintenance.py verify --snapshot {args.snapshot} --expect restored")
    return E_OK


# ----------------------------------------------------------------------------------
# Verify: read-only proof.  Unknown never PASSes.
# ----------------------------------------------------------------------------------
def verify(args, backend) -> int:
    """Read-only proof of a claimed state.  Unknown never PASSes, and a PASS lists every
    predicate that was proved - not a printout of ignored facts.

    Prerequisites that cannot even be loaded (unreadable snapshot, foreign/malformed record)
    exit 2 (refused).  A state whose proofs fail exits 4.  Only a complete proof exits 0.
    """
    snap = load_snapshot(backend, args.snapshot)
    validate_mutation_args(args, snap)
    unit = validate_unit(args.unit or snap.get("unit"))
    dev = snap["_device"]
    prio = snap["_priority"]
    captured_sysctls = snap["sysctls"]

    if args.expect == "drained":
        f = live_facts(backend, dev, unit)
        miss = proof_missing(f, prio, captured_sysctls, "drained")
        try:
            prod_processes_gone(backend, snap)
        except Refuse as e:
            miss.append(f"recorded production not proven gone: {e}")
        try:
            if port_listening(backend, args.port):
                miss.append(f"endpoint port {args.port} is still listening")
        except Refuse as e:
            miss.append(f"endpoint port state unknown: {e}")
        gpu = gpu_release_note(backend, snap, dev, args.margin_mib)
        if not gpu.get("released"):
            miss.append("GPU backing release not proven: "
                        + json.dumps({k: v for k, v in gpu.items() if k != "gpus"},
                                     sort_keys=True))
        out = {"expect": "drained", "device": dev, "unit": unit, **{
            k: f[k] for k in ("swaps_status", "in_swaps", "priority", "unit_state", "main_pid",
                              "client_status", "client_pid", "state_file", "state_file_device",
                              "sysctls")}, "gpu_backing": gpu, "missing_proofs": miss}
        print(json.dumps(out, indent=2, sort_keys=True))
        print(f"[verify] drained: {'PASS' if not miss else 'FAIL'}")
        for line in miss:
            print(f"[verify]   missing: {line}")
        return E_OK if not miss else E_VERIFY_FAIL

    if args.expect == "restored":
        # The device to check comes ONLY from the validated, snapshot-bound receipt - never
        # from a raw JSON read, never from a guess at an old target.
        receipt = load_receipt(backend, args.opstate, snap)
        rec, target = receipt["record"], receipt["target"]
        prio_rec = receipt["priority"]
        f = live_facts(backend, target, unit)
        miss = proof_missing(f, prio_rec, captured_sysctls, "restored")
        bind = None
        try:
            bt = require_boot_identity(backend, snap, "verify restored")
            if rec.get("restored_btime") != bt:
                miss.append(f"the receipt names boot {rec.get('restored_btime')!r}, the live "
                            f"system is in boot {bt} - the receipt is from another boot")
            bind = bind_restore_daemon(backend, service_state(backend, unit), snap, rec, bt,
                                       "verify restored")
        except Refuse as e:
            miss.append(str(e))
        try:
            sock = require_socket_identity(backend, snap, "verify restored")
        except Refuse as e:
            miss.append(str(e))
            sock = None
        causal = probe_causality(backend, f["main_pid"]) if f["main_pid"] else {"proven": False}
        out = {"expect": "restored", "target_device": target,
               "snapshot_device": receipt["snapshot_device"], "unit": unit, "record_phase":
               rec.get("phase"), "record_restored_device": rec.get("restored_device"),
               "record_priority": prio_rec, "daemon_binding": bind,
               "causal_proof": causal.get("proof"), "causal_proofs": causal.get("proofs"),
               "socket": sock, **{k: f[k] for k in ("swaps_status", "in_swaps", "priority",
                                                    "unit_state", "main_pid",
                                                    "main_start_epoch", "client_status",
                                                    "client_pid", "state_file",
                                                    "state_file_device", "sysctls")},
               "missing_proofs": miss}
        print(json.dumps(out, indent=2, sort_keys=True))
        print(f"[verify] restored: {'PASS' if not miss else 'FAIL'}")
        for line in miss:
            print(f"[verify]   missing: {line}")
        return E_OK if not miss else E_VERIFY_FAIL

    raise Refuse(f"unknown --expect {args.expect!r}")


def gpu_release_note(backend, snap, dev, margin_mib: int) -> dict:
    """Is the GPU backing released?  Baseline is not a 0 MiB literal: display and driver
    allocations stay, so the proof is a drop of at least (swap size - margin) against the
    captured per-UUID usage."""
    swap = next((r for r in (snap.get("swaps") or {}).get("rows", []) if r["filename"] == dev), None)
    nv = snap.get("gpu", {}).get("nvidia", {})
    if not nv.get("available") or swap is None:
        return {"checked": False, "released": False,
                "note": "captured NVIDIA data or the swap row is missing - release unverified"}
    probe = gpu_snapshot(backend, DEFAULT_TIMEOUT).get("nvidia", {})
    if not probe.get("available"):
        return {"checked": False, "released": False,
                "note": f"live nvidia-smi unreadable ({probe.get('unknown')}) - unverified"}
    live = {g["uuid"]: g for g in probe.get("gpus", [])}
    size_mib = swap["size_kib"] // 1024
    need = size_mib - int(margin_mib)
    rows = []
    all_ok = True
    for g in nv.get("gpus", []):
        now = live.get(g.get("uuid"))
        if now is None:
            return {"checked": False, "released": False, "note": "captured UUID missing now"}
        drop = g["memory_used_mib"] - now["memory_used_mib"]
        rows.append({"uuid": g["uuid"], "before_used_mib": g["memory_used_mib"],
                     "after_used_mib": now["memory_used_mib"], "drop_mib": drop,
                     "need_drop_mib": need})
        all_ok = all_ok and drop >= need
    return {"checked": True, "released": all_ok, "swap_size_mib": size_mib,
            "margin_mib": int(margin_mib), "gpus": rows,
            "note": "baseline is display+driver allocations, never a 0 MiB literal"}


def plan_only(args) -> bool:
    """True when the caller did not pass --execute: print the plan, run no command."""
    return not getattr(args, "execute", False)


# ----------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="maintenance.py",
        description="Read-only baseline capture and guarded, idempotent VRAM-swap maintenance "
                    "for the mixed HIP/CUDA window. Nothing is mutated without --execute, and "
                    "unknown state never counts as success.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Read tools/mixed_gpu/README.md before any --execute run. Prod and the NVIDIA-"
               "backed swap stay up until the primary opens the maintenance window. Exit codes: "
               "0 done, 2 refused (pre-action, nothing mutated), 3 swapoff aborted, 4 verify "
               "failed, 5 partial step (mutations already happened), 6 internal (unexpected "
               "value, no cleanup attempted).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="read-only inventory; writes only --out (mode 0600)")
    c.add_argument("--out", required=True, help="snapshot file to write (never overwritten)")
    c.add_argument("--live-root", default=None, help="worktree whose git identity to record")
    c.add_argument("--binary", default=None, help="engine binary to hash (path + full sha256)")
    c.add_argument("--config", default=None, help="live config: referenced by path + hash, not copied")
    c.add_argument("--launcher", default=None, help="live launcher script to hash")
    c.add_argument("--build-dir", default=None, help="CMake build dir whose CMakeCache.txt to read")
    c.add_argument("--file", action="append", default=None, help="extra path to hash (repeatable)")
    c.add_argument("--unit", default=ALLOWED_UNITS[0])
    c.add_argument("--port", type=int, default=8009)
    c.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    c.set_defaults(func=_cmd_capture)

    for name, help_text in (("drain", "take the confirmed VRAM swap offline, drain-first"),
                            ("restore", "bring back only what this tool offlined")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--snapshot", required=True)
        p.add_argument("--opstate", default="build-mixed-gpu/opstate.json")
        p.add_argument("--execute", action="store_true",
                       help="REQUIRED to mutate; without it only the plan is printed")
        p.add_argument("--unit", default=None)
        p.add_argument("--port", type=int, default=None,
                       help="defaults to the snapshot's recorded port; a mismatch is refused")
        p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
        p.add_argument("--drain-timeout", type=float, default=DRAIN_TIMEOUT)
        if name == "drain":
            p.add_argument("--mem-budget-kib", type=int, default=None,
                           help="caller-provided post-shutdown memory budget; required, positive")
            p.add_argument("--staging-kib", type=int, default=0,
                           help="expected staging/peak RAM the window needs on top of the swap in use")
            p.add_argument("--margin-kib", type=int, default=DEFAULT_MARGIN_KIB)
        else:
            p.add_argument("--confirm-cuda-released", action="store_true",
                           help="caller confirms the CUDA device is released (unit start only)")
            p.add_argument("--expect-prod-stopped", action="store_true",
                           help="caller confirms prod stays stopped while VRAM is reserved")
            p.add_argument("--allow-compute-pid", action="append", default=None,
                           help="name an expected compute client (never relaxes VRAM capacity)")
            p.add_argument("--vram-margin-mib", type=int, default=DEFAULT_VRAM_MARGIN_MIB,
                           help="headroom above the unit's VRAM reservation for the unit start")
        p.set_defaults(func=_cmd_drain if name == "drain" else _cmd_restore)

    v = sub.add_parser("verify", help="read-only postcondition check; unknown never PASSes")
    v.add_argument("--snapshot", required=True)
    v.add_argument("--expect", choices=("drained", "restored"), required=True)
    v.add_argument("--opstate", default="build-mixed-gpu/opstate.json")
    v.add_argument("--unit", default=None)
    v.add_argument("--port", type=int, default=None)
    v.add_argument("--margin-mib", type=int, default=512)
    v.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    v.set_defaults(func=_cmd_verify)
    return ap


def _cmd_capture(args, backend):
    snap = capture(args, backend)
    write_snapshot(backend, snap, args.out)
    print("[ok] snapshot written " + args.out + " (0600); files: "
          + ", ".join(f"{f['label']}={f.get('sha256', f.get('missing') and 'missing' or 'unreadable')}"
                      for f in snap["files"]))
    print("[ok] swap table status " + snap["swaps"]["status"] + "; unit state "
          + snap["service"]["state"] + "; nbd device " + str(snap["nbd_state_file"].get("device")))
    return E_OK


def _cmd_drain(args, backend):
    return drain(args, backend)


def _cmd_restore(args, backend):
    return restore(args, backend)


def _cmd_verify(args, backend):
    return verify(args, backend)


def main(argv=None, backend=None) -> int:
    backend = backend or RealBackend()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args, backend))
    except Refuse as e:
        print(f"[refused] {e}", file=sys.stderr)
        return E_REFUSED
    except ParseFail as e:
        print(f"[refused] parse failure: {e}", file=sys.stderr)
        return E_REFUSED
    except KeyboardInterrupt:
        print("[abort] interrupted; the operation record says where to resume", file=sys.stderr)
        return E_PARTIAL
    except (TypeError, ValueError, KeyError, IndexError, AttributeError, ArithmeticError,
            OSError) as e:
        # A programming bug or an unexpected value: reported, not a traceback, and NOT
        # reported as "refused, nothing mutated" - mutations may already have happened, so
        # the operation record on disk is the truth and no cleanup is attempted.
        print(f"[internal] {type(e).__name__}: {e} - unexpected value while running "
              f"{getattr(args, 'func', 'command')!r}. No cleanup is attempted; the operation "
              f"record (if present) is the disk truth. Exit {E_INTERNAL}.", file=sys.stderr)
        return E_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
